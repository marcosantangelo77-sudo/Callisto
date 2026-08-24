"""Expected information gain gating — adaptive retrieval.

PLAN-THEN-FETCH built queries and executed them; nothing asked which fetch
could most reduce uncertainty. This module adds the smallest viable
expected-gain test BEFORE each round N+1 fetch:

  - what is unknown = EvidenceRequirement.unmet_reasons over the trace
  - could this candidate's SUCCESS satisfy any unmet requirement?
      no  -> skip the call entirely (recorded as a gain-skip)
  - a duplicate independent voice cannot satisfy an independence
    shortfall, whatever its content — that call is never worth making.
  - a source whose declared cannot_answer covers the question cannot
    produce admissible evidence for it at all.

Nothing here raises confidence or invents evidence: the gate only stops
SPENDING budget, like the terminator it sits beside.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever,
    RelevanceGate,
    estimate_gain,
    independence_key,
)
from tools.sources.base import SourceSpec  # noqa: E402
from tools.sources.registry import SourceRegistry, SourceAdapter  # noqa: E402


GOOD_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2024},
]})
IRRELEVANT_BODY = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})


def _spec(name, answers=("semiconductor supply chain resilience scholarly "
                         "works",), base_url="https://a.example",
          cannot_answer=()):
    return SourceSpec(
        name=name, base_url=base_url, description="",
        answers=tuple(answers),
        cannot_answer=tuple(cannot_answer) or None,
        tier=1, min_interval_s=0.0)


def _registry(*specs):
    reg = SourceRegistry()

    def make_adapter(source):
        path = "/" + re.sub(r"^https?://|[^a-z0-9].*$", "",
                            source.spec.base_url.lower()) or "works"

        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    term = next((a for a in args if isinstance(a, str)),
                                kwargs.get("query_term", "q"))
                    url = source.build_url(
                        path, {"search": term.replace(" ", "+")})
                    return source.get_json(url)[0]
                return call
        return _Ad()

    for spec in specs:
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


def _q(min_ind=2):
    rq = ResearchQuestion(
        text="What does research say about semiconductor supply chain "
             "resilience?", kind="descriptive")
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _retriever(reg, routes, max_rounds=3, adaptive_gain=True):
    return IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=fixture_transport(routes),
        gate=RelevanceGate(min_coverage=0.25),
        max_rounds=max_rounds, adaptive_gain=adaptive_gain,
        generic_calls={
            s.name: ("works_search", ("term",), {"limit": 3})
            for s in specs_iter(reg)})


def specs_iter(reg):
    return [reg.get(n).spec for n in reg.names()]


# ── estimate_gain unit behaviour ───────────────────────────────────────────


def test_duplicate_voice_cannot_address_independence_shortfall():
    reqs = EvidenceRequirement(min_independent_sources=2)
    keys = {independence_key("openalex", "https://api.openalex.org")}
    est = estimate_gain(
        _spec("semanticscholar",
              base_url="https://semanticscholar.example"),
        reqs, keys, "scholarly literature")
    assert est.duplicate_voice is True       # declared overlap family
    assert est.worth_the_call is False       # zero possible gain


def test_fresh_voice_is_worth_the_call():
    reqs = EvidenceRequirement(min_independent_sources=2)
    keys = {independence_key("openalex", "https://api.openalex.org")}
    est = estimate_gain(
        _spec("gdelt", answers=["news events about semiconductor "
                                "supply chains"],
              base_url="https://api.gdeltproject.example"),
        reqs, keys, "news events")
    assert est.worth_the_call is True


def test_declared_cannot_answer_source_is_skipped():
    reqs = EvidenceRequirement(min_independent_sources=2)
    est = estimate_gain(
        _spec("weather", answers=(),
              cannot_answer=("weather forecasts and climate data",)),
        reqs, set(), "weather forecast data")
    assert est.unsatisfiable
    assert est.worth_the_call is False


def _three_source_reg():
    return _registry(
        _spec("alpha", base_url="https://api.openalex.org"),
        _spec("semanticscholar", base_url="https://s.example"),
        _spec("gamma", answers=["agency rules about semiconductor "
                                "supply chains"],
              base_url="https://g.example"))


def test_second_round_duplicate_voice_is_skipped_not_fetched():
    """min_independent=3; round 1 admits alpha+semanticscholar (2 keys,
    one shared family). Round 2 re-offers both duplicates plus a fresh
    voice that keeps returning junk: the duplicates must be skipped
    BEFORE spend, and once only duplicates remain the loop stops with
    the no-gain reason instead of burning the round budget."""
    reg = _three_source_reg()
    routes = {"/api?": GOOD_BODY, "/s?": GOOD_BODY, "/g?": IRRELEVANT_BODY}
    tr = _retriever(reg, routes, max_rounds=3)\
        .retrieve(_q(min_ind=3), "", min_independent=3)
    fetched = {r["name"] for rnd in tr.rounds
               for r in rnd["sources"] if r.get("admitted")}
    assert "alpha" in fetched
    dup_skips = {g["source"] for g in tr.gain_skipped
                 if "duplicate" in g["reason"]}
    assert dup_skips, tr.gain_skipped
    # a duplicate-voice source is never fetched after its voice is counted
    for rnd in tr.rounds[1:]:
        for r in rnd["sources"]:
            if r["name"] in dup_skips:
                assert not r.get("admitted")
                assert not r.get("rejected")   # not fetched at all
    # every duplicate skip happened BEFORE spend: no fetch call was made
    # for those sources in any round after their voice was counted
    assert len(tr.rounds) < 3 or all(
        r["name"] == "gamma" for rnd in tr.rounds[1:]
        for r in rnd["sources"])


def test_adaptive_false_recovers_plan_then_fetch():
    """adaptive_gain=False must reproduce the old loop exactly: rounds run
    to budget even when they cannot change anything."""
    reg = _three_source_reg()
    routes = {"/api?": GOOD_BODY, "/s?": GOOD_BODY, "/g?": IRRELEVANT_BODY}
    tr = _retriever(reg, routes, max_rounds=3, adaptive_gain=False)\
        .retrieve(_q(min_ind=3), "", min_independent=3)
    assert tr.gain_skipped == []
    assert len(tr.rounds) >= 2   # kept spending despite zero possible gain


def test_gain_skips_recorded_on_trace_for_audit():
    reg = _three_source_reg()
    routes = {"/api?": GOOD_BODY, "/s?": GOOD_BODY, "/g?": IRRELEVANT_BODY}
    tr = _retriever(reg, routes).retrieve(_q(min_ind=3), "",
                                          min_independent=3)
    skipped = {g["source"] for g in tr.gain_skipped}
    assert "alpha" in skipped or "semanticscholar" in skipped
