"""Offline tests for persisted source-health staleness.

No sockets, no network: probes are faked with simple objects; the store
is pointed at a temp file. The no-socket guard stays intact.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sources.staleness import (  # noqa: E402
    HEALTHY, NEVER_OK, STALE, UNSEEN, HealthStore, SourceHealth,
    amend_null_classification, stale_sources_among)


@dataclass
class FakeProbe:
    source: str
    verdict: str
    row_count: int = 5
    evidence: list = None

    def __post_init__(self):
        if self.evidence is None:
            self.evidence = [f"{self.verdict} evidence"]

    def to_dict(self) -> dict:
        return {"source": self.source, "verdict": self.verdict,
                "row_count": self.row_count, "evidence": self.evidence}


class TestHealthStore:
    def test_record_ok_then_degraded_is_stale(self, tmp_path):
        store = HealthStore(tmp_path / "h.json")
        rec = store.record(FakeProbe("openalex", "OK", row_count=9))
        assert rec.status == HEALTHY
        assert rec.last_ok is not None
        assert rec.consecutive_bad == 0
        rec = store.record(FakeProbe("openalex", "DEGRADED"))
        # worked before, empty now — the dangerous silent-drift case
        assert rec.status == STALE
        assert rec.consecutive_bad == 1
        # last_ok survives: history is the point
        assert rec.last_ok is not None

    def test_broken_counts_toward_consecutive_bad(self, tmp_path):
        store = HealthStore(tmp_path / "h.json")
        for _ in range(3):
            rec = store.record(FakeProbe("treasury", "BROKEN"))
        assert rec.consecutive_bad == 3
        assert rec.status == NEVER_OK      # never succeeded on record

    def test_ok_resets_bad_streak(self, tmp_path):
        store = HealthStore(tmp_path / "h.json")
        store.record(FakeProbe("fdic", "BROKEN"))
        store.record(FakeProbe("fdic", "BROKEN"))
        rec = store.record(FakeProbe("fdic", "OK", row_count=4))
        assert rec.consecutive_bad == 0
        assert rec.status == HEALTHY

    def test_skipped_changes_neither_counter(self, tmp_path):
        store = HealthStore(tmp_path / "h.json")
        store.record(FakeProbe("fred", "BROKEN"))
        rec = store.record(FakeProbe("fred", "SKIPPED",
                                     evidence=["requires key"]))
        assert rec.consecutive_bad == 1     # unchanged by the skip
        assert rec.last_verdict == "SKIPPED"
        assert rec.status == NEVER_OK       # still no success on record

    def test_persistence_roundtrip(self, tmp_path):
        p = tmp_path / "h.json"
        store = HealthStore(p)
        store.record(FakeProbe("cftc", "OK", row_count=12))
        store.record(FakeProbe("bls", "OK", row_count=3))
        store.record(FakeProbe("bls", "DEGRADED"))
        raw = json.loads(p.read_text())
        assert raw["sources"]["cftc"]["last_verdict"] == "OK"
        reopened = HealthStore(p)
        assert reopened.status_of("cftc") == HEALTHY
        assert reopened.status_of("bls") == STALE
        assert reopened.status_of("wikidata") == UNSEEN

    def test_corrupt_store_degrades_to_empty(self, tmp_path):
        p = tmp_path / "h.json"
        p.write_text("{not json")
        store = HealthStore(p)
        assert store.all_records() == {}
        assert store.status_of("openalex") == UNSEEN


class TestNullAmendment:
    def test_honest_null_flips_when_a_leaned_source_went_stale(self, tmp_path):
        store = HealthStore(tmp_path / "h.json")
        store.record(FakeProbe("clinicaltrials", "OK", row_count=121))
        store.record(FakeProbe("clinicaltrials", "DEGRADED", row_count=0))
        import tools.sources.staleness as st
        orig = st.HealthStore
        st.HealthStore = lambda path=None: store   # pin the temp store
        try:
            kind, expl = amend_null_classification(
                "honest_null", "searched 3 queries",
                ["clinicaltrials", "openalex"])
        finally:
            st.HealthStore = orig
        assert kind == "retrieval_failure"
        assert "health-history amendment" in expl
        assert "clinicaltrials" in expl

    def test_no_history_never_flips(self, tmp_path):
        import tools.sources.staleness as st
        orig = st.HealthStore
        st.HealthStore = lambda path=None: HealthStore(
            tmp_path / "empty.json")
        try:
            kind, expl = amend_null_classification(
                "honest_null", "x", ["openalex"])
        finally:
            st.HealthStore = orig
        assert kind == "honest_null"

    def test_never_ok_does_not_flip(self, tmp_path):
        # A source that NEVER succeeded has no earned claim about silence;
        # absence of evidence must not invent a failure either.
        store = HealthStore(tmp_path / "h.json")
        store.record(FakeProbe("uspto_odp", "BROKEN"))
        assert stale_sources_among.__module__
        import tools.sources.staleness as st
        orig = st.HealthStore
        st.HealthStore = lambda path=None: store
        try:
            kind, _ = amend_null_classification(
                "honest_null", "", ["uspto_odp"])
        finally:
            st.HealthStore = orig
        assert kind == "honest_null"

    def test_retrieval_failure_not_re_amended(self, tmp_path):
        kind, expl = amend_null_classification(
            "retrieval_failure", "source errors", [])
        assert kind == "retrieval_failure"


# ── gaps.py integration ────────────────────────────────────────────────────

def test_classify_null_kind_consults_history(monkeypatch, tmp_path):
    from tools.pipeline.retrieval import RetrievalTrace
    import tools.gaps as gaps
    import tools.sources.staleness as st

    store = HealthStore(tmp_path / "h.json")
    store.record(FakeProbe("gdelt", "OK", row_count=25))
    store.record(FakeProbe("gdelt", "DEGRADED", row_count=0))
    monkeypatch.setattr(st, "HealthStore", lambda path=None: store)

    trace = RetrievalTrace(question_id="q1")
    trace.rounds = [{"sources": [{"name": "gdelt"}]}]
    trace.stop_reason = "search exhausted"
    kind, expl = gaps.classify_null_kind(trace)
    assert kind == "retrieval_failure"
    assert "history of good results" in expl


def test_classify_null_kind_unaffected_without_history(monkeypatch, tmp_path):
    from tools.pipeline.retrieval import RetrievalTrace
    import tools.gaps as gaps
    import tools.sources.staleness as st

    monkeypatch.setattr(st, "HealthStore",
                        lambda path=None: HealthStore(tmp_path / "none.json"))
    trace = RetrievalTrace(question_id="q1")
    trace.rounds = [{"sources": [{"name": "gdelt"}]}]
    kind, _ = gaps.classify_null_kind(trace)
    assert kind == "honest_null"


def test_health_main_persists(monkeypatch, tmp_path, capsys):
    """The CLI records its observations into the default store."""
    import tools.sources.health as health
    monkeypatch.setenv(health.NET_GATE_ENV, "1")
    monkeypatch.setattr(health, "run_all",
                        lambda names=None: [
                            FakeProbe("wayback", "OK", row_count=1),
                            FakeProbe("eia", "SKIPPED"),
                            FakeProbe("fdic", "BROKEN")])
    import tools.sources.staleness as st
    target = tmp_path / "cli-history.json"
    monkeypatch.setattr(st, "default_store_path", lambda: target)
    rc = health.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1                      # fdic BROKEN -> nonzero exit
    assert {r["verdict"] for r in out} == {"OK", "SKIPPED", "BROKEN"}
    stored = HealthStore(target)
    assert stored.status_of("wayback") == HEALTHY
    assert stored.status_of("fdic") == NEVER_OK
