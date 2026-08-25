"""RED TEAM — source registry & query builders (rotating pass, 2026-08-24).

Surface: SourceRegistry selection + tools/sources/query_builder.py planners
+ the adapter/gate seam where fetched payloads meet RelevanceGate.judge().
Method: property-style invariant checks over the question->plan space plus
adversarial payloads ("what happens when a source lies, or returns 200 with
zero results" — the brief's named unattacked scenario).

Families hunted (see PATTERNS.md):
  1  verification that never runs      -> R4 (four dead id-shape regexes),
                                          R1 (the gate judges bytes nobody
                                          means it to judge)
  3  absence treated as success        -> R1/R2 (200 + zero data ingested)
  4  a label standing in for evidence  -> R3 (uppercase+digits = identifier),
                                          R6 (assignee spelling decides filter)
  9  internally consistent, wrong      -> R5 (comparison answered about one
                                          bank), R6 (two companies ANDed into
                                          one assignee filter)

Offline suite: no socket is opened anywhere; the end-to-end test injects a
transport. Defect tests encode DESIRED behaviour and are expected to FAIL on
master; pins encode behaviour that already holds and must survive the fix.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest

from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate
from tools.sources import query_builder as qb
from tools.sources.base import SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry, get_source_registry


def _trailer(url: str) -> dict:
    """The provenance trailer every list-returning source adapter embeds in
    its parsed payload before the pipeline ever sees it (fred.py:76,
    bls.py:78, bea.py:76, census.py:69, eia.py:77/98, worldbank.py:67/93,
    cftc.py:72, fdic.py:80, sec_fts.py:70, cmefedfut.py:134)."""
    return {"url": url, "sha256": hashlib.sha256(b"x").hexdigest(),
            "fetched_at": "2026-08-24T09:00:00Z"}


# ── R1: the _fetch provenance trailer pollutes relevance judging ─────────


class TestR1TrailerPollutesGate:
    Q_PAST = "What was the unemployment rate in January 2023?"
    FRED_URL_23 = ("https://api.stlouisfed.org/fred/series/observations"
                   "?series_id=UNRATE&observation_start=2023-01-01"
                   "&limit=120&sort_order=desc&file_type=json")

    def test_r1a_zero_data_body_admitted_via_url_echo(self):
        """CRITICAL (family 3): a 200 body carrying ZERO observations is
        admitted by RelevanceGate because the planner's own URL parameters
        (embedded in the adapter's _fetch trailer) supply the question's
        year token '2023'. One echoed token = 25% coverage = admission at
        the default min_coverage. The gate must judge PAYLOAD content, not
        the fetch record's echo of the request."""
        gate = RelevanceGate()
        body = {"observations": [], "_fetch": _trailer(self.FRED_URL_23)}
        ok, cov, reason = gate.judge(self.Q_PAST, self.Q_PAST, body)
        assert not ok, (
            f"empty observations admitted at coverage {cov:.2f} via "
            f"{reason!r} — the only matched token came from the URL in "
            f"the _fetch trailer, not from any data")

    def test_r1b_error_envelope_admitted_via_url_words(self):
        """CRITICAL (family 3+5): a Treasury-shaped ERROR envelope whose
        request URL contains question vocabulary ('debt', a date the
        question names) sails through the gate as relevant evidence. A
        lying source that echoes request parameters passes relevance
        without carrying one byte of substance."""
        gate = RelevanceGate()
        url = ("https://api.fiscaldata.treasury.gov/services/api/"
               "fiscal_service/v2/debt/mspd/mspd_table_1?limit=100"
               "&filters=record_date:gte:2026-01-01")
        err = {"error": True, "message": "dataset not found",
               "_fetch": _trailer(url)}
        q = "National debt since 2026-01-01"
        ok, cov, reason = gate.judge(q, q, err)
        assert not ok, (
            f"error envelope admitted at coverage {cov:.2f}; matched "
            f"tokens came from the request URL echoed in _fetch.url")

    def test_r1c_pin_same_bodies_without_trailer_are_rejected(self):
        """PIN (must survive the fix): strip the _fetch trailer and the
        gate honestly rejects both bodies above. This isolates the trailer
        as the cause and guards the fix against overcorrecting into
        rejecting genuine structured data."""
        gate = RelevanceGate()
        assert not gate.judge(self.Q_PAST, self.Q_PAST,
                              {"observations": []})[0]
        err = {"error": True, "message": "dataset not found"}
        assert not gate.judge("National debt since 2026-01-01",
                              "National debt since 2026-01-01", err)[0]


# ── R2: end-to-end — the retriever ingests a lying source ────────────────


class TestR2LyingSourceIngestedEndToEnd:
    def test_retriever_must_not_ingest_zero_data_200_body(self, monkeypatch):
        """CRITICAL (families 3+9): through the REAL retrieval loop —
        registry select, planner, RestSource record, gate — a source that
        answers 200 OK with {"observations": []} lands in trace.admitted
        as evidence for the leaf. Downstream this becomes a synthesis
        voice: absence of data masquerading as data. Must be rejected or
        failed, never admitted."""
        monkeypatch.setenv("CALLISTO_FRED_API_KEY", "test-key")
        import tools.sources.fred as fred_mod

        reg = SourceRegistry()
        reg.register(SourceAdapter(spec=fred_mod.SPEC,
                                   make_adapter=fred_mod.FredAdapter))

        def transport(url, headers):
            return 200, json.dumps({"observations": []})

        class _Rec:
            def record_tool_result(self, *a, **k):
                pass

            def record_gate_rejection(self, *a, **k):
                pass

        r = IterativeRetriever(registry=reg, ledger=_Rec(), transport=transport,
                               max_rounds=1, max_sources_per_leaf=3)
        q = SimpleNamespace(
            question_id="L1",
            text=self.__class__.Q if hasattr(self.__class__, "Q") else "",
            evidence_requirements=None)
        q.text = "What was the unemployment rate in January 2023?"
        trace = r.retrieve(q, "unemployment rate January 2023",
                           min_independent=2)
        names = [f.source_name for f in trace.admitted]
        assert not names, (
            f"zero-data 200 body ingested as evidence from {names}; "
            f"rejected={[(x.source_name, x.reason[:60]) for x in trace.rejected]}")


# ── R3: passthrough-before-curation identifier hijack (family 4) ─────────


class TestR3IdentifierHijack:
    HIJACKS = [
        # (source, ordinary question, junk identifier it resolves to)
        ("fred", "Inflation trend since COVID19", "COVID19"),
        ("treasury", "National debt in Q1 2024", "Q1"),
        ("bls", "CPI inflation in Q2 2024", "Q2"),
        ("cftc_cot", "COT positioning for gold, week 202401", "202401"),
        ("worldbank", "GDP for country X1 in 2020", "X1"),
    ]

    @pytest.mark.parametrize("source,question,junk", HIJACKS)
    def test_quarter_and_acronym_tokens_must_not_become_identifiers(
            self, source, question, junk):
        """HIGH (family 4): any fully-uppercase token containing a digit is
        trusted as a source-native identifier and SHORT-CIRCUITS curated
        concept resolution — Q1..Q4 quarters, H1/H2 halves, COVID19, Y2K,
        G7 all qualify. Each planner confidently authors a fetch of an
        identifier the question never supplied as an identifier. Desired:
        such tokens go to candidates/disambiguation (or are validated
        against a known-id set), never silently resolved."""
        plan = qb.build_plan(source, question)
        slot = {"fred": "series_id", "treasury": "dataset",
                "bls": "series_id", "cftc_cot": "market_code",
                "worldbank": "indicator_code"}[source]
        assert slot not in plan.resolved, (
            f"{source}: {junk!r} auto-resolved from {question!r}; "
            f"plan={plan.to_dict()}")

    def test_curated_resolution_not_bypassed_by_word_that_is_also_an_id(self):
        """HIGH: 'GDP' is an English word that happens to also be a FRED
        series id (nominal level). Because passthrough runs BEFORE the
        curated table, asking about GDP *growth* resolves to nominal GDP
        instead of the table's best candidate GDPC1 (real GDP). A string
        coincidence overrides curated knowledge."""
        plan = qb.build_plan("fred", "What was US GDP growth in Q1 2024?")
        assert plan.resolved.get("series_id") == "GDPC1", (
            f"resolved {plan.resolved} — the word GDP hijacked resolution "
            f"away from the curated best-first candidate")


# ── R4: verification shapes that never run (families 1+2) ────────────────


class TestR4DeadValidationShapes:
    def test_worldbank_explicit_indicator_code_is_plannable(self):
        """HIGH: the WB planner's own refusal says 'or supply an explicit
        indicator code like SP.POP.TOTL' — but supplying exactly that is
        unplannable, because no dotted-code passthrough exists. The
        documented contract is false; _WB_INDICATOR_RE encodes the shape
        and is referenced nowhere."""
        plan = qb.build_plan("worldbank", "SP.POP.TOTL")
        assert plan.plannable and plan.queries, (
            f"explicit indicator code refused: {plan.reason!r}")

    @pytest.mark.parametrize("const", [
        "_FRED_ID_RE", "_BLS_ID_RE", "_CIK_RE", "_WB_INDICATOR_RE"])
    def test_id_shape_regexes_are_referenced_somewhere(self, const):
        """HIGH (family 1: a check that cannot fail): four id-SHAPE
        validators are defined in query_builder and used NOWHERE — while
        _resolve's live passthrough accepts any uppercase+digits token
        (see R3). The correct validation exists and never runs."""
        src = inspect.getsource(qb)
        n = src.count(const)
        assert n > 1, (
            f"{const} appears {n} time(s) in query_builder.py: defined, "
            f"never called — validation theatre")


# ── R5: FDIC entity handling (family 9: half-right looks like right) ─────


class TestR5FdicEntities:
    def test_comparison_question_must_disclose_dropped_entities(self):
        """MEDIUM: two banks named, one searched, zero disclosure. The plan
        resolves bank_name to ONE institution and carries no candidates —
        a downstream comparison conclusion rests on half its entities and
        nothing marks the gap."""
        plan = qb.build_plan(
            "fdic", "Compare JPMorgan Chase and Wells Fargo deposits")
        dropped = {"JPMorgan", "Wells Fargo"} - {
            str(plan.resolved.get("bank_name", "")).split()[0]}
        assert plan.candidates.get("bank_name") or \
            len(plan.resolved) > 1 or plan.queries == [] or True
        # the actual invariant: the plan must not silently commit to exactly
        # one of several named entities
        named_both = ["wells fargo", "jpmorgan"]
        hits = [n for n in named_both if n in plan.reason.lower()]
        assert len(hits) >= 2 or plan.candidates, (
            f"plan silently picked '{plan.resolved.get('bank_name')}' from "
            f"a two-entity comparison; reason={plan.reason!r}")

    def test_camelcase_proper_noun_is_plannable(self):
        """MEDIUM: 'JPMorgan' defeats the proper-noun regex ([A-Z][a-z]{2,}
        needs two lowercase after the initial), so a directly answerable
        question gets an honest-gap refusal citing vocabulary rules the
        name does not violate."""
        plan = qb.build_plan("fdic", "What is the deposit base of JPMorgan?")
        assert plan.plannable, f"refused: {plan.reason!r}"


# ── R6: USPTO assignee extraction (family 4 + 9) ─────────────────────────


class TestR6UsptoAssignee:
    def test_two_companies_never_anded_into_one_assignee_filter(
            self, monkeypatch):
        monkeypatch.setenv("CALLISTO_USPTO_ODP_KEY", "test-key")
        """LOW-MED: 'patents assigned to Apple Inc and Microsoft Corp'
        produces assigneeName:"Apple Inc and Microsoft Corp" — a filter
        guaranteed to return zero results, silently answering 'no such
        patents' about two real assignees. Conjunctions must split the
        capture or fall back to the core search."""
        plan = qb.build_plan("uspto_odp",
                             "patents assigned to Apple Inc and Microsoft Corp")
        q = plan.queries[0].kwargs["query"] if plan.plannable else ""
        assert "and " not in q, f"conjunction swallowed into filter: {q!r}"

    def test_assignee_extraction_is_case_consistent(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_USPTO_ODP_KEY", "test-key")
        """The assignee regex is case-sensitive: sentence-initial 'Patents'
        disables extraction entirely while lowercase 'patents' extracts.
        Same words, same meaning, different plan — spelling deciding
        structure (family 4)."""
        lower = qb.build_plan("uspto_odp",
                              "patents assigned to Apple Inc about batteries")
        upper = qb.build_plan("uspto_odp",
                              "Patents assigned to Apple Inc about batteries")
        lo = lower.queries[0].kwargs["query"] if lower.plannable else ""
        up = upper.queries[0].kwargs["query"] if upper.plannable else ""
        assert ("assigneeName" in lo) == ("assigneeName" in up), (
            f"case changed the plan: {lo!r} vs {up!r}")


# ── PINS: behaviour that already holds and must survive fixes ┌───────────


class TestRegistryPins:
    """MORNING_REPORT's live battery, kept as regression pins: these five
    selections were broken on 2026-08-21 and fixed since. A fix for R3/R4
    must not regress them."""

    @pytest.mark.parametrize("question,expected", [
        ("scholarly works", "openalex"),
        ("clinical trials", "clinicaltrials"),
        ("scholarly literature about semiconductor supply chains",
         "openalex"),
    ])
    def test_selection_battery(self, question, expected):
        names = [s.name for s in get_source_registry().select(question)]
        assert expected in names, f"{question!r} -> {names}"

    def test_raising_min_score_never_adds_sources(self):
        """Property (400-case probe in findings): inclusion is monotone in
        min_score. The diagnostic-score floor may lift a score to 0.5 but
        must never override the caller's threshold (its own comment
        promises 'a caller asking for 0.99 still gets 0.99')."""
        import random

        reg = get_source_registry()
        rng = random.Random(7)
        vocab = ["unemployment", "rate", "clinical trials", "gdp",
                 "semiconductor", "supply chain", "bank failures",
                 "crude oil", "yield curve", "population china",
                 "co2 emissions", "patents", "court opinions",
                 "news coverage", "housing starts", "debt"]
        for _ in range(150):
            k = rng.randint(1, 4)
            q = " ".join(rng.sample(vocab, k))
            lo, hi = sorted((rng.uniform(0.1, 0.95),
                             rng.uniform(0.1, 0.95)))
            low_set = {s.name for s in reg.select(q, min_score=lo)}
            high_set = {s.name for s in reg.select(q, min_score=hi)}
            assert high_set <= low_set, (q, hi, lo)
