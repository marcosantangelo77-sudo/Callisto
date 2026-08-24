"""Standing-review run 2026-08-23 (review/rotating-0823-224921).

Reproductions for findings/review_2026-08-23.md. Scope reviewed: the four
unreviewed merges at the head of backlog2 — stasis stopping rule
(8f45d1e), derived-analysis loop (2ce0413/54e9a81), expected-gain gating
(6195e45), and the source-health probe (f38550e).

R1 (CRITICAL, family 1/7 shape): StasisStop's guarantee — "a round that
changed nothing proves further rounds cannot change anything" — holds only
in a CLOSED world where the next round's candidate set equals this one's.
The retriever rotates its candidate set every barren round
(`excluded.update(specs)` when nothing was admitted), so a stasis stop can
fire while sources NEVER YET TRIED are still queued; those sources could
admit evidence and move tier/keys/shas. The 27-case proof corpus never
builds this shape (its leaves admit in round 1 or have no further routes),
so the proof holds there and fails here.

R2 (HIGH, family 4): estimate_gain decides "duplicate voice" via a STRING
MATCH on another module's prose (`"independent sources <" in reason`).
Reword agp.EvidenceRequirement.unmet_reasons without changing meaning and
the duplicate-voice skip silently evaporates. Pinned below by rewording.

R3 (MEDIUM, family 4): tools/sources/health.py `_wayback` writes
http_status=200 into its evidence without any HTTP exchange having been
observed on that path — a label standing in for evidence inside the very
module whose job is to replace labels with evidence.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import ssl  # must precede the socket guard
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket

NoSocket().install()

import pytest  # noqa: E402

from agp import Domain, Evidence, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever,
    RelevanceGate,
    estimate_gain,
)
from tools.sources.base import SourceSpec  # noqa: E402
from tools.sources.registry import SourceRegistry, SourceAdapter  # noqa: E402

CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

Q = ("What does research say about semiconductor supply chain "
     "resilience?")
ANSWERS = ["semiconductor supply chain resilience scholarly works"]

GOOD_A = json.dumps({"results": [
    {"id": "E1", "title": "Semiconductor supply chain resilience review"}]})
GOOD_B = json.dumps({"results": [
    {"id": "F1", "title": "Resilience of the semiconductor supply chain"}]})
JUNK = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})


def _spec(name):
    return SourceSpec(
        name=name, base_url=f"https://{name}.example", description="",
        answers=tuple(ANSWERS), tier=1, min_interval_s=0.0)


def _registry(names):
    reg = SourceRegistry()

    def make_adapter(source):
        host = source.spec.base_url.split("//")[1].split(".")[0]

        class _Ad:
            def works_search(self, term, limit=3):
                url = source.build_url(
                    f"/{host}", {"search": term.replace(" ", "+")})
                return source.get_json(url)[0]
        return _Ad()

    for n in names:
        reg.register(SourceAdapter(spec=_spec(n), make_adapter=make_adapter))
    return reg


def _routes():
    r = {f"/{n}?": JUNK for n in ("alpha", "bravo", "charlie", "delta")}
    r["/echo?"] = GOOD_A
    r["/foxtrot?"] = GOOD_B
    return r


def _question(min_ind=2):
    rq = ResearchQuestion(text=Q, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _retriever(reg, routes, **kw):
    r = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=fixture_transport(routes),
        gate=RelevanceGate(min_coverage=0.25),
        max_rounds=3, max_sources_per_leaf=2,
        generic_calls={n: ("works_search", ("term",), {"limit": 3})
                       for n in reg.names()},
        **kw)
    return r


def _state(trace, ledger):
    classes = []
    for f in trace.admitted:
        ev = Evidence(content=f.body[:4000],
                      source_class=SourceClass.INFERRED,
                      confidence_score=0.30, domain=Domain.GENERAL,
                      origin_agent="pipeline",
                      source_name=f.source_name)
        classes.append(ledger.assign_source_class(ev).value)
    best = max(classes, key=lambda c: CLASS_RANK.get(c, 0)) if classes \
        else None
    return {
        "best_class": best,
        "indep_keys": sorted(trace.independent_keys),
        "distinct_shas": sorted({f.content_sha256 for f in trace.admitted}),
    }


NAMES = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def test_baseline_reaches_the_untried_sources():
    """Control: without the stop rule the rotating candidate set reaches
    echo/foxtrot in round 3 and the leaf becomes sufficient."""
    tr = _retriever(_registry(NAMES), _routes()).retrieve(
        _question(), "", min_independent=2)
    fetched = {r["name"] for rnd in tr.rounds
               for r in rnd["sources"] if r.get("admitted")}
    assert len(tr.rounds) == 3
    assert {"echo", "foxtrot"} <= fetched
    assert tr.stop_reason.startswith("sufficient")


def test_r1_stasis_must_not_stop_while_untried_sources_remain():
    """THE REPRODUCTION. After two barren rounds the state fingerprint is
    (empty, empty) == (empty, empty), so StasisStop fires — but rounds 3's
    candidates (echo, foxtrot) have NEVER BEEN QUERIED and would have been
    admitted. The sealed conclusion therefore DIFFERS with the rule on:
    the exact outcome 'stasis: ... further rounds cannot alter
    tier/stance/confidence' claims to be impossible."""
    reg_b, reg_s = _registry(NAMES), _registry(NAMES)
    routes = _routes()
    base_r = _retriever(reg_b, routes)
    base = base_r.retrieve(_question(), "", min_independent=2)
    from tools.pipeline.stasis_stop import StasisStop
    sta_r = _retriever(reg_s, routes)
    sta_r.stasis_stop = StasisStop()
    sta = sta_r.retrieve(_question(), "", min_independent=2)

    assert sta.stop_reason.startswith("stasis"), sta.stop_reason
    assert not sta.admitted, (
        "stasis stopped after barren rounds yet admits differ")
    assert base.stop_reason.startswith("sufficient")
    fetched_base = {r["name"] for rnd in base.rounds
                    for r in rnd["sources"]}
    assert "echo" in fetched_base and "foxtrot" in fetched_base
    fetched_sta = {r["name"] for rnd in sta.rounds
                   for r in rnd["sources"]}
    assert "echo" not in fetched_sta and "foxtrot" not in fetched_sta
    # The invariant the module was built and merged to guarantee:
    assert _state(sta, sta_r.ledger) == _state(base, base_r.ledger)


def test_r1b_same_closed_world_shape_in_InformationGainTerminator():
    """Second instance of the new family (documented, not regressed):
    the terminator's 'confidence' is the indep-keys ratio, so two barren
    rounds stop the loop even though rounds 3's candidates were never
    queried and rounds 4's pool holds relevant sources. Current behaviour
    passes this test; it is pinned so the family has both instances on
    record."""
    names = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
             "golf", "hotel")
    routes = {f"/{n}?": JUNK
              for n in ("alpha", "bravo", "charlie", "delta", "echo",
                        "golf")}
    routes["/foxtrot?"] = GOOD_A
    routes["/hotel?"] = GOOD_B
    tr = _retriever(_registry(names), routes)
    tr.max_rounds = 4
    trace = tr.retrieve(_question(), "", min_independent=2)
    assert len(trace.rounds) < 4          # stopped early on stagnation
    assert not trace.admitted             # while nothing had been tried
    assert trace.stop_reason              # and relevant sources remained


def test_r2_duplicate_voice_skip_survives_reworded_reason_prose():
    """The rule as documented: a voice already counted can never address an
    independence shortfall. estimate_gain implements it by substring-
    matching agp's sentence. Same meaning, different words -> the rule
    disappears and a provably useless call is rated worth making."""
    class Reworded(EvidenceRequirement):
        def unmet_reasons(self, achieved_source_class, independent_sources,
                          produced_quant):
            reasons = []
            if independent_sources < self.min_independent_sources:
                reasons.append(f"only {independent_sources} distinct "
                               f"voices; need "
                               f"{self.min_independent_sources}")
            return reasons

    keys = {"family-openalex"}
    est = estimate_gain(
        SourceSpec(name="openalex_copy", base_url="https://x.example",
                   description="", answers=("anything",), tier=1,
                   min_interval_s=0.0),
        Reworded(min_source_class=SourceClassRank.SECONDARY,
                 min_independent_sources=2),
        keys, "scholarly works")
    assert est.duplicate_voice is False  # today: prose match found nothing
    assert est.worth_the_call is False  # the documented rule must hold


def _health():
    import tools.sources.health as h
    return h


def test_r3_wayback_evidence_status_is_observed_not_asserted():
    """health.py's own contract: 'Each verdict carries evidence ... HTTP
    status'. The wayback probe stamps http_status=200 unconditionally —
    no transport exists on that path, so the number cannot have come from
    an observation. With the adapter mocked to answer, the reported status
    must still be None (unknown), never a fabricated 200."""
    h = _health()
    real_build = h._build

    class FakeSrc:
        def build_url(self, path, params):
            return f"https://web.archive.example{path}"

    class FakeClosest:
        def closest(self, url):
            return {"archived_snapshots": {"closest": {
                "timestamp": "20240101", "status": "200"}}}

    h._build = lambda name: (FakeSrc(), FakeClosest())
    try:
        res = h._wayback()
    finally:
        h._build = real_build
    assert res.verdict == "OK"
    assert res.http_status is None, (
        f"verdict evidence asserts HTTP {res.http_status} that no "
        "exchange ever produced")


def test_health_gate_fails_closed_without_opt_in(monkeypatch):
    h = _health()
    monkeypatch.delenv(h.NET_GATE_ENV, raising=False)
    with pytest.raises(RuntimeError):
        h.run_all()


def test_health_unknown_source_is_broken_not_ok(monkeypatch):
    h = _health()
    monkeypatch.setenv(h.NET_GATE_ENV, "1")
    res = h.run_all(["no_such_source"])
    assert res[0].verdict == "BROKEN"
