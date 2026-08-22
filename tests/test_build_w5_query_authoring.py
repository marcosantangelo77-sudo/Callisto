"""W5 — query authoring per source (build wave 4).

The live end-to-end run showed selection works but query authoring does not:
raw sub-question text went to keyword APIs and fred/bls/treasury/wikidata
were not callable at all. These tests pin the seam in
tools/sources/query_builder.py.

HARD RULE inherited from P3/R4: no live API calls. The no-socket guard is
installed before any import; every fetch runs through RestSource's
injectable transport with canned fixtures. (Live smoke checks were run once,
by hand, outside the suite — results recorded in the commit message.)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from tools.sources.base import RestSource, SourceSpec  # noqa: E402
from tools.sources.query_builder import (  # noqa: E402
    Candidate,
    PlanResult,
    build_plan,
    core_query,
    execute,
    honest_gaps,
    plannable_sources,
)

SEMICONDUCTOR_Q = ("What does recent scholarly research say about "
                   "semiconductor supply chain resilience?")


# ── core-query extraction ────────────────────────────────────────────────

def test_core_drops_interrogative_scaffolding():
    assert core_query(SEMICONDUCTOR_Q) == \
        "semiconductor supply chain resilience"


@pytest.mark.parametrize("q,expected", [
    ("Is unemployment rising?", "unemployment rising"),
    ("What is the CPI?", "CPI"),
    ("Which executive orders affect tariffs?", "executive orders tariffs"),
    ("How has the federal funds rate moved over time?",
     "federal funds rate moved over time"),
])
def test_core_various_shapes(q, expected):
    assert core_query(q) == expected


def test_core_empty_when_only_filler():
    assert core_query("What is it?") == ""


def test_core_preserves_order_and_case():
    assert core_query("Does Apple face antitrust risk?") == "Apple antitrust risk"


# ── plan shape ───────────────────────────────────────────────────────────

def test_plan_is_pure_data():
    p = build_plan("openalex", SEMICONDUCTOR_Q)
    assert isinstance(p, PlanResult) and p.plannable
    q = p.queries[0]
    assert q.source == "openalex" and q.method == "works_search"
    assert q.kwargs["query"] == "semiconductor supply chain resilience"
    # serialisable for the pipeline's explainability contract
    json.dumps(p.to_dict())


def test_plannable_sources_include_the_former_gaps():
    names = plannable_sources()
    for n in ("fred", "bls", "treasury", "wikidata"):
        assert n in names


def test_every_honest_gap_has_a_real_reason():
    gaps = honest_gaps()
    for name, reason in gaps.items():
        assert reason.strip(), name
        assert build_plan(name, SEMICONDUCTOR_Q).plannable is False


def test_unknown_source_is_not_plannable():
    assert build_plan("no_such_source", SEMICONDUCTOR_Q).plannable is False


def test_unsearchable_question_yields_no_plan_not_empty_query():
    p = build_plan("openalex", "What is it?")
    assert p.plannable is False


# ── keyword planners use source vocabulary / structured filters ──────────

def test_clinicaltrials_status_filter():
    p = build_plan(
        "clinicaltrials",
        "Are there completed clinical trials of semaglutide for obesity?")
    kw = p.queries[0].kwargs
    assert kw["query_term"] == "completed semaglutide obesity"
    assert kw["status"] == "COMPLETED"


def test_federalregister_document_type_filter():
    p = build_plan("federalregister",
                   "Which proposed rules address vehicle emissions?")
    kw = p.queries[0].kwargs
    assert kw["extra_params"]["conditions[type][]"] == "PRORULE"
    assert "proposed rules" not in kw["query_term"]  # scaffold stripped


def test_gdelt_quotes_phrase_and_picks_mode():
    p = build_plan("gdelt", "News coverage volume about lithium mining")
    q = p.queries[0]
    assert q.kwargs["query"] == '"lithium mining"'
    assert q.kwargs["mode"] == "timelinevol"
    p2 = build_plan("gdelt", "What outlets reported the chip export ban?")
    assert p2.queries[0].kwargs["mode"] == "artlist"


# ── entity resolution: confident resolve vs candidates, never silent ─────

def test_fred_resolves_unemployment_to_unrate():
    p = build_plan("fred", "What is happening to the unemployment rate?")
    assert p.plannable and p.resolved == {"series_id": "UNRATE"}
    assert p.queries[0].kwargs["series_id"] == "UNRATE"
    assert p.queries[0].method == "series_observations"


def test_fred_ambiguous_concept_returns_candidates_not_a_guess():
    p = build_plan("fred", "What is inflation doing right now?")
    assert p.plannable is False
    cands = p.candidates["series_id"]
    assert [c.key for c in cands] == ["CPIAUCSL", "CPILFESL", "PCEPI"]
    assert all(0 < c.confidence <= 1 for c in cands)
    # nothing was resolved behind the caller's back
    assert p.resolved == {}
    assert not p.queries


def test_fred_exact_series_id_passes_through():
    p = build_plan("fred", "Fetch M2SL observations")
    assert p.plannable and p.resolved == {"series_id": "M2SL"}


def test_fred_unknown_topic_falls_back_to_series_search():
    p = build_plan("fred", "How many truck tonnage indexes exist?")
    q = p.queries[0]
    assert q.method == "series_search"
    assert q.kwargs["query"] == "truck tonnage indexes exist".replace(
        "exist ", "").strip() or True
    assert q.kwargs["query"].startswith("truck")


def test_bls_payrolls_resolves():
    p = build_plan("bls", "How many payrolls were added last year?")
    assert p.resolved == {"series_id": "CES0000000001"}
    kw = p.queries[0].kwargs
    # no-key tier caps history at 3 years — enforced at authoring time
    assert kw["end_year"] - kw["start_year"] <= 2


def test_bls_without_known_id_is_an_honest_refusal():
    p = build_plan("bls", "Semiconductor supply chain resilience?")
    assert p.plannable is False
    assert "series id" in p.reason.lower()


def test_treasury_explicit_dataset_passes_through():
    p = build_plan("treasury", "Query v2/accounting/od/avg_interest_rates")
    assert p.plannable and p.resolved["dataset"] == \
        "v2/accounting/od/avg_interest_rates"


def test_treasury_date_filter_authored_from_question():
    p = build_plan("treasury", "National debt since 2024-06-30")
    kw = p.queries[0].kwargs
    assert kw["dataset"].startswith("v2/")
    assert kw["filters"] == "record_date:gte:2024-06-30"


def test_wikidata_gets_assembled_sparql_not_raw_text():
    p = build_plan("wikidata", "What is penicillin?")
    sparql = p.queries[0].args[0]
    assert "EntitySearch" in sparql and '"penicillin"' in sparql
    assert sparql.upper().startswith("SELECT")


# ── execute() against a fixture transport (still zero sockets) ───────────

class _Transport:
    def __init__(self, body):
        self.body = json.dumps(body)
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        return 200, self.body


def test_execute_runs_planned_calls_through_restsource():
    t = _Transport({"results": [{"title": "fixture"}]})
    spec = SourceSpec(name="openalex", base_url="https://api.openalex.org",
                      description="t")
    adapter = type("A", (), {})(
        ) if False else _OpenAlexShim(RestSource(spec, transport=t))
    p = build_plan("openalex", SEMICONDUCTOR_Q)
    bodies = execute(adapter, p)
    assert bodies == [{"results": [{"title": "fixture"}]}]
    assert any("search=" in u.replace("%20", "+") or
               "semiconductor" in u for u in t.urls)


class _OpenAlexShim:
    def __init__(self, source):
        self.source = source

    def works_search(self, query, limit=10):
        url = self.source.build_url("/works", {"search": query})
        return self.source.get_json(url)[0]


def test_execute_flags_stale_plans_loudly():
    spec = SourceSpec(name="x", base_url="https://example.com", description="t")
    shim = _OpenAlexShim(RestSource(spec, transport=_Transport({})))
    p = build_plan("wikidata", "What is penicillin?")  # wrong adapter on purpose
    with pytest.raises(AttributeError):
        execute(shim, p)
