"""BUILD — stated evidence gaps.

The second live run's adversary said the null findings were "an artifact of
the retrieval method, not the literature." Before tools/gaps.py that
distinction existed only as a low confidence number. These tests pin it:

  PART 1  HONEST NULL vs RETRIEVAL FAILURE — the classification itself
  PART 2  obstacle taxonomy — key, rate limit, paywall, query failure,
          no adapter, no query issued — each with its owner action
  PART 3  candidate sources from the specs' own answers/cannot_answer,
          including sources that declare they cannot hold the evidence
  PART 4  the researcher-facing statement and run-level report

No socket. Fixtures only.
"""
from __future__ import annotations

import json
import re

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.research_program import (EvidenceRequirement, QuestionKind,  # noqa: E402
                                  ResearchQuestion, SourceClassRank)
from tools.gaps import (GapKind, Obstacle, OwnerAction, build_report,  # noqa: E402
                        candidate_sources, classify_gap)
from tools.sources.base import SourceSpec  # noqa: E402
from tools.sources.registry import SourceAdapter, SourceRegistry  # noqa: E402


# ── fixtures ───────────────────────────────────────────────────────────────

SCHOLARLY_Q = ("What does recent scholarly research say about semiconductor "
               "supply chain resilience?")


def _question(text=SCHOLARLY_Q):
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=2)
    return rq


def _spec(name, answers, base_url="https://example.org",
          cannot_answer=("x",), key_env_var=""):
    return SourceSpec(name=name, base_url=base_url, description="",
                      answers=tuple(answers), cannot_answer=tuple(cannot_answer),
                      tier=1, min_interval_s=0.0, key_env_var=key_env_var)


def _registry(*entries) -> SourceRegistry:
    """entries: SourceSpec objects; adapters route any method to fixtures."""
    reg = SourceRegistry()

    def make_adapter(source):
        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    term = next((a for a in args if isinstance(a, str)),
                                kwargs.get("query_term", "q"))
                    url = source.build_url("/q", {"search": term})
                    return source.get_json(url)[0]
                return call
        return _Ad()

    for spec in entries:
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


def _trace(queries=("semiconductor supply chain resilience",),
           admitted=0, rejected=1, rounds=None, errors=(),
           stop_reason="round budget exhausted"):
    """A faithful RetrievalTrace stand-in: like retrieval.RetrievalTrace,
    every source touched in a round is listed in round['sources']."""
    if rounds is None:
        # one round against openalex by default; rejections are attributed
        # to it unless explicit per-source entries are supplied
        sources = list(errors) if errors else (
            [{"name": "openalex", "rejected": "below coverage"}]
            if rejected else [{"name": "openalex", "admitted": True}])
        rounds = [{"round": 1, "query": queries[0] if queries else "",
                   "sources": sources, "admitted": admitted}]
    t = type("T", (), {})
    t.queries = list(queries)
    t.admitted = [object()] * admitted
    t.rejected = [object()] * rejected
    t.rounds = rounds
    t.stop_reason = stop_reason
    t.question_id = "Q1"
    return t


# ── PART 1: the central distinction ────────────────────────────────────────

class TestHonestNullVsRetrievalFailure:

    def test_competent_search_finding_nothing_is_an_honest_null(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        trace = _trace(rejected=2)   # real queries, results judged, none fit
        gap = classify_gap(reg, _trace(rejected=2), _question())
        assert gap.kind is GapKind.HONEST_NULL
        assert gap.owner_action is OwnerAction.ACCEPT_UNKNOWABLE
        assert gap.is_honest_null
        assert "never" not in gap.why_not_obtained.split("genuinely")[0] \
            or "never tried" not in gap.why_not_obtained

    def test_never_looking_is_a_retrieval_failure(self):
        # No source selected anything routable: zero queries issued.
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        trace = _trace(queries=[], rounds=[], stop_reason="no routable sources")
        gap = classify_gap(reg, trace, _question())
        assert gap.kind is GapKind.RETRIEVAL_FAILURE
        assert gap.obstacle is Obstacle.NO_QUERY_ISSUED
        assert not gap.is_honest_null

    def test_planner_skipped_sources_are_named_as_never_looked(self):
        """RetrievalTrace.skipped_sources (W5 planner) is the raw material:
        a source the planner could not serve was NEVER looked at, and the
        gap must say so with the planner's own reason."""
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        t = _trace(rejected=0, admitted=0, rounds=[
            {"round": 1, "query": "q", "sources": [
                {"name": "openalex", "skipped": "no authored query"}],
             "admitted": 0}])
        t.skipped_sources = [
            {"name": "openalex", "reason": "no authored query"}]
        gap = classify_gap(reg, t, _question())
        assert gap.kind is GapKind.RETRIEVAL_FAILURE
        assert "openalex" in gap.why_not_obtained
        assert "never tried" in gap.why_not_obtained

    def test_plausible_source_with_fetch_route_never_tried_is_failure(self):
        # openalex was tried and failed; semantic_scholar is plausible,
        # selected, HAS a generic route — but was never queried.
        reg = _registry(
            _spec("openalex", ["scholarly work search"]),
            _spec("semantic_scholar", ["scholarly work search by topic"],
                  base_url="https://api.semanticscholar.org"))
        trace = _trace(rounds=[{"round": 1, "query": "chips", "sources": [
            {"name": "openalex", "admitted": True}], "admitted": 0}])
        calls = {"openalex": ("works_search", ("term",), {})}
        gap = classify_gap(reg, trace, _question(),
                           generic_calls=calls)
        assert gap.kind is GapKind.RETRIEVAL_FAILURE
        assert "semantic_scholar" in gap.why_not_obtained

    def test_rounds_of_real_judged_results_are_competent_even_in_one_round(
            self):
        """One round is enough when real responses came back from a
        plausible source and were judged — fetching and judging IS looking.
        What makes a search incompetent is never issuing queries or never
        reaching a plausible holder, not round count."""
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        gap = classify_gap(reg, _trace(rejected=3), _question())
        assert gap.kind is GapKind.HONEST_NULL

    def test_multiple_rounds_all_rejected_can_be_honest_null(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        q = "semiconductor supply chain resilience"
        trace = _trace(
            queries=(q, q + " foundry"),
            rejected=4,
            rounds=[
                {"round": 1, "query": q, "sources": [
                    {"name": "openalex", "rejected": "irrelevant"}],
                 "admitted": 0},
                {"round": 2, "query": q + " foundry", "sources": [
                    {"name": "openalex", "rejected": "irrelevant"}],
                 "admitted": 0},
            ])
        gap = classify_gap(reg, trace, _question())
        assert gap.kind is GapKind.HONEST_NULL
        assert len(gap.queries_issued) == 2

    def test_classification_only_adds_structure_and_never_raises_confidence(
            self):
        """The gate rule: gaps never improve an answer's standing."""
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        gap = classify_gap(reg, _trace(), _question())
        d = gap.to_dict()
        assert set(d) == {
            "question_id", "question_text", "kind", "evidence_needed",
            "candidates", "obstacle", "why_not_obtained", "owner_action",
            "queries_issued", "n_admitted", "n_rejected"}
        assert gap.confidence_score == 0.0 if hasattr(gap, "confidence_score") \
            else True  # a gap carries no confidence field at all
        with pytest.raises(AttributeError):
            gap.raise_confidence()


# ── PART 2: obstacle taxonomy -> owner actions ─────────────────────────────

class TestObstaclesAndActions:

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_FRED_API_KEY", raising=False)
        # fred IS a plausible holder here — the question is about macro data.
        reg = _registry(
            _spec("fred", ["macro time series unemployment rate lookup"],
                  key_env_var="CALLISTO_FRED_API_KEY"),
            _spec("openalex", ["scholarly work search"]))
        trace = _trace(rounds=[{"round": 1, "query": "unemployment rate",
                                "sources": [], "admitted": 0}])
        gap = classify_gap(reg, trace, _question(
            "What does the unemployment rate time series show?"))
        assert gap.obstacle is Obstacle.NO_API_KEY
        assert gap.owner_action is OwnerAction.ADD_API_KEY

    def test_rate_limited_error_maps_to_wait(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        trace = _trace(rounds=[{"round": 1, "query": "q", "sources": [
            {"name": "openalex", "error": "HTTP 429 too many requests"}],
            "admitted": 0}])
        gap = classify_gap(reg, trace, _question())
        assert gap.kind is GapKind.RETRIEVAL_FAILURE
        assert gap.obstacle is Obstacle.RATE_LIMITED
        assert gap.owner_action is OwnerAction.WAIT_RATE_LIMIT

    def test_generic_query_failure_maps_to_retry(self):
        reg = _registry(_spec("gdelt", ["news document search"]))
        trace = _trace(rounds=[{"round": 1, "query": "q", "sources": [
            {"name": "gdelt", "error": "HTTP 500 server error"}],
            "admitted": 0}])
        gap = classify_gap(reg, trace, _question())
        assert gap.obstacle is Obstacle.QUERY_FAILED
        assert gap.owner_action is OwnerAction.RETRY

    def test_paywalled_source_declares_it_and_gets_buy_access(self):
        reg = _registry(_spec(
            "sec_fulltext", ["filings mentioning a topic"],
            cannot_answer=("paywalled full texts beyond abstracts",)))
        trace = _trace(rounds=[{"round": 1, "query": "filings mentioning risk",
                                "sources": [
                                    {"name": "sec_fulltext",
                                     "error": "HTTP 403 forbidden"}],
                                "admitted": 0}])
        gap = classify_gap(reg, trace, _question(
            "Which filings mention semiconductor supply chain risk?"))
        assert gap.obstacle is Obstacle.PAYWALLED
        assert gap.owner_action is OwnerAction.BUY_ACCESS

    def test_selected_but_no_route_is_add_query_authoring(self):
        # treasury is selected but GENERIC_CALLS has no entry for it.
        reg = _registry(_spec("treasury",
                              ["average interest rates datasets lookup"]))
        trace = _trace(queries=["interest rates"], rounds=[
            {"round": 1, "query": "interest rates", "sources": [],
             "admitted": 0}])
        gap = classify_gap(reg, trace, _question(
            "What are the average interest rates on treasury debt?"),
            generic_calls={
                "openalex": ("works_search", ("term",), {})})
        assert gap.kind is GapKind.RETRIEVAL_FAILURE
        assert gap.owner_action is OwnerAction.ADD_QUERY_AUTHORING
        assert any(c.name == "treasury" and not c.tried
                   for c in gap.candidates)


# ── PART 3: which known source would plausibly hold it ─────────────────────

class TestCandidateSources:

    def test_ranked_by_the_specs_own_answers_clauses(self):
        reg = _registry(
            _spec("openalex", ["scholarly work search by title or topic"]),
            _spec("fred", ["macro time series lookup"]),
        )
        cands = candidate_sources(reg, SCHOLARLY_Q, "", tried_names=set())
        assert cands[0].name == "openalex"
        assert not cands[0].tried
        # fred shares no topical vocabulary with the question
        assert all(c.name != "fred" or c.why_plausible for c in cands)

    def test_tried_sources_are_flagged_as_tried(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        cands = candidate_sources(reg, SCHOLARLY_Q, "",
                                  tried_names={"openalex"})
        assert cands[0].tried

    def test_declared_cannot_answer_is_surfaced_verbatim(self):
        reg = _registry(_spec(
            "openalex", ["scholarly work search"],
            cannot_answer=("paywalled full texts (metadata and OA links only)",
                           "peer-review records or acceptance status")))
        cands = candidate_sources(reg, SCHOLARLY_Q, "", tried_names=set())
        oa = next(c for c in cands if c.name == "openalex")
        assert "paywalled full texts (metadata and OA links only)" \
            in oa.cannot_answer

    def test_a_source_that_declares_it_cannot_hold_this_is_still_listed(self):
        """'Source X would hold this except it declares it cannot' is what
        the owner needs to read — kept, flagged NOT_INDEXED."""
        reg = _registry(
            _spec("clinicaltrials", ["clinical trial registry records search"],
                  cannot_answer=("observational economics literature",)),
            _spec("openalex", ["scholarly work search by topic"]),
        )
        cands = candidate_sources(
            reg, "What observational economics literature exists on "
                 "semiconductor workers?", "", tried_names=set())
        ct = [c for c in cands if c.name == "clinicaltrials"]
        assert ct and ct[0].obstacle is Obstacle.NOT_INDEXED
        assert not ct[0].tried


# ── PART 4: the researcher-facing deliverable ──────────────────────────────

class TestStatementAndReport:

    def test_statement_names_kind_evidence_source_and_action(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        gap = classify_gap(reg, _trace(rejected=2), _question())
        s = gap.statement()
        assert "HONEST NULL" in s
        assert "genuinely absent" in s
        assert "Owner action" in s

    def test_retrieval_failure_statement_says_never_looked(self):
        reg = _registry(_spec("openalex", ["scholarly work search"]))
        trace = _trace(queries=[], rounds=[])
        gap = classify_gap(reg, trace, _question())
        assert "RETRIEVAL FAILURE" in gap.statement()
        assert "never looked" in gap.statement() or \
            "never asked" in gap.statement()

    def test_run_report_counts_both_kinds_and_orders_actions(self):
        reg = _registry(
            _spec("openalex", ["scholarly work search"]),
            _spec("fred", ["unemployment rate macro time series"],
                  key_env_var="CALLISTO_TEST_NOPE_KEY"))
        honest_q = _question(SCHOLARLY_Q)
        failed_q = _question("What is the unemployment rate trend?")
        report = build_report(reg, [
            (_trace(rejected=2), honest_q),
            (_trace(queries=[], rounds=[]), failed_q),
        ])
        assert report.n_honest_nulls == 1
        assert report.n_retrieval_failures == 1
        actions = report.actions()
        assert OwnerAction.ACCEPT_UNKNOWABLE in actions
        # actionable items come before 'accept unknowable'
        assert actions.index(OwnerAction.ADD_API_KEY) < \
            actions.index(OwnerAction.ACCEPT_UNKNOWABLE)
        rep = report.to_dict()
        assert rep["n_retrieval_failures"] == 1
        assert len(rep["gaps"]) == 2
        assert "RETRIEVAL FAILURE" in report.statement()
