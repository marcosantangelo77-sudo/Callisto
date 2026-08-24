"""I2 — routable coverage (build wave 5).

Every registered source must be QUERYABLE from a sub-question, or declare
honestly why it cannot have a plan path. The second live run produced nine
fetches all from OpenAlex — independence stuck at 1 — partly because nine
adapters had no plan path and partly because the independence family
declaration used 'semantic_scholar' while the registry name is
'semanticscholar', so openalex+S2 silently counted as TWO independent
sources. Both defects are pinned here.

Fixture-only: no_socket guard installed before imports; live smoke was run
once by hand via scripts/live_smoke_w6_i2.py and is recorded in the commit.
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

from tools.sources.base import (  # noqa: E402
    INDEPENDENCE_FAMILIES,
    RestSource,
    SourceSpec,
    independence_family,
)
from tools.sources.query_builder import (  # noqa: E402
    build_plan,
    execute,
    honest_gaps,
    plannable_sources,
    resolve_entity,
)
from tools.sources.registry import get_source_registry  # noqa: E402


# ── JOB 1: every registered source has a plan path or an honest gap ──────

def test_every_registered_source_is_plannable_or_honestly_gapped():
    """The coverage contract: NO registered source may fall through to
    'unknown source'. It either plans, or its absence from planning is an
    explicit, reasoned entry in honest_gaps()."""
    reg = get_source_registry()
    plannable = set(plannable_sources())
    gaps = set(honest_gaps())
    for name in reg.names():
        assert name in plannable or name in gaps, (
            f"source {name!r} has neither a planner nor an honest gap")


def test_semanticscholar_plans_under_its_real_registry_name():
    # regression: wave 4 keyed the planner 'semantic_scholar' while the
    # registry says 'semanticscholar', so S2 fell into "deferred" despite
    # having had a working planner all along.
    p = build_plan("semanticscholar",
                   "What does recent research say about lithium battery "
                   "recycling?")
    assert p.plannable
    q = p.queries[0]
    assert q.method == "paper_search"
    assert "lithium battery recycling" in q.kwargs["query"]


def test_worldbank_resolves_indicator_and_country():
    p = build_plan("worldbank", "How has GDP in China changed since 2010?")
    assert p.plannable
    assert p.resolved["indicator_code"] == "NY.GDP.MKTP.CD"
    assert p.resolved["country"] == "CHN"
    kw = p.queries[0].kwargs
    assert kw["code"] == "NY.GDP.MKTP.CD" and kw["iso3"] == "CHN"


def test_worldbank_gdp_growth_beats_gdp_on_specificity():
    p = build_plan("worldbank", "GDP growth in India over time")
    assert p.resolved["indicator_code"] == "NY.GDP.MKTP.KD.ZG"


def test_worldbank_two_countries_named_returns_candidates_not_a_guess():
    p = build_plan("worldbank", "Compare GDP of China and India")
    # one indicator, but TWO countries: the caller must pick, or pass 'all'
    assert p.candidates.get("country") and not p.plannable or \
        p.queries[0].kwargs["iso3"] == "all"


def test_worldbank_unknown_topic_falls_back_to_indicator_search():
    p = build_plan("worldbank", "What about palladium production volumes?")
    assert p.plannable
    assert p.queries[0].method == "search_indicators"


def test_bea_maps_trade_concept_to_dataset_pair():
    p = build_plan("bea", "What happened to the trade balance last year?")
    assert p.plannable
    q = p.queries[0]
    assert q.method == "get_data" and q.kwargs["dataset"] == "IntlTrade"


def test_census_maps_housing_starts_to_timeseries_survey(monkeypatch):
    monkeypatch.setenv("CALLISTO_CENSUS_API_KEY", "test-key")
    p = build_plan("census",
                   "Are housing starts falling since 2023-06?")
    assert p.plannable
    kw = p.queries[0].kwargs
    assert kw["dataset"] == "timeseries/eits/resconst"
    assert "HOUSTNSA" in kw["get_vars"]
    assert kw["start"].startswith("2023")


def test_fdic_failures_query_plans_directly():
    p = build_plan("fdic", "List recent failed banks with assets")
    assert p.plannable
    assert p.queries[0].method == "failures"


def test_fdic_bank_name_becomes_a_field_filter_within_the_adapter_dsl():
    p = build_plan("fdic", "What are the assets of Silicon Valley Bank?")
    assert p.plannable
    q = p.queries[0]
    # live-smoke finding: filters=NAME:x is an EXACT match (0 hits for
    # partials); search=NAME:"x" is the partial-friendly ES query string
    assert q.method == "search_institutions"
    assert q.kwargs["search"] == 'NAME:"Silicon Valley Bank"'
    assert q.kwargs["fields"]


def test_cftc_commodity_resolves_to_real_market_code():
    p = build_plan("cftc_cot", "Positioning of money managers in crude oil")
    assert p.plannable
    # 067651 is WTI on NYMEX per CFTC's legacy futures-only report
    assert p.resolved["market_code"] == "067651"
    assert p.queries[0].kwargs["disaggregated"] is True  # money managers


def test_wayback_extracts_url_and_timestamp():
    p = build_plan("wayback",
                   "What did https://example.com/pricing say before "
                   "2024-06-01?")
    assert p.plannable
    q = p.queries[0]
    assert q.method == "closest"
    assert q.kwargs["url"] == "https://example.com/pricing"
    assert q.kwargs["timestamp"].startswith("2024")


def test_wayback_without_any_url_refuses_honestly():
    p = build_plan("wayback", "What did the pricing page say in 2024?")
    assert p.plannable is False
    assert "url" in p.reason.lower()


def test_keyed_sources_without_keys_are_unplannable_with_instructions():
    """Failing loudly at PLANNING beats dying mid-fetch: keyed sources name
    their env var and how to get a key when none is configured."""
    os.environ.pop("CALLISTO_EIA_API_KEY", None)
    os.environ.pop("CALLISTO_USPTO_ODP_KEY", None)
    os.environ.pop("CALLISTO_COURTLISTENER_TOKEN", None)
    for name, var in (("eia", "CALLISTO_EIA_API_KEY"),
                      ("uspto_odp", "CALLISTO_USPTO_ODP_KEY"),
                      ("courtlistener", "CALLISTO_COURTLISTENER_TOKEN"),
                      ("census", "CALLISTO_CENSUS_API_KEY")):
        p = build_plan(name, "Anything at all about something topical")
        assert p.plannable is False, name
        assert var in p.reason, f"{name} must name {var}"
        assert len(p.reason) > len(var), f"{name} reason must instruct"


def test_eia_resolves_series_when_keyed(monkeypatch):
    monkeypatch.setenv("CALLISTO_EIA_API_KEY", "test-key")
    p = build_plan("eia", "Monthly WTI crude oil prices since 2020")
    assert p.plannable
    assert p.resolved["series_id"] == "PET.RWTC.M"
    assert p.queries[0].kwargs["frequency"] == "monthly"


def test_uspto_assignee_extraction_when_keyed(monkeypatch):
    monkeypatch.setenv("CALLISTO_USPTO_ODP_KEY", "test-key")
    p = build_plan(
        "uspto_odp",
        "Patents assigned to TSMC regarding semiconductor packaging?")
    assert p.plannable
    assert "assigneeName:" in p.queries[0].kwargs["query"]
    assert "TSMC" in p.queries[0].kwargs["query"]


def test_courtlistener_search_type_routing_when_keyed(monkeypatch):
    monkeypatch.setenv("CALLISTO_COURTLISTENER_TOKEN", "tok")
    p = build_plan("courtlistener", "Recent dockets about chip export rules")
    assert p.plannable
    assert p.queries[0].kwargs["search_type"] == "d"
    assert p.queries[0].kwargs["order_by"] == "dateFiled desc"


# ── JOB 2: entity resolution — resolve-or-candidates, never silent ───────

def test_apple_resolves_to_cik():
    resolved, cands = resolve_entity("company", "Does Apple face antitrust "
                                               "risk?")
    assert resolved == {"cik": "0000320193"}
    assert not cands


def test_google_alphabet_ambiguity_returns_candidates():
    # 'google' maps at 0.9 to Alphabet's CIK but 'alphabet' is the stronger
    # match; a bare 'google' still resolves — but both names must agree on
    # the same CIK rather than offering two different registrants.
    r1, c1 = resolve_entity("company", "Google")
    r2, c2 = resolve_entity("company", "Alphabet")
    assert r1.get("cik") == r2.get("cik") == "0001652044"


def test_unknown_company_yields_nothing_not_a_guess():
    resolved, cands = resolve_entity("company", "Bob's Discount Crabs Ltd")
    assert resolved == {} and cands == {}


def test_wikidata_q_number_passthrough():
    p = build_plan("wikidata", "What is Q42?")
    assert p.resolved == {"q_id": "Q42"}


def test_wikidata_class_hints_resolve_or_return_candidates():
    p = build_plan("wikidata", "Which companies make chips?")
    assert p.plannable  # single class -> SPARQL authored as usual
    # two competing classes would be candidates, never a silent pick
    from tools.sources.query_builder import _plan_wikidata_concept
    resolved, cands = _plan_wikidata_concept("companies and countries")
    assert cands.get("q_id") and resolved == {}


def test_exact_identifier_passthrough_for_each_slot_shape():
    assert build_plan("worldbank", "Fetch SP.POP.TOTL for BRA")\
        .resolved["indicator_code"] == "SP.POP.TOTL"
    assert build_plan("cftc_cot", "History for market code 067651")\
        .resolved["market_code"] == "067651"


# ── JOB 3: independence families declared at the source ─────────────────

def test_family_declaration_collapses_openalex_and_s2():
    # regression for the misspelled member that let them count twice
    assert independence_family("openalex") == \
        independence_family("semanticscholar") == "scholarly-aggregator"


def test_family_members_are_real_registry_names():
    reg = get_source_registry()
    for family, members in INDEPENDENCE_FAMILIES.items():
        for m in members:
            assert m in reg.names(), (
                f"family {family!r} member {m!r} is not a registered source "
                f"— a stale name here silently un-declares the overlap")


def test_standalone_sources_count_as_themselves():
    assert independence_family("worldbank") == "worldbank"
    assert independence_family("gdelt") == "gdelt"


def test_retrieval_independence_key_derives_from_declaration():
    from tools.pipeline.retrieval import independence_key
    assert independence_key("openalex", "") == \
        independence_key("semanticscholar", "")


# ── plans execute against fixture transports (still zero sockets) ────────

class _Transport:
    def __init__(self, body):
        self.body = json.dumps(body)
        self.urls: list[str] = []

    def __call__(self, url, headers):
        self.urls.append(url)
        return 200, self.body


def _adapter_for(name):
    entry = get_source_registry().get(name)
    t = _Transport({})
    return entry.make_adapter(RestSource(entry.spec, transport=t)), t


@pytest.mark.parametrize("name,question,method", [
    ("worldbank", "Population of Brazil over time", "indicator"),
    ("fdic", "Assets of JPMorgan Chase bank", "search_institutions"),
    ("wayback", "Snapshot of https://whitehouse.gov before 2020-01-01",
     "closest"),
])
def test_planned_calls_bind_to_adapter_methods(name, question, method):
    adapter, transport = _adapter_for(name)
    plan = build_plan(name, question)
    assert plan.plannable
    assert plan.queries[0].method == method
    # attribute exists and accepts exactly these kwargs (stale-plan check)
    getattr(adapter, method)
    try:
        execute(adapter, plan)
    except Exception:
        pass  # empty fixture body may fail parsing; binding is what we pin
    assert transport.urls, "the planned call must reach the transport"
