"""Review run 2026-08-23 (staleness) — repros for findings/review_20260823_staleness.md.

Target: build/source-staleness @ ee6db37 (unmerged; proven failing on a
detached worktree of those exact bytes). The module does not exist on this
branch yet, so the file importorskips and activates the moment the branch
merges. Repros are xfail(strict=True): XFAIL while the defect lives,
XPASS->failure forces this file to be revisited when a fix lands.
Controls in the same file PASS on the branch bytes, proving each xfail
fails for its stated reason rather than by import noise or a broken rule.

S1 (family 1 — a check wired where its input never arrives): the health-
    history amendment in tools/gaps.classify_null_kind sits ONLY in the
    fall-through branch reachable when a round has no rejected / admitted /
    error / skipped entries. A stale source returning HTTP 200 with an
    empty body is REJECTED at the relevance gate with a reason, which takes
    the first branch (`reachable_attempt`) and returns honest_null without
    consulting history. The 200-empty signature of the eleven live-API
    defects — the case the module says it exists for — cannot reach the
    amendment through any trace IterativeRetriever emits.

S2 (families 3+6 — absence of evidence manufactured as evidence of
    failure): HealthStore.status() derives STALE from `last_ok is not None`
    after record(SKIPPED) overwrites last_verdict. Any scheduled probe run
    where a source's API key is unset (the CLI persists SKIPPED verdicts by
    default) demotes a HEALTHY source to STALE, which then flips honest
    nulls to retrieval_failure and injects SOURCE COVERAGE WARNINGs into
    sealed conclusions. Findings/docstrings claim SKIPPED is neutral; it
    is not, at the derived-status level.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("tools.sources.staleness",
                    reason="defects live on build/source-staleness "
                           "(unmerged); activate on merge")

from tools.pipeline.retrieval import (  # noqa: E402
    RejectedItem,
    RetrievalTrace,
)
from tools.sources.staleness import (  # noqa: E402
    HEALTHY, NEVER_OK, STALE, HealthStore, amend_null_classification)


@dataclass
class FakeProbe:
    source: str
    verdict: str
    row_count: int = 5
    evidence: list = field(default_factory=list)


# ── S1 ────────────────────────────────────────────────────────────────────

def _trace_as_retriever_emits_it() -> RetrievalTrace:
    """The trace IterativeRetriever.retrieve actually records when a source
    with a history of good results returns HTTP 200 with an empty body:
    the gate rejects it with a reason (retrieval.py:588-596), producing a
    round entry {"name", "rejected": ...} AND a trace.rejected item."""
    t = RetrievalTrace(question_id="q1")
    t.rounds = [{"round": 1, "query": "clinical trials", "admitted": 0,
                 "sources": [{"name": "clinicaltrials",
                              "rejected": ("content covers 0% of the "
                                           "question's topical words")}]}]
    t.rejected = [RejectedItem(
        source_name="clinicaltrials", url="https://x/api",
        reason="content covers 0%", relevance_score=0.0)]
    t.stop_reason = "round budget exhausted"
    return t


@pytest.mark.xfail(strict=True, reason="S1: amendment unreachable on the "
                                       "gate-rejected branch")
def test_s1_stale_source_200_empty_flips_to_retrieval_failure(monkeypatch,
                                                              tmp_path):
    """THE module's stated target case (findings/source_staleness.md:
    'empty response from a stale source -> gap_kind = retrieval_failure')."""
    import tools.gaps as gaps
    import tools.sources.staleness as st

    store = HealthStore(tmp_path / "h.json")
    store.record(FakeProbe("clinicaltrials", "OK", row_count=121))
    store.record(FakeProbe("clinicaltrials", "DEGRADED", row_count=0))
    assert store.status_of("clinicaltrials") == STALE
    monkeypatch.setattr(st, "HealthStore", lambda path=None: store)

    kind, expl = gaps.classify_null_kind(_trace_as_retriever_emits_it())
    assert kind == "retrieval_failure", (
        f"stale-source null stayed '{kind}' — amendment never ran on the "
        f"gate-rejected branch: {expl[:160]}")


def test_s1_control_hand_built_shape_does_flip(monkeypatch, tmp_path):
    """CONTROL (must pass): the amendment works when reached — S1 is about
    reachability, not a broken rule. This is the hand-built shape
    tests/test_source_staleness.py uses, which no real retriever emits."""
    import tools.gaps as gaps
    import tools.sources.staleness as st

    store = HealthStore(tmp_path / "h.json")
    store.record(FakeProbe("gdelt", "OK", row_count=25))
    store.record(FakeProbe("gdelt", "DEGRADED", row_count=0))
    monkeypatch.setattr(st, "HealthStore", lambda path=None: store)

    t = RetrievalTrace(question_id="q1")
    t.rounds = [{"sources": [{"name": "gdelt"}]}]   # no rejected/error keys
    kind, expl = gaps.classify_null_kind(t)
    assert kind == "retrieval_failure"
    assert "history of good results" in expl


# ── S2 ────────────────────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="S2: record(SKIPPED) demotes "
                                       "HEALTHY to STALE")
def test_s2_skipped_probe_must_not_demote_healthy_to_stale(tmp_path):
    store = HealthStore(tmp_path / "h.json")
    store.record(FakeProbe("openalex", "OK", row_count=9))
    assert store.status_of("openalex") == HEALTHY
    store.record(FakeProbe("openalex", "SKIPPED",
                           evidence=["requires OPENALEX_KEY; not configured"
                                     " — live health unknown"]))
    assert store.status_of("openalex") == HEALTHY, (
        "a SKIPPED probe (key unset) reclassified a healthy source STALE")


@pytest.mark.xfail(strict=True, reason="S2 end-to-end: SKIPPED-poisoned "
                                       "history flips an honest null")
def test_s2_end_to_end_cli_skip_then_null_flip(tmp_path, monkeypatch):
    """Day 1 openalex probes OK; day 2 a probe runs without its key set and
    main()'s default persist path records SKIPPED; afterwards an otherwise-
    honest null leaning on openalex flips to retrieval_failure off
    manufactured staleness."""
    import tools.sources.staleness as st

    p = tmp_path / "h.json"
    store = HealthStore(p)
    store.record(FakeProbe("openalex", "OK", row_count=9))
    store.record(FakeProbe("openalex", "SKIPPED"))
    monkeypatch.setattr(st, "HealthStore",
                        lambda path=None: HealthStore(p))

    kind, expl = amend_null_classification("honest_null", "", ["openalex"])
    assert kind == "honest_null", (
        f"null flipped to {kind} on staleness manufactured by a SKIPPED "
        f"probe: {expl[:140]}")


def test_s2_control_skipped_on_never_ok_stays_never_ok(tmp_path):
    """CONTROL (must pass): the neutrality their own suite pins keeps
    working — S2 is specifically the HEALTHY -> SKIPPED transition."""
    store = HealthStore(tmp_path / "h.json")
    store.record(FakeProbe("fred", "BROKEN"))
    rec = store.record(FakeProbe("fred", "SKIPPED"))
    assert rec.status == NEVER_OK
