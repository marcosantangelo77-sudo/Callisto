"""RED TEAM — source registry, query builders, and the health seam.

Surface: tools/sources/{registry,query_builder,base,health}.py plus the
ingestion gate that consumes their output (tools/pipeline/retrieval.py).
Method: property-based sweep over the selection/planning parameter space
(first use on this surface), plus corrupt-envelope replay at the gate and
one mutation probe on the health layer.

FAILING tests demonstrate defects on master. Tests named *_pin are honest
negatives — attacks that did NOT land — kept as regression pins.

Run: python3 -m pytest tests/test_redteam_source_registry.py -q
"""
import json

import pytest

from tools.sources.base import SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry, _tokens


# ── helpers ────────────────────────────────────────────────────────────────

class _Q:
    def __init__(self, text, qid="rt", min_indep=2):
        from agp.research_program import EvidenceRequirement
        self.question_id = qid
        self.text = text
        self.evidence_requirements = EvidenceRequirement(
            min_independent_sources=min_indep)


def _routed_transport(routes):
    """(url) -> 200 + body by host substring; 404 otherwise."""
    def transport(url, headers):
        for host, body in routes.items():
            if host in url:
                return 200, body if isinstance(body, str) else json.dumps(body)
        return 404, "not found"
    return transport


# ── SRC1 (CRITICAL): a lying source's 200-error envelope becomes evidence ──

class TestLyingSourceAdmission:
    """Family #3: absence treated as success. A source that answers HTTP 200
    with an ERROR envelope echoing the question words is admitted by the
    relevance gate, counted as an INDEPENDENT voice, and can declare the
    leaf's evidence requirement satisfied. The BLS body below is not even a
    lie — it is BLS v2's REAL shape for an invalid series request."""

    HOSTS = {"api.stlouisfed.org": None, "api.bls.gov": None}

    def _routes(self):
        return {
            "api.stlouisfed.org": json.dumps({
                "error_code": 404,
                "error_message":
                    "series not found: unemployment rate data unavailable",
            }),
            "api.bls.gov": json.dumps({
                "status": "REQUEST_NOT_PROCESSED",
                "responseTime": 6,
                "message":
                    ["no data found for unemployment rate series id"],
            }),
        }

    def test_error_envelopes_are_not_admissible_evidence(self,
                                                         monkeypatch):
        monkeypatch.setenv("CALLISTO_FRED_API_KEY", "test-key")
        from tools.pipeline.retrieval import IterativeRetriever
        from tools.sources.registry import get_source_registry

        class _Ledger:
            recorded = []
            def record_tool_result(self, *a, **k):
                self.recorded.append((a, k))

        r = IterativeRetriever(
            registry=get_source_registry(), ledger=_Ledger(),
            transport=_routed_transport(self._routes()),
            max_rounds=2, max_sources_per_leaf=4)
        trace = r.retrieve(_Q("What is the unemployment rate trend?"),
                           "macroeconomic time series", min_independent=2)
        # Both fetches carry ZERO domain rows; neither may be admitted.
        assert trace.admitted == [], (
            f"error envelopes admitted as evidence: "
            f"{[f.source_name for f in trace.admitted]}")

    def test_two_lying_sources_cannot_declare_sufficiency(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_FRED_API_KEY", "test-key")
        from tools.pipeline.retrieval import IterativeRetriever
        from tools.sources.registry import get_source_registry

        class _Ledger:
            def record_tool_result(self, *a, **k):
                pass

        r = IterativeRetriever(
            registry=get_source_registry(), ledger=_Ledger(),
            transport=_routed_transport(self._routes()),
            max_rounds=2, max_sources_per_leaf=4)
        trace = r.retrieve(_Q("What is the unemployment rate trend?"),
                           "macroeconomic time series", min_independent=2)
        assert "sufficient" not in trace.stop_reason, trace.stop_reason
        assert len(trace.independent_keys) < 2, sorted(trace.independent_keys)


# ── SRC2 (CRITICAL): reseller fan-out manufactures fake independence ──────

class TestResellerIndependence:
    """The registry's own planners route ONE underlying statistic through
    several adapters (FRED resells BLS CPS data; FRED/BEA/World Bank all
    serve BEA national accounts). Each host counts as its own independent
    voice, so min_independent_sources is satisfiable by one measurement."""

    def _plan_targets(self, question, sources):
        from tools.sources import query_builder as qb
        reg = SourceRegistry()
        out = []
        for name in sources:
            plan = qb.build_plan(name, question)
            if plan.plannable and plan.queries:
                out.append(name)
        return out

    def _keys_for(self, question, sources):
        from tools.pipeline.retrieval import independence_key
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        keys = set()
        for name in self._plan_targets(question, sources):
            spec = reg.get(name).spec
            plan_keys = {independence_key(name, spec.base_url)}
            keys |= plan_keys
        return keys

    def test_fred_and_bls_unemployment_are_one_voice(self):
        # UNRATE (FRED) IS LNS14000000 (BLS): the same CPS series.
        keys = self._keys_for("What is the unemployment rate doing?",
                              ("fred", "bls"))
        assert len(keys) == 1, (
            f"same statistic counted as {len(keys)} independent voices: "
            f"{sorted(keys)}")

    def test_gdp_resellers_are_not_three_voices(self):
        # fred GDPC1/GDP, bea NIPA tables, worldbank NY.GDP.MKTP.CD all
        # republish the US national accounts.
        keys = self._keys_for("Is GDP growing?", ("fred", "bea", "worldbank"))
        assert len(keys) == 1, (
            f"one national-accounts figure counted as "
            f"{len(keys)} voices: {sorted(keys)}")


# ── SRC3 (HIGH): health probes keyed to names that do not exist ───────────

class TestHealthProbeWiring:
    """The health module exists because live breakage hid behind passing
    fixtures. Its probe table is keyed 'cftc', 'sec_fts' and
    'semantic_scholar'; the registry registers 'cftc_cot', 'sec_fulltext'
    and 'semanticscholar'. Those probes crash before any verdict, and the
    real names report 'no health probe defined' forever."""

    def test_every_registered_source_has_a_probe(self):
        from tools.sources import health
        from tools.sources.registry import get_source_registry
        missing = set(get_source_registry().names()) - set(health.PROBES)
        assert not missing, f"registered sources with no probe: {sorted(missing)}"

    def test_every_probe_key_resolves_in_the_registry(self):
        from tools.sources import health
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        stale = [k for k in health.PROBES if reg.get(k) is None]
        assert not stale, f"probes naming unregistered sources: {sorted(stale)}"

    def test_probe_build_succeeds_for_all_keys(self):
        from tools.sources import health
        from tools.sources.registry import get_source_registry
        broken = [k for k in health.PROBES
                  if health._build(k) is None]
        assert not broken, f"_build returns None (probe can never run): {broken}"


# ── SRC4 (HIGH): the USPTO probe cannot report zero rows ──────────────────

class TestUsptoProbeZeroRows:
    """count_of = int(d.get('count') or d.get('total') or 1) — zero is
    falsy, so a 200-with-zero-results reports row_count>=1 and DEGRADED is
    unreachable. Family #3 reborn inside the layer built to catch it."""

    def test_zero_result_search_verdicts_degraded(self, monkeypatch):
        from tools.sources import health

        class _FakeSrc:
            def build_url(self, path, params=None):
                return f"https://fake.test{path}"

        class _FakeAdapter:
            def search_applications(self, query, offset=0, limit=25):
                return {"total": 0, "count": 0}   # honest empty result

        monkeypatch.setattr(health, "_build",
                            lambda name: (_FakeSrc(), _FakeAdapter()))
        res = health.PROBES["uspto_odp"][1]()
        assert res.row_count == 0, f"phantom row count: {res.row_count}"
        assert res.verdict == health.DEGRADED, (
            f"zero-results verdict {res.verdict}; DEGRADED unreachable")


# ── SRC5 (HIGH): quarter tokens hijack entity resolution ──────────────────

class TestEntityResolutionHijack:
    """_resolve passes ANY fully-uppercase token containing a digit through
    as an identifier BEFORE consulting curated concepts, then returns
    immediately. Ordinary macro phrasing ('...in Q2?') plans a fetch of
    FRED series 'Q2' and never considers inflation."""

    @pytest.mark.parametrize("question,forbidden", [
        ("What happens to inflation in Q2?", {"Q2"}),
        ("Is unemployment falling this year per Q1 data?", {"Q1"}),
    ])
    def test_calendar_quarters_do_not_become_series_ids(self,
                                                        question,
                                                        forbidden):
        from tools.sources import query_builder as qb
        plan = qb.build_plan("fred", question)
        assert plan.plannable and plan.queries, plan.reason
        sid = str(plan.queries[0].kwargs.get("series_id", "")).upper()
        assert sid not in forbidden, (
            f"calendar token {sid!r} hijacked resolution; planned fetch of "
            f"a nonexistent series instead of the question's concept")
        # ...and whatever it resolved must be a real curated id or an
        # explicit candidate list, never an invented one.
        known = {"UNRATE", "CIVPART", "CPIAUCSL", "CPILFESL", "PCEPI",
                 "GDPC1", "GDP", "DFF", "FEDFUNDS"}
        assert sid in known or not sid, f"invented series id {sid!r}"


# ── SRC7 (MEDIUM): gate-rejected bytes still mint PRIMARY ledger records ──

class TestRejectedBytesStillMintLedgerRecords:
    """RestSource._record runs inside get(), BEFORE the relevance gate.
    Bytes the system itself refuses still enter the ledger as primary tool
    results with urls — feeding the citation-laundering rule documented in
    redteam_confidence F4 with evidence deemed inadmissible."""

    def test_gate_rejection_leaves_no_primary_ledger_record(self):
        from tools.pipeline.retrieval import IterativeRetriever

        reg = SourceRegistry()
        spec = SourceSpec(
            name="faketest_src", base_url="https://faketest.example.com",
            description="macro data", answers=("unemployment data",),
            tier=1)

        class _Adapter:
            def __init__(self, src):
                self.src = src
            def works_search(self, **kw):
                return {"articles": [{"title": "pancake recipes"}]}

        reg.register(SourceAdapter(spec=spec, make_adapter=_Adapter))

        recorded = []

        class _Ledger:
            def record_tool_result(self, *a, **k):
                recorded.append((a[0] if a else "", k))

        from tools.sources import query_builder as qb

        def _plan(question_text):
            return qb.PlanResult(True, queries=[qb.PlannedQuery(
                source="faketest_src", method="works_search",
                kwargs={"query": question_text})])

        r = IterativeRetriever(registry=reg, ledger=_Ledger(),
                               transport=_routed_transport({
                                   "faketest.example.com": json.dumps(
                                       {"articles": [
                                           {"title": "pancake recipes"}]})}),
                               max_rounds=1, max_sources_per_leaf=2)
        trace = r.retrieve(_Q("What is the unemployment rate trend?"),
                           "unemployment data", min_independent=1)
        assert trace.rejected, "gate should reject irrelevant content"
        assert recorded == [], (
            f"gate-rejected bytes minted {len(recorded)} primary ledger "
            f"records; laundering input survives rejection")


# ── SRC8 (MEDIUM): the adapter-layer membership rule is unnormalized ──────

class TestMembershipRuleCopiesDisagree:
    """tools/sources/base.py::independence_family matches members RAW while
    retrieval.in_family normalises spelling. Same rule, two copies, two
    answers — PATTERNS family #2, fourth landing."""

    def test_both_copies_agree_on_spelling_drift(self):
        from tools.pipeline.retrieval import in_family
        from tools.sources.base import INDEPENDENCE_FAMILIES as fams
        from tools.sources.base import independence_family

        for spelling in ("semanticscholar", "semantic_scholar",
                         "Semantic-Scholar"):
            normalized = any(in_family(spelling, members)
                             for members in fams.values())
            raw = independence_family(spelling)
            raw_collapsed = raw != spelling   # collapsed => found a family
            assert normalized == raw_collapsed, (
                f"membership rule disagrees for {spelling!r}: normalized="
                f"{normalized}, base.py says family={raw!r}")


# ══ HONEST NEGATIVES — attacks that did NOT land (regression pins) ════════

class TestSelectionPins:
    """Property sweeps over selection. These held; kept so they keep holding."""

    def test_pin_morning_report_cases_select_obvious_sources(self):
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()

        def included(q):
            return {d.name for d in reg.select_explained(q) if d.included}

        assert "openalex" in included("scholarly works")
        assert {"openalex", "semanticscholar"} & included("papers")
        assert {"fred", "bls"} & included("unemployment rate")
        assert "clinicaltrials" in included("clinical trials")
        assert "fred" in included("economic time series")
        assert "openalex" in included(
            "scholarly literature about semiconductor supply chains")

    def test_pin_self_vocabulary_completeness_sweep(self):
        """A question drawn ENTIRELY from a source's own answer clauses
        must select that source — a source that cannot find itself is
        broken worse than any external vocabulary could be."""
        import random
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        rng = random.Random(7)
        checked = 0
        for name in reg.names():
            vocab = sorted({w for cl in reg.get(name).spec.answers
                            for w in _tokens(cl)})
            if len(vocab) < 2:
                continue
            for k in (1, 2, 3):
                for _ in range(10):
                    q = " ".join(rng.sample(vocab, min(k, len(vocab))))
                    sel = {d.name for d in reg.select_explained(q)
                           if d.included}
                    assert name in sel, (
                        f"{name} cannot find itself via own vocabulary "
                        f"{q!r} -> {sorted(sel)}")
                    checked += 1
        assert checked > 100   # sweep must actually have swept

    def test_pin_min_score_monotonic_subset(self):
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        questions = ["unemployment rate", "gdp", "clinical trials",
                     "patent applications battery",
                     "yield curve interest rates"]
        for q in questions:
            lo = [d.name for d in reg.select_explained(q, min_score=0.34)
                  if d.included]
            hi = [d.name for d in reg.select_explained(q, min_score=0.99)
                  if d.included]
            assert set(hi) <= set(lo), (q, hi, lo)

    def test_pin_diagnostic_floor_does_not_defy_strict_caller(self):
        """The floor lifts a score to 0.5 but must never include at
        min_score above it (the code comments claim this — verify)."""
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        for q in ("unemployment rate", "interest rates", "trade flows"):
            strict = {d.name for d in reg.select_explained(q, min_score=0.51)
                      if d.included}
            loose = {d.name for d in reg.select_explained(q, min_score=0.34)
                     if d.included}
            # anything included strictly must genuinely score >= 0.51
            for d in reg.select_explained(q, min_score=0.51):
                if d.included:
                    assert d.score >= 0.51, (q, d.name, d.score)
            assert strict <= loose


# ── SRC10 (LOW): stopword-only questions fan out to nearly every source ───

class TestStopwordOnlyQuestionFanout:
    """select_explained falls back to judging raw q_words when EVERY
    topical word is a stopword ('core = [...] or q_words'). A degenerate
    decomposition emitting pure connectives then matches half of them
    against adapter boilerplate and includes 17/20 sources at the
    diagnostic floor."""

    def test_pure_connective_question_selects_nothing(self):
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        decisions = reg.select_explained("the of and")
        included = [d.name for d in decisions if d.included]
        assert not included, (
            f"stopword-only question selected {len(included)} sources: "
            f"{included}")


class TestFailClosedPins:
    """Empty and adversarial inputs fail closed across the seam."""

    def test_pin_empty_question_selects_nothing(self):
        from tools.sources.registry import get_source_registry
        reg = get_source_registry()
        for q in ("", "???", "\u00e9\u00e8\u00ea"):
            decisions = reg.select_explained(q)
            assert decisions, "every source still gets a decision"
            assert not any(d.included for d in decisions), q

    def test_pin_core_query_empty(self):
        from tools.sources.query_builder import core_query
        assert core_query("") == ""
        assert core_query("What about it?") == ""

    def test_pin_planners_refuse_empty_questions(self):
        from tools.sources import query_builder as qb
        for src in qb.plannable_sources():
            plan = qb.build_plan(src, "")
            assert isinstance(plan, qb.PlanResult)
            if plan.plannable:
                # plannable on empty input would send garbage queries
                raise AssertionError(f"{src} planned on an empty question")

    def test_pin_unknown_source_honest_gap(self):
        from tools.sources import query_builder as qb
        plan = qb.build_plan("no_such_source", "anything at all")
        assert not plan.plannable

    def test_pin_gate_rejects_empty_and_junk_payloads(self):
        from tools.pipeline.retrieval import RelevanceGate
        g = RelevanceGate(min_coverage=0.25)
        q, qt = "unemployment rate trend", "macro time series"
        for payload in ({}, [], "", None,
                        {"results": []},
                        {"articles": [{"title": "pancake"}]}):
            ok, cov, reason = g.judge(q, qt, payload)
            assert not ok, (payload, reason)

    def test_pin_census_style_header_only_classifies_degraded(self):
        """When the counter works (census rows), zero rows DO degrade —
        proving the classification logic itself is sound where wired."""
        from tools.sources.health import DEGRADED, ProbeResult, _finish
        res = ProbeResult("census")
        res = _finish(res, {"rows": []},
                      lambda d: len(d.get("rows", [])),
                      lambda d: "")
        assert res.verdict == DEGRADED and res.row_count == 0
