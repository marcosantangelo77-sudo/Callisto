"""Stopping-rule study tests — measurement, then proof.

JOB 1  the retriever's round observer reports cumulative conclusion-relevant
       state after every round (instrumentation only).
JOB 3  StasisStop: identical final conclusion state with fewer rounds, and
       an honest null never collapses into a retrieval failure (or vice
       versa) because of the stop.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ssl  # must precede the socket guard
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket

NoSocket().install()

from agp import Domain, Evidence, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.gaps import NullKind, classify_null_kind  # noqa: E402
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from tools.pipeline.stasis_stop import StasisStop  # noqa: E402
from tools.sources.registry import get_source_registry  # noqa: E402

CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}


def _q(text="What does research say about semiconductor supply chain "
             "resilience?", min_ind=2):
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _run(question, qtype, routes, min_ind=2, stasis=False):
    ledger = ProvenanceLedger()
    obs = []
    r = IterativeRetriever(registry=get_source_registry(), ledger=ledger,
                           transport=fixture_transport(routes))
    if stasis:
        r.stasis_stop = StasisStop()
    if obs is not None:
        r.round_observer = obs.append
    trace = r.retrieve(question, qtype, min_independent=min_ind)
    classes = []
    for f in trace.admitted:
        ev = Evidence(content=f.body[:4000],
                      source_class=SourceClass.INFERRED,
                      confidence_score=0.30, domain=Domain.GENERAL,
                      origin_agent="pipeline", source_name=f.source_name)
        classes.append(ledger.assign_source_class(ev).value)
    best = max(classes, key=lambda c: CLASS_RANK.get(c, 0)) if classes \
        else None
    return trace, obs, {
        "best_class": best,
        "indep_keys": sorted(trace.independent_keys),
        "distinct_shas": sorted({f.content_sha256 for f in trace.admitted}),
    }


GDELT_RELEVANT = json.dumps({"articles": [
    {"title": "Apple quarterly earnings results above Wall Street consensus "
              "expectations in its next report",
     "url": "https://news0.example.org/a", "seendate": "20240110T120000"},
]})
CT_RELEVANT = json.dumps({"studies": [{"protocolSection": {
    "identificationModule": {"nctId": "NCT1",
                             "briefTitle": "Apple consensus expectations "
                                           "study of quarterly earnings "
                                           "reports"}}}]})
GDELT_IRRELEVANT = json.dumps({"articles": [
    {"title": "Mating habits of deep-sea isopods",
     "url": "https://news0.example.org/a", "seendate": "20240110T120000"}]})


def test_observer_reports_each_round():
    trace, obs, _state = _run(_q(), "scholarly work search",
                              {"/works": json.dumps({"results": [
                                  {"id": "W1", "title": "Semiconductor "
                                   "supply chain resilience review"}]})},
                              stasis=False)
    assert len(obs) == len(trace.rounds) >= 1
    for i, o in enumerate(obs, start=1):
        assert o["round"] == i
        assert isinstance(o["indep_keys"], list)
        assert isinstance(o["admitted"], list)


def test_stasis_identical_conclusion_fewer_rounds_on_null():
    """All-irrelevant corpus: baseline burns all rounds re-fetching the same
    nothing; stasis stops one round after the miss. Conclusion state and gap
    classification IDENTICAL."""
    routes = {"/doc": GDELT_IRRELEVANT}
    base_trace, _, base_state = _run(
        _q("Will Apple report quarterly results above consensus?"),
        "news coverage of events", routes, stasis=False)
    sta_trace, _, sta_state = _run(
        _q("Will Apple report quarterly results above consensus?"),
        "news coverage of events", routes, stasis=True)
    # fewer rounds...
    assert len(sta_trace.rounds) < len(base_trace.rounds)
    # ...identical conclusion inputs...
    assert sta_state == base_state
    # ...and the null keeps its HONEST classification — stopping did not
    # turn it into a retrieval failure or vice versa.
    b_kind, _ = classify_null_kind(base_trace)
    s_kind, _ = classify_null_kind(sta_trace)
    assert b_kind == s_kind == NullKind.HONEST_NULL.value


def test_stasis_never_lowers_sufficiency_or_classes():
    """A case that admits evidence in round 1: stasis must not fire before
    sufficiency is checked, and admitted bodies/classes are unchanged."""
    routes = {"/doc": GDELT_RELEVANT, "/api/v2/studies": CT_RELEVANT}
    base_trace, _, base_state = _run(
        _q("Will Apple report quarterly results above consensus?"),
        "news coverage of events", routes, stasis=False)
    sta_trace, _, sta_state = _run(
        _q("Will Apple report quarterly results above consensus?"),
        "news coverage of events", routes, stasis=True)
    assert base_state == sta_state
    assert base_trace.stop_reason == sta_trace.stop_reason  # both sufficient
