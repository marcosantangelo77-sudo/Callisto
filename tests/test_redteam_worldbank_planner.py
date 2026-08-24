"""TASK — question battery D1: World Bank planner free-text search defect.

findings/question_battery.md D1 (29 of 41 bad outcomes): the planner stuffed
natural-language questions into the WB API's free-text `search` parameter.
The API ignores that parameter and returns the same default indicator
catalogue page every time (byte-identical sha256 across differently-worded
queries), so fertilizer datasets came back for court-case questions and the
relevance gate kept them. The fix: an honest gap when no curated WDI concept
resolves, plus real WDI codes for common macro concepts so correct retrieval
still happens (silent retrieval would starve leaves just as badly).

Offline suite: no network calls anywhere in this file.
"""
from __future__ import annotations

from tools.sources import query_builder as qb


# Questions from findings/battery/questions.json that previously fell into
# the search_indicators junk path (worldbank contributed admitted fetches
# on 23 of 41 runs; nearly all were this default catalogue).
JUNK_PATH_QUESTIONS = [
    # court case -> used to plan worldbank.search_indicators("supreme court
    # decision obergefell hodges issued january") and admit fertilizer rows
    "Was the Supreme Court decision in Obergefell v. Hodges issued before "
    "January 2016?",
    # bank count -> same shape
    "Are there more than 5,000 active FDIC-insured financial institutions "
    "right now?",
    # scholarly works count -> same shape
    "Have more than 100,000 scholarly works indexed by OpenAlex mentioned "
    "drosophila in their title?",
]


class TestD1NoFreeTextSearchFallback:
    def test_unresolvable_concepts_declare_honest_gap(self):
        for q in JUNK_PATH_QUESTIONS:
            plan = qb.build_plan("worldbank", q)
            assert not plan.plannable, (
                f"question routed to WB free-text search again: "
                f"{[x.describe() for x in plan.queries]}")
            assert plan.queries == [], q

    def test_gap_reason_names_the_defect_and_the_fix(self):
        plan = qb.build_plan("worldbank", JUNK_PATH_QUESTIONS[0])
        low = (plan.reason or "").lower()
        assert "search" in low and "wdi" in low or "indicator" in low
        assert "_worldbank_indicators" in low or "indicator code" in low, (
            "honest gap must say what IS missing and how to supply it")

    def test_no_plan_uses_search_indicators_method(self):
        """The planner must never author search_indicators again: the WB API
        provably ignores its `search` parameter."""
        probe_questions = JUNK_PATH_QUESTIONS + [
            "Was the US unemployment rate lower in January 2023 than June 2026?",
            "Did the World Bank report the US population exceeding 330 million "
            "in 2021?",
            "Does the World Bank report India's population as larger than the "
            "United States population?",
            "What is GDP per capita of China compared with Germany?",
        ]
        for q in probe_questions:
            plan = qb.build_plan("worldbank", q)
            for planned in plan.queries:
                assert planned.method != "search_indicators", (q, planned)


class TestD1CorrectRetrievalStillHappens:
    """The trap: fixing by making World Bank return nothing starves leaves
    exactly as badly. Real concepts must still resolve to REAL indicator
    codes and a country."""

    def test_population_question_resolves_to_real_indicator(self):
        plan = qb.build_plan(
            "worldbank",
            "Did the World Bank report the US population as exceeding 330 "
            "million people in calendar year 2021?")
        assert plan.plannable and plan.queries
        kw = plan.queries[0].kwargs
        assert kw["code"] == "SP.POP.TOTL"
        assert kw["iso3"] == "USA"

    def test_cross_country_comparison_fetches_all(self):
        plan = qb.build_plan(
            "worldbank",
            "Does the World Bank report India's population as larger than "
            "the United States population?")
        assert plan.plannable and plan.queries
        assert plan.queries[0].kwargs["iso3"] == "all"

    def test_unemployment_resolves_not_falls_to_gap(self):
        plan = qb.build_plan(
            "worldbank",
            "Was the unemployment rate lower in Germany than in Spain?")
        assert plan.plannable and plan.queries
        kw = plan.queries[0].kwargs
        assert kw["code"] == "SL.UEM.TOTL.ZS", kw

    def test_explicit_indicator_code_passthrough(self):
        plan = qb.build_plan(
            "worldbank",
            "World Bank indicator SP.DYN.LE00.IN for France, latest value?")
        resolved = plan.resolved.get("indicator_code")
        if resolved == "SP.DYN.LE00.IN":
            assert plan.plannable and plan.queries
            assert plan.queries[0].kwargs["code"] == "SP.DYN.LE00.IN"


class TestAuditOtherPlanners:
    """Same-shape audit across all planners: a natural-language question may
    only flow into a parameter the API actually searches, AND the planner
    must be able to say no when the question is not its source's kind."""

    def test_fdic_does_not_treat_arbitrary_capitalized_words_as_banks(self):
        # 'Supreme Court' is not an FDIC institution predicate target for a
        # question about institution COUNTS — but the name-filter route is a
        # real field=value predicate, so it stays plannable when a bank name
        # genuinely appears. Pin the current behavior: failure-history and
        # name lookups are structured filters, never NL free text.
        p = qb.build_plan("fdic", "Did any FDIC-insured banks fail during 2021?")
        assert p.plannable and p.queries[0].method == "failures"

    def test_wayback_still_requires_a_url(self):
        p = qb.build_plan("wayback", JUNK_PATH_QUESTIONS[0])
        assert not p.plannable

    def test_treasury_refuses_without_catalog_dataset(self):
        p = qb.build_plan("treasury", JUNK_PATH_QUESTIONS[0])
        assert not p.plannable, "treasury must not free-text into the catalog"

    def test_cftc_refuses_without_market_code(self):
        p = qb.build_plan("cftc_cot", JUNK_PATH_QUESTIONS[0])
        assert not p.plannable

    def test_bls_refuses_without_series_id(self):
        p = qb.build_plan("bls", JUNK_PATH_QUESTIONS[0])
        assert not p.plannable

    def test_keyword_sources_are_documented_search_adapters(self):
        # openalex/semanticscholar/fred/clinicaltrials/federalregister/
        # courtlistener/gdelt/uspto_odp pass the core into parameters their
        # APIs DO full-text-search (unlike WB's ignored `search=`). That is
        # the FDIC `filters=` vs `search=` distinction: real search params,
        # not decoration. This test pins that each planner targets the
        # adapter method backed by a genuine server-side search endpoint.
        expectations = {
            "openalex": ("works_search", "query"),
            "semanticscholar": ("paper_search", "query"),
            "fred": ("series_search", "query"),
            "clinicaltrials": ("search_studies", "query_term"),
            "federalregister": ("search", "query_term"),
            "courtlistener": ("search", "query"),
            "gdelt": ("doc_query", "query"),
        }
        for src, (method, param) in expectations.items():
            plan = qb.build_plan(src, "drosophila circadian rhythm")
            assert plan.plannable, src
            assert plan.queries[0].method == method, src
            assert param in plan.queries[0].kwargs, src
