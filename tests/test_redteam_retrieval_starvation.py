"""TASK 191 — retrieval starvation: D3/D4/D2.

Reproductions from findings/known_answer_harness.md. The harness questions
come from task 181 (~/callisto-wt/money/harness/questions.py, ground truth
pinned by direct API calls); the unit tests here pin each defect at its seam
so the fix is checkable without a model or a network.

D3 — source selection cannot route entity questions to Wikidata.
D2 — BLS planner ignores years named in the question; quota errors surface
     as ordinary bodies instead of auth/quota failures.
D4 — the relevance gate rejects exactly-on-topic numeric bodies (FRED
     observations, debt_to_penny rows) while keyword-junk passes.
"""
from __future__ import annotations

import pytest

from tools.pipeline.retrieval import RelevanceGate, translate_question_type
from tools.sources import query_builder as qb
from tools.sources.registry import get_source_registry


# ── harness questions (task 181) ───────────────────────────────────────────

Q01 = "Was the U.S. unemployment rate lower in January 2026 than in January 2023?"
Q02 = ("Did the U.S. unemployment rate exceed 4.0 percent at any point "
       "in the first half of 2026?")
Q03 = "Was the U.S. national debt on March 31, 2020 higher than $23 trillion?"
Q05 = ("Did U.S. nonfarm payrolls fall by more than 15 million between "
       "February 2020 and April 2020?")
Q06 = "Was the federal funds effective rate above 5 percent in June 2007?"
Q07 = "Did any FDIC-insured banks fail during calendar year 2021?"
Q08 = "Is Paris the capital of France according to Wikidata?"
Q10 = "Is the World Bank U.S. population for 2020 greater than 330 million?"
Q19 = "According to Wikidata, was Einstein born in Ulm?"


# ── D4: relevance gate vs numeric bodies ───────────────────────────────────

class TestD4NumericBodies:
    """A numeric body from THE SOURCE THE PIPELINE ITSELF QUERIED is on-topic
    evidence. The gate must admit it while still rejecting genuinely
    irrelevant documents. Correctness, not looseness."""

    def gate(self):
        return RelevanceGate()

    def test_fred_observations_admitted_for_unrate_question(self):
        body = {
            "series_title": "Civilian Unemployment Rate",
            "observations": [
                {"date": "2023-01-01", "value": "3.5"},
                {"date": "2026-01-01", "value": "4.3"},
            ],
        }
        ok, cov, reason = self.gate().judge(Q01, "comparison", body)
        assert ok, f"exact-answer FRED body rejected: {reason}"

    def test_debt_to_penny_rows_admitted(self):
        body = {"data": [{
            "record_date": "2020-03-31",
            "total_public_debt_outstanding_amt": "23686870812640.08"}]}
        ok, cov, reason = self.gate().judge(Q03, "comparison", body)
        assert ok, f"debt_to_penny row rejected: {reason}"

    def test_bare_numeric_body_without_title_still_admitted_when_window_matches(self):
        # No series title at all — dates + values only — but every date the
        # question names is present in the window.
        body = {"observations": [{"date": "2026-03-01", "value": "4.3"},
                                 {"date": "2026-04-01", "value": "4.3"}]}
        ok, cov, reason = self.gate().judge(Q02, "comparison", body)
        assert ok, f"window-matching numeric body rejected: {reason}"

    def test_worldbank_indicator_value_body_admitted(self):
        body = {"page": 1,
                "indicator": {"id": "SP.POP.TOTL", "name": "Population, total"},
                "country": {"id": "US", "value": "United States"},
                "date": "2020", "value": 331002651}
        ok, cov, reason = self.gate().judge(Q10, "comparison", body)
        assert ok, f"World Bank indicator value rejected: {reason}"

    # ── the trap: these MUST stay rejected ────────────────────────────────

    def test_zero_overlap_document_still_rejected(self):
        g = self.gate()
        ok, _, reason = g.judge("semiconductor foundry capex outlook",
                                "empirical",
                                {"title": "Marine biology of kelp forests"})
        assert not ok

    def test_numeric_junk_with_wrong_dates_rejected(self):
        # Numbers whose window does NOT match the question's years must not
        # ride the numeric pass through the gate.
        body = {"observations": [{"date": "1957-06-01", "value": "3.9"},
                                 {"date": "1957-07-01", "value": "3.8"}]}
        ok, cov, reason = self.gate().judge(Q02, "comparison", body)
        assert not ok, "wrong-window numeric body admitted"

    def test_keyword_search_catalogue_row_still_rejected(self):
        junk = {"result": [{"id": "AG.CON.FERT.ZS",
                            "name": "Fertilizer consumption (% of production)",
                            "value": None}]}
        ok, _, _ = self.gate().judge(Q01, "comparison", junk)
        assert not ok

    def test_news_prose_sharing_topic_words_is_NOT_promoted_by_numeric_rule(self):
        # Prose gains nothing from the structural route: when it fails token
        # coverage it must be rejected for coverage, never waved through as
        # 'structured data'.
        news = {"title": "Hiring outlook chatter",
                "abstract": ("Policymakers discussed the jobs picture; "
                             "analysts expect hiring to stay soft.")}
        ok, cov, reason = RelevanceGate().judge(Q01, "comparison", news)
        if ok:
            # admitted on genuine token coverage only — verify the numeric
            # route was not the reason
            assert "structured data" not in reason
        else:
            assert "topical words" in reason


# ── D3: entity questions route to Wikidata ─────────────────────────────────

class TestD3EntityRouting:

    def test_paris_capital_of_france_selects_wikidata(self):
        reg = get_source_registry()
        translated, chosen = translate_question_type(reg, Q08, "")
        assert "wikidata" in chosen, (
            f"Wikidata not selected for an entity lookup: {chosen}")

    def test_einstein_ulm_selects_wikidata(self):
        reg = get_source_registry()
        translated, chosen = translate_question_type(reg, Q19, "")
        assert "wikidata" in chosen, (
            f"Wikidata not selected for an entity lookup: {chosen}")

    def test_entity_question_ranks_wikidata_first(self):
        reg = get_source_registry()
        translated, chosen = translate_question_type(reg, Q08, "")
        # Wikidata holds the answer and must outrank keyword noise (FDIC
        # 'institution search by name' matching 'Paris'); it may not be the
        # ONLY selection — corroboration candidates are fine — but the
        # entity graph leads.
        assert chosen[0] == "wikidata", (
            f"wikidata did not lead entity-question selection: {chosen}")

    def test_macro_question_routing_unchanged(self):
        reg = get_source_registry()
        translated, chosen = translate_question_type(
            reg, Q01, "")
        assert any(s in chosen for s in ("fred", "bls")), (
            f"macro routing broken by entity rule: {chosen}")
        assert "wikidata" not in chosen, chosen


# ── D2: BLS planner year handling + quota masking ──────────────────────────

class TestD2BlsPlanner:

    def test_year_in_question_defines_window_start(self):
        plan = qb.build_plan("bls", Q01)
        assert plan.plannable and plan.queries
        kwargs = plan.queries[0].kwargs
        assert kwargs["start_year"] <= 2023, (
            f"'January 2023' question planned start_year={kwargs['start_year']}")

    def test_payrolls_2020_window_starts_at_2020(self):
        plan = qb.build_plan("bls", Q05)
        assert plan.plannable and plan.queries
        assert plan.queries[0].kwargs["start_year"] <= 2020

    def test_no_year_named_defaults_to_recent(self):
        plan = qb.build_plan("bls", "What is the current unemployment rate?")
        assert plan.plannable and plan.queries
        kw = plan.queries[0].kwargs
        assert kw["start_year"] == kw["end_year"] - 2

    def test_quota_error_is_reported_not_masked(self):
        """A BLS REQUEST_NOT_PROCESSED payload is an auth/quota failure, not a
        body of evidence. The retriever must record it as such."""
        quota_body = {"status": ["REQUEST_NOT_PROCESSED"],
                      "message": ["daily threshold reached"]}
        reason = qb.classify_fetch_failure("bls", quota_body)
        assert reason is not None and "quota" in reason.lower(), reason

    def test_normal_bls_payload_has_no_failure(self):
        body = {"status": ["REQUEST_SUCCEEDED"],
                "Results": {"series": []}}
        assert qb.classify_fetch_failure("bls", body) is None

    def test_non_bls_payloads_unaffected(self):
        assert qb.classify_fetch_failure("fred", {"status": "weird"}) is None


# ── HTTP-200 error envelopes: BEA and CFTC/Socrata ─────────────────────────

class TestHttp200ErrorEnvelopes:
    """BEA and CFTC also return 200-OK bodies that are really ERROR
    ENVELOPES. The classifier must flag them narrowly while never rejecting
    legitimate (even empty) data payloads."""

    # Documented singular wire shape from the live BEA API:
    def test_bea_singular_error_envelope_flagged(self):
        body = {"BEAAPI": {"Results": {"Error": {
            "APIErrorCode": "1",
            "APIErrorDescription": "bad key"}}}}
        reason = qb.classify_fetch_failure("bea", body)
        assert reason is not None and "BEA" in reason, reason

    def test_bea_singular_error_envelope_carries_detail(self):
        body = {"BEAAPI": {"Results": {"Error": {
            "APIErrorCode": "1",
            "APIErrorDescription": "bad key",
            "ErrorMessage": "An invalid API key was supplied."}}}}
        reason = qb.classify_fetch_failure("bea", body)
        assert reason is not None and "bad key" in reason, reason

    def test_bea_legacy_plural_error_list_still_flagged(self):
        body = {"BEAAPIs": {"Error": [{
            "APIErrorDescription": "Invalid API KEY",
            "ErrorMessage": "An invalid API key was supplied."}]}}
        reason = qb.classify_fetch_failure("bea", body)
        assert reason is not None and "BEA" in reason, reason

    def test_bea_plural_non_error_list_not_flagged(self):
        """The legacy plural wrapper is honored only while narrowly
        structured: a non-list Error is not recognized."""
        body = {"BEAAPIs": {"Error": {"oops": True}}}
        assert qb.classify_fetch_failure("bea", body) is None

    def test_bea_singular_valid_data_payload_not_an_error(self):
        body = {"BEAAPI": {"Results": {"Data": [
            {"CL_UNIT": "Level", "DataValue": "2153.606"}]}}}
        assert qb.classify_fetch_failure("bea", body) is None

    def test_bea_singular_valid_empty_data_list_not_an_error(self):
        body = {"BEAAPI": {"Results": {"Data": []}}}
        assert qb.classify_fetch_failure("bea", body) is None

    def test_cftc_cot_socrata_error_mapping_flagged(self):
        # CftcCotAdapter normalizes whatever Socrata returned under 'rows';
        # its real source identity is cftc_cot (not the old 'cftc' alias).
        body = {"rows": {"error": True,
                         "message": "no matching rows",
                         "code": "query.soql.noMatch"},
                "_fetch": {}}
        reason = qb.classify_fetch_failure("cftc_cot", body)
        assert reason is not None and "CFTC" in reason, reason
        assert "query.soql.noMatch" in reason, reason

    def test_cftc_alias_no_longer_recognized(self):
        """The retired alias must not silently keep working."""
        body = {"rows": {"error": True,
                         "message": "no matching rows",
                         "code": "query.soql.noMatch"}}
        assert qb.classify_fetch_failure("cftc", body) is None

    def test_cftc_rows_without_documented_shape_not_flagged(self):
        # error=True alone without a string message is not the documented
        # failure shape; neither is an error mapping with empty message.
        assert qb.classify_fetch_failure(
            "cftc_cot", {"rows": {"error": True}}) is None
        assert qb.classify_fetch_failure(
            "cftc_cot", {"rows": {"error": False,
                                  "message": "x"}}) is None

    def test_cftc_cot_legitimate_empty_rows_list_not_an_error(self):
        assert qb.classify_fetch_failure("cftc_cot", {"rows": []}) is None

    def test_generic_error_key_heuristic_rejected(self):
        """A source whose ordinary data contains an 'error'-ish key stays
        untouched — no broad generic heuristics."""
        body = {"data": [{"error_rate": "0.01"}]}
        assert qb.classify_fetch_failure("fred", body) is None
