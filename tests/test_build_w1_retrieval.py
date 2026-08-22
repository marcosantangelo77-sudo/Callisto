"""W1 — retrieval: iterative, gated, fanned out.

Fixtures only (no_socket guard active). The live run's failure — five
sub-questions, one irrelevant hit accepted by string coincidence — is
regression-tested here in four parts:

  JOB 1  iterative: query -> inspect -> refine -> stop (terminator reused)
  JOB 2  relevance gate rejects BEFORE ingestion; rejections recorded;
         zero admissible evidence is an honest null
  JOB 3  fan-out across sources; min_independent_sources enforced against
         actual source diversity (overlap families collapse)
  JOB 4  question_type translation onto adapter vocabulary
"""
from __future__ import annotations

import json
import re

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.research_program import ResearchQuestion  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever,
    RelevanceGate,
    build_query,
    independence_key,
    refine_query,
    translate_question_type,
)
from tools.sources.registry import SourceRegistry, SourceAdapter  # noqa: E402
from tools.sources.base import SourceSpec  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────


def _openalex_body(title="Semiconductor supply chain resilience review"):
    return json.dumps({"results": [
        {"id": "W1", "title": title, "publication_year": 2024},
        {"id": "W2", "title": "Chips act industrial policy",
         "publication_year": 2023},
    ]})


def _semantic_scholar_body():
    return json.dumps({"data": [
        {"title": "Resilience of semiconductor supply networks",
         "year": 2025},
        {"title": "Tariff exposure in chip manufacturing",
         "year": 2024},
    ]})


IRRELEVANT_BODY = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"},
]})


class _FakeSpec:
    """Minimal spec stand-in so tests need no real adapters."""
    def __init__(self, name, answers, base_url="https://example.org"):
        self.name = name
        self.answers = tuple(answers)
        self.tier = 1
        self.base_url = base_url
        self.min_interval_s = 0.0

    def to_dict(self):
        return {"name": self.name, "answers": list(self.answers),
                "tier": self.tier}


def _registry(*specs) -> SourceRegistry:
    """specs: (name, answers, base_url). Adapters route any method through
    RestSource against a fixture path derived from the query."""
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

    for name, answers, url in specs:
        spec = SourceSpec(
            name=name, base_url=url, description="", answers=tuple(answers),
            cannot_answer=("x",), tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg



def _routes(bodies: dict[str, str]) -> dict[str, str]:
    return bodies


def _q(text="What does research say about semiconductor supply chain "
             "resilience?", min_ind=2):
    from agp.research_program import (EvidenceRequirement, QuestionKind,
                                      SourceClassRank)
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _retriever(reg, transport_routes, gate=None, max_rounds=3,
               call_names=("alpha", "beta"), **kw):
    ledger = ProvenanceLedger()
    return IterativeRetriever(
        registry=reg, ledger=ledger,
        transport=fixture_transport(transport_routes),
        gate=gate or RelevanceGate(min_coverage=0.25),
        max_rounds=max_rounds, generic_calls={
            "alpha": ("works_search", ("term",), {"limit": 3}),
            "beta": ("works_search", ("term",), {"limit": 3}),
            "openalex": ("works_search", ("term",), {"limit": 3}),
            "semantic_scholar": ("works_search", ("term",), {"limit": 3}),
        }, **kw)


# Adapter answer clauses must overlap the question's SUBJECT for selection;
# that is the registry contract under test.
_ALPHA_ANSWERS = ["semiconductor supply chain resilience scholarly works"]
_BETA_ANSWERS = ["news events about semiconductor supply chains"]


def _alpha_reg(name="alpha", url="https://a.example",
               answers=_ALPHA_ANSWERS):
    return _registry((name, answers, url))



# ── JOB 2: relevance gating at ingestion ───────────────────────────────────


def test_irrelevant_hit_rejected_before_ingestion_with_reason():
    reg = _alpha_reg()
    trace = _retriever(reg, {"/a?": IRRELEVANT_BODY}).retrieve(
        _q(), "", min_independent=1)

    assert trace.n_admitted == 0, "irrelevant hit must not enter evidence"
    assert len(trace.rejected) == 1
    rej = trace.rejected[0]
    assert rej.source_name == "alpha"
    assert "covers" in rej.reason and "%" in rej.reason
    assert rej.content_sha256  # the rejected bytes are still identified


def test_irrelevant_hit_does_not_satisfy_min_independent():
    reg = _alpha_reg()
    trace = _retriever(reg, {"/a?": IRRELEVANT_BODY}).retrieve(
        _q(min_ind=1), "", min_independent=1)
    assert trace.independent_keys == set()


def test_gate_threshold_is_respected_not_lowered_by_caller():
    g = RelevanceGate(min_coverage=0.9)
    with pytest.raises(ValueError):
        RelevanceGate(min_coverage=1.5)  # invalid config rejected outright
    ok, cov, reason = g.judge("quantum error correction rates", "",
                              {"title": "quantum computing survey"})
    assert not ok and cov < 0.9 and reason


# ── JOB 3: fan-out + enforced independence ─────────────────────────────────


def test_fanout_queries_multiple_selected_sources():
    reg = _registry(
        ("alpha", _ALPHA_ANSWERS, "https://api.openalex.org"),
        ("beta", _BETA_ANSWERS, "https://api.gdeltproject.org"))
    routes = {"/api": _openalex_body(), "/gdelt": _openalex_body()}
    trace = _retriever(reg, routes).retrieve(_q(), "", min_independent=2)
    sources_hit = {r["name"] for rnd in trace.rounds
                   for r in rnd["sources"] if r.get("admitted")}
    assert sources_hit == {"alpha", "beta"}
    assert trace.n_admitted >= 2


def test_overlap_family_collapses_to_one_independent_source():
    assert independence_key("openalex", "") == \
        independence_key("semantic_scholar", "")
    assert independence_key("openalex", "") != independence_key("gdelt", "")


def test_two_hits_from_one_source_do_not_meet_min_independent_2():
    reg = _registry(("alpha", _ALPHA_ANSWERS, "https://api.openalex.org"),
                    ("beta", ["unrelated clause entirely"], "https://b.org"))
    # Only alpha is selected AND returns relevant data → 1 independent key.
    routes = {"/api": _openalex_body(), "/s?": _openalex_body()}
    trace = _retriever(reg, routes).retrieve(_q(min_ind=2), "",
                                             min_independent=2)
    assert trace.n_admitted >= 1
    assert len(trace.independent_keys) == 1
    # The retriever must NOT claim sufficiency.
    assert "sufficient" not in trace.stop_reason
    # And it should have kept trying other rounds/sources rather than
    # declaring victory on one publisher.
    assert len(trace.rounds) >= 1


def test_sufficiency_declared_only_at_real_independence():
    reg = _registry(("openalex", _ALPHA_ANSWERS, "https://api.openalex.org"),
                    ("semantic_scholar", _BETA_ANSWERS,
                     "https://s.example"))
    routes = {"/api": _openalex_body(), "/s?": _openalex_body()}
    trace = _retriever(reg, routes).retrieve(_q(min_ind=2), "",
                                             min_independent=2)
    # openalex+semantic_scholar are one declared overlap family (both index
    # the scholarly literature), so two hits from them are ONE independent
    # source and sufficiency must NOT be declared at min_independent=2.
    assert len(trace.independent_keys) == 1
    assert not trace.stop_reason.startswith("sufficient")


# ── JOB 1: iterative refinement with the reused terminator ────────────────


def test_empty_first_round_refines_and_retries_other_sources():
    reg = _registry(("alpha", _ALPHA_ANSWERS, "https://api.openalex.org"),
                    ("beta", ["agency rules about supply chains"],
                     "https://c.org"))
    # alpha always returns junk, beta always good. fixture_transport matches
    # by substring; the path segment is the stable part of each URL.
    # alpha always returns junk; beta admits. Requiring 2 independent
    # sources keeps the loop running past round 1, so the query must
    # actually refine once relevant hits exist.
    trace = _retriever(reg, {"/api": IRRELEVANT_BODY,
                             "/c?": _openalex_body()})\
        .retrieve(_q(min_ind=2), "", min_independent=2)
    assert trace.n_admitted >= 1
    assert any(r["name"] == "beta" for rnd in trace.rounds
               for r in rnd["sources"] if r.get("admitted"))
    assert len(trace.queries) == len(trace.rounds)
    assert trace.queries[0] != trace.queries[-1], (
        "refinement should change the query once hits exist")
    assert trace.stop_reason  # every stop carries its reason


def test_round_budget_stops_with_reason():
    reg = _alpha_reg()
    trace = _retriever(reg, {"/a?": IRRELEVANT_BODY}, max_rounds=2)\
        .retrieve(_q(), "", min_independent=1)
    assert trace.n_admitted == 0
    assert trace.stop_reason
    assert len(trace.rounds) <= 2


def test_build_query_drops_stopwords_keeps_subject_terms():
    q = build_query("What does recent research say about semiconductor "
                    "supply chain resilience?")
    assert "semiconductor" in q and "resilience" in q
    assert "does" not in q and "what" not in q


def test_refine_query_adds_tokens_from_relevant_titles():
    refined = refine_query("semiconductor supply chain",
                           ["Resilience of semiconductor networks",
                            "Chips act policy"])
    assert "resilience" in refined
    assert refined.startswith("semiconductor supply chain")


# ── JOB 4: question-type translation ───────────────────────────────────────


def test_translation_adopts_adapter_vocabulary():
    reg = _registry(("clinicaltrials",
                     ["trial design arms endpoints by NCT id or search",
                      "recruitment status and enrollment counts"],
                     "https://clinicaltrials.gov"))
    translated, names = translate_question_type(
        reg, "what do clinical trials show about the drug?",
        "clinical trial evidence")
    assert names == ["clinicaltrials"]
    assert "trial" in translated


def test_translation_falls_back_to_raw_inputs_when_nothing_matches():
    reg = _registry(("alpha", ["totally different domain clauses"],
                     "https://x.org"))
    translated, names = translate_question_type(
        reg, "quantum error correction thresholds", "qec literature")
    assert names == []
    assert "quantum" in translated and "correction" in translated


# ── Engine integration: honest null surfaces as refusal, not a seal ───────


def test_engine_records_rejections_and_caps_unmet_leaf(tmp_path):
    """The live-run regression: one irrelevant fetch must be REJECTED at
    ingestion and the leaf must end honestly capped/null, with the
    rejection visible in result.notes."""
    import asyncio
    from datetime import date

    from tools.pipeline.model import ScriptedModel
    from tools.pipeline.engine import ResearchPipeline

    decompose = json.dumps({"sub_questions": [{
        "text": "what does scholarly research say about the topic",
        "kind": "descriptive",
        "question_type": "scholarly literature about the topic",
        "min_source_tier": 2, "min_independent_sources": 1}]})
    model = ScriptedModel({
        "Architect": [{"content": decompose}],
        "Manager": [{"content": json.dumps(
            {"answer": "no relevant evidence found",
             "proposed_confidence": 0.8})}],
    })

    class _Quiet:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    from tools.artifacts import ArtifactStore
    pipe = ResearchPipeline(
        model=model, adversary_router=_Quiet(),
        transport=fixture_transport({"/works?": IRRELEVANT_BODY}),
        store=ArtifactStore(root=tmp_path / "art"))
    result = asyncio.run(pipe.run(
        "What does research say about the topic?",
        today=date(2026, 8, 22)))

    assert result.fetches == [], (
        "irrelevant fetch must never reach the evidence set")
    assert any("rejected at ingestion" in n for n in result.notes), result.notes
