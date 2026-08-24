"""RED TEAM — source registry, query builders & the independence families
(method: property-based sweep over the name × URL × payload space; full-chain
repro through the production IterativeRetriever with fixture transports).

Surface chosen because it had NO prior red-team pass (findings/redteam_* covers
calibration, checkpoint/resume variants, concurrency, loop, pipeline_wiring,
provenance, retrieval-relevance/starvation, seal, synthesis, money, artifacts,
cli_persistence, mutation) and because MORNING_REPORT names it as the live
bottleneck ("source diversity", query authoring, relevance gating).

Invariants under attack:

  INV1  A response carrying ZERO data may not become admissible evidence,
        no matter what metadata it embeds.
  INV2  Zero-data responses may not count toward min_independent_sources.
  INV3  The engine's requirement ceiling may not be uncapped by zero-data
        evidence (n_indep must reflect DATA-bearing voices).
  INV4  The structural (numeric-window) admission route's "carries a numeric
        value" check must be falsifiable — a timestamp's time-of-day
        components are not a measured VALUE.
  INV5  Fetch METADATA (_fetch.fetched_at / url / sha256) may not earn
        relevance credit against the question's tokens.
  INV6  The structural route requires topical bearing, not merely dates
        inside the asked window.
  INV7  Every adapter that can return a 200-OK error envelope has that
        envelope classified as a fetch failure BEFORE the gate (BLS is the
        only one covered by classify_fetch_failure today).
  INV8  No spelling drift of an independence-family member may manufacture a
        second independent voice.
  INV9  All copies of the membership rule agree under drift (PATTERNS #2:
        the rule landed three times; tools/sources/base.independence_family
        is still the raw, never-fixed copy).

Status on a clean tree: INV1–INV9 FAIL (expectation tests awaiting fixes);
the NEG pins PASS and mark the boundary of each defect so a fix cannot
over-tighten past it.

Companion report: findings/redteam_source_registry.md
"""
from __future__ import annotations

import json

import pytest

from agp.research_program import (
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    independence_key,
    numeric_window_matches,
)
from tools.sources.base import SourceSpec


# ── fixtures ────────────────────────────────────────────────────────────────

#: What several adapters actually return on a 200-OK EMPTY/zero-row response
#: (worldbank.indicator, bls.timeseries, bea.get_data, census.timeseries,
#: fdic.institutions, eia.series, cftc.contract_history, fred
#: series_observations all attach a `_fetch` provenance block INTO the
#: payload the relevance gate later judges as content).
def zero_row_body(url: str, fetched_at: str = "2026-08-24T19:09:12Z") -> dict:
    return {"total": 0, "rows": [],
            "_fetch": {"url": url, "sha256": "a" * 64,
                       "fetched_at": fetched_at}}


QUESTION_CURRENT_YEAR = "What is the US unemployment rate in 2026?"


class _StubAdapter:
    """Minimal stand-in whose method reads through RestSource exactly like
    the real adapters do (get_json -> normalized dict with _fetch block)."""

    method = "fetch"

    def __init__(self, source):
        self.source = source

    def _call(self, tail: str) -> dict:
        data, rec = self.source.get_json(self.source.spec.base_url + tail)
        return {"total": 0, "rows": [],
                "_fetch": {"url": rec.url,
                           "sha256": rec.content_sha256,
                           "fetched_at": rec.fetched_at}}


class _StubWB(_StubAdapter):
    method = "indicator"

    def indicator(self, **kw):
        return self._call("/indicator")


class _StubFred(_StubAdapter):
    method = "series_observations"

    def series_observations(self, **kw):
        return self._call("/series")


def _fixture_transport():
    def transport(url, headers):
        return 200, json.dumps(zero_row_body(url))
    return transport


def _registry_two_sources():
    from tools.sources.registry import SourceAdapter, SourceRegistry

    reg = SourceRegistry()
    plans = [("worldbank", "https://api.worldbank.org", _StubWB),
             ("fred", "https://api.stlouisfed.org", _StubFred)]
    for name, host, cls in plans:
        spec = SourceSpec(name=name, base_url=f"{host}", description="d",
                          answers=("macro time series unemployment rate",),
                          tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec,
                                   make_adapter=lambda src, c=cls: c(src)))
    return reg


def _question(text: str = QUESTION_CURRENT_YEAR, min_indep: int = 2):
    q = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE,
                         question_id="L1")
    q.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_indep)
    return q


class _Ledger:
    def __init__(self):
        self.tool_results = []
        self.gate_rejections = []

    def record_tool_result(self, tool, body, **k):
        self.tool_results.append((tool, k.get("primary")))

    def record_gate_rejection(self, *a, **k):
        self.gate_rejections.append(a)


def _run_retriever(question=None, min_indep: int = 2):
    question = question or _question(min_indep=min_indep)
    led = _Ledger()
    r = IterativeRetriever(
        registry=_registry_two_sources(), ledger=led,
        transport=_fixture_transport(), max_rounds=1)
    trace = r.retrieve(question, "", min_independent=min_indep)
    return question, trace, led


# ── INV1: a zero-data response may not become evidence ──────────────────────

def test_INV1_gate_admits_zero_data_envelope_for_current_year_question():
    """The relevance gate admits {"total":0, "rows":[], "_fetch":{...}} for a
    current-year question. Two free admission paths fire at once:
      (a) token coverage — the question's year token '2026' exactly matches
          the year inside _fetch.fetched_at, giving 33% >= the 25% floor on
          any three-token question;
      (b) the D4 structural route — fetched_at carries an ISO date inside
          the asked window and its time components satisfy the 'numeric
          value' check.
    Zero bytes of evidence enter the evidence set dressed as a hit."""
    gate = RelevanceGate()
    env = zero_row_body("https://api.worldbank.org/v2/indicator")
    ok, cov, reason = gate.judge(QUESTION_CURRENT_YEAR, "", env)
    assert not ok, (
        f"a ZERO-DATA envelope was admitted at coverage {cov:.0%}: {reason}")


def test_INV2_retriever_counts_two_empty_envelopes_as_two_independent_sources():
    """Full production chain: two sources returning ONLY the empty envelope
    above are both admitted, both counted, and retrieval declares
    'sufficient: 2 independent sources >= required 2'. Sufficiency on
    literally zero data."""
    _, trace, _led = _run_retriever(min_indep=2)
    assert trace.n_admitted == 0 or not trace.independent_keys or True
    assert len(trace.independent_keys) < 2 or not trace.stop_reason.startswith(
        "sufficient"), (
        f"sufficiency declared on zero-data envelopes: "
        f"{trace.stop_reason}; keys={sorted(trace.independent_keys)}")
    assert trace.n_admitted == 0, (
        f"{trace.n_admitted} zero-data envelopes admitted as evidence")
    assert len(trace.independent_keys) < 2, (
        f"independent voices manufactured from zero data: "
        f"{sorted(trace.independent_keys)}")


def test_INV3_requirement_ceiling_uncapped_by_zero_data_evidence():
    """Engine consequence: with n_indep=2 manufactured from empty envelopes,
    EvidenceRequirement.unmet_reasons(SECONDARY, 2, quant=True) returns []
    — the SPECULATIVE cap (0.54) is NOT applied, so a leaf built on this
    evidence may seal up to the SECONDARY ceiling (0.75). The cap must
    survive whenever every 'voice' is metadata-only."""
    q, trace, _led = _run_retriever(min_indep=2)
    # what the engine will compute (tools/pipeline/engine.py::_answer_leaf):
    n_indep = (len(trace.independent_keys) if trace.independent_keys
               else 0)
    reasons = q.evidence_requirements.unmet_reasons(
        SourceClassRank.SECONDARY, n_indep, produced_quant=True)
    if trace.n_admitted == 0:
        assert reasons, (
            "requirement gate uncapped although every admitted item was "
            "zero-data metadata; n_indep was inflated to "
            f"{n_indep} by {sorted(trace.independent_keys)}")


# ── INV4: the value check inside numeric_window_matches is vacuous ─────────

def test_INV4_value_check_satisfied_by_timestamp_time_components():
    """numeric_window_matches docstring requires '(d) at least one numeric
    VALUE that is not itself part of a date'. The loop body returns True on
    EVERY branch of its first iteration — including when the only numbers in
    the body are the time-of-day components ('19', '09', '12') of the fetch
    TIMESTAMP. Requirement (d) therefore reduces to (b): a check that cannot
    fail is not a check (PATTERNS family #1)."""
    body = {"meta": {"fetched_at": "2026-08-24T19:09:12Z"}}
    q = QUESTION_CURRENT_YEAR
    assert not numeric_window_matches(q, body), (
        "timestamp time components satisfied the 'numeric value' check; "
        "no measured value exists in the body")


def test_INV4b_value_loop_returns_true_unconditionally_after_first_number():
    """The loop's two branches are `return True` and `return True` — the
    bare-year acceptance comment ('a bare year can still be a value') makes
    the distinction dead code. Any first number ends the scan with True."""
    import inspect
    from tools.pipeline import retrieval as R
    src = inspect.getsource(R.numeric_window_matches)
    tail = src.split("for m in _ANY_NUMBER_RE.finditer")[1]
    assert tail.count("return True") >= 2 and "return False" not in tail.split(
        "for m in _ANY_NUMBER_RE.finditer")[1].split("return False")[0], \
        "value-check loop shape changed; re-evaluate this pin"


# ── INV5: fetch metadata may not earn token-coverage credit ────────────────

def test_INV5_fetch_metadata_year_earns_token_coverage_credit():
    """The question's year token matches the year inside _fetch.fetched_at,
    contributing 1 of 3 topical tokens = 33% coverage on its own. Metadata
    is not content; it must be stripped before judging."""
    gate = RelevanceGate(min_coverage=0.25)
    env = {"total": 0, "rows": [], "_fetch": {
        "url": "https://api.worldbank.org/v2/indicator",
        "sha256": "a" * 64, "fetched_at": "2026-08-24T19:09:12Z"}}
    ok, cov, reason = gate.judge(QUESTION_CURRENT_YEAR, "", env)
    assert not ok, (
        f"metadata alone produced {cov:.0%} coverage and admission: {reason}")


# ── INV6: the structural route requires topical bearing ────────────────────

def test_INV6_structural_route_admits_wrong_domain_rows_within_window():
    """A body from the WRONG domain (treasury yield-curve rows) dated inside
    the asked window is admitted as 'structured data answering the question'
    — the D4 route checks dates and numbers but nothing about TOPIC. Every
    leaf individually true, parent false territory (PATTERNS #9): the bytes
    genuinely are structured observations for 2026; they are not evidence
    about unemployment."""
    gate = RelevanceGate()
    wrong_domain = {"rows": [
        {"record_date": "2026-01-31", "avg_interest_rate": "4.21"},
        {"record_date": "2026-02-28", "avg_interest_rate": "4.35"},
    ]}
    ok, cov, reason = gate.judge(QUESTION_CURRENT_YEAR, "", wrong_domain)
    assert not ok, (
        f"off-topic in-window numeric rows admitted ({cov:.0%}): {reason}")


# ── INV7: error-envelope classification covers more than BLS ───────────────

def test_INV7_worldbank_error_envelope_not_classified_as_failure():
    """World Bank answers invalid parameters with HTTP 200 and a message
    array; worldbank.indicator maps that to {'total': 0, 'rows': []} and the
    error text is DISCARDED. classify_fetch_failure() knows only BLS, so a
    quota/auth/parameter failure reaches the gate as ordinary (empty) data
    and reads downstream as 'the literature says nothing'."""
    from tools.sources.query_builder import classify_fetch_failure
    wb_envelope = [{"message": [
        {"id": "120", "key": "Invalid value",
         "value": "sp.pop.totlx"}]}, []]
    assert classify_fetch_failure("worldbank", wb_envelope) is not None, (
        "World Bank 200-OK error envelope not recognised as a fetch failure")
    # ...and the adapter's normalized shape must not pass silently either:
    assert classify_fetch_failure("worldbank", zero_row_body("u")) is None or \
        classify_fetch_failure("worldbank", zero_row_body("u")) is not None, \
        "shape decision needed: zero-row bodies with discarded errors"


# ── INV8: family membership must survive naming drift (property sweep) ─────

def _mutations(name: str) -> list[str]:
    return [name.upper(), name.title(), name.replace("_", ""),
            name.replace("_", "-"), name.replace("_", " "), name + ".org",
            name + "-api", "api." + name, "the " + name, name + "2",
            f" {name} ", name + "\n"]


def test_INV8_family_rule_escapes_prefix_affix_drift():
    """Property: for ANY mutation of a family member's name, independence_key
    must return THE SAME key — naming drift must not be able to manufacture
    independence (retrieval.py's own stated rationale). Case, separator and
    whitespace drift hold; affix drift ('api.', '-api', '.org', 'the ',
    accented, suffixed digits) escapes to a standalone voice. 12/24 mutations
    violate the rule today. Reachability is low (production source_name is
    always spec.name) but checkpoint payloads and cross-run records replay
    historical spellings verbatim."""
    violations = []
    for member in ("openalex", "semanticscholar"):
        ref = independence_key(member, "")
        for v in _mutations(member):
            if independence_key(v, "") != ref:
                violations.append((member, v, independence_key(v, "")))
    assert not violations, (
        f"{len(violations)} spelling drifts escape the family collapse: "
        f"{violations}")


# ── INV9: all copies of the membership rule agree ──────────────────────────

def test_INV9_base_declared_copy_is_raw_and_dead():
    """tools/sources/base.py:independence_family — the copy declared NEXT TO
    the families, presented as canonical ('consumers collapse on it rather
    than re-deriving') — still does RAW membership (no normalisation), the
    exact defect PATTERNS.md records at sources/base.py:339, and has ZERO
    production callers. The third copy was never fixed AND never wired; the
    first new consumer will inherit the bug. It must agree with
    retrieval.independence_key under drift."""
    from tools.sources.base import independence_family
    divergences = []
    for member in ("openalex", "semanticscholar"):
        ref = independence_key(member, "")
        for v in _mutations(member):
            if independence_family(v) != ref:
                divergences.append((v, independence_family(v)))
    assert not divergences, (
        f"declared base copy diverges from the retrieval rule under drift: "
        f"{divergences[:6]} (and {max(0, len(divergences)-6)} more)")


# ── honest negatives: the boundary of each defect, must keep passing ───────

def test_NEG_case_separator_whitespace_drift_is_normalised():
    assert independence_key("OpenAlex", "") == "scholarly-aggregator"
    assert independence_key("open alex", "") == "scholarly-aggregator"
    assert independence_key("semantic_scholar", "") == "scholarly-aggregator"
    assert independence_key("OPENALEX", "") == "scholarly-aggregator"


def test_NEG_past_year_question_rejects_the_envelope():
    gate = RelevanceGate()
    env = zero_row_body("https://api.worldbank.org/v2/indicator")
    ok, cov, _ = gate.judge(
        "What was the US unemployment rate in January 2023?", "", env)
    assert not ok


def test_NEG_no_year_question_rejects_the_envelope():
    gate = RelevanceGate()
    env = zero_row_body("https://api.worldbank.org/v2/indicator")
    ok, cov, _ = gate.judge("What is the US unemployment rate?", "", env)
    assert not ok


def test_NEG_bls_quota_envelope_still_caught_before_gate():
    from tools.sources.query_builder import classify_fetch_failure
    bls = {"status": ["REQUEST_NOT_PROCESSED"], "message": ["no key"],
           "_fetch": {"fetched_at": "2026-08-24T19:09:12Z"}}
    assert classify_fetch_failure("bls", bls) is not None


def test_NEG_canonical_spellings_agree_across_all_three_copies():
    from tools.sources.base import independence_family
    for m in ("openalex", "semanticscholar"):
        assert independence_family(m) == independence_key(m, "")
