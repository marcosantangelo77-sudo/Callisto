"""RED TEAM — source registry, query authoring, adapter ingestion seam.

Surface: tools/sources/{registry,base,query_builder,gdelt,bea}.py and the
ingestion seam in tools/pipeline/retrieval.py that turns fetched bodies into
admitted evidence. Method: corrupt-one-field replay over recorded response
shapes (the codebase's own live-probe shapes from tools/sources/health.py),
plus deterministic repros for every defect found.

Defects pinned here (all FAIL against the pre-fix tree; fixing agents flip
them green one by one):

  S1  CRITICAL  zero-result bodies admitted as evidence via metadata echo;
                two unrelated hosts satisfy min_independent_sources with
                bodies containing ZERO results ("sufficient" on empties)
  S2  HIGH      classify_fetch_failure is a BLS-only chokepoint (Family 2:
                the D2 fix half-landed) — health.py's own probe table
                documents BEA BEAAPIs.Error and Socrata/CFTC 200-error
                envelopes; both sail through the retrieval path as data
  S3  HIGH      _RateLimiter is per-INSTANCE despite the docstring claim
                "shared per source per process"; no production caller
                passes one — parallel leaves/rounds multiply request rates
                past every self-imposed politeness limit
  S4  MEDIUM-HIGH provenance binds fetched bytes to the REQUESTED url;
                urllib silently follows redirects, so a redirect to
                attacker-controlled bytes mints PRIMARY provenance under
                the real API host. resp.geturl() is never consulted.
  S5  MEDIUM    entity resolution precedence: an uppercase-token bypass
                overrides the curated table ("GDP growth..." resolves to
                the NOMINAL level series GDP over GDPC1), and
                longest-concept-wins picks the wrong subject on
                multi-topic questions (gdp growth + inflation -> CPI)
  S6  LOW-MED   translate_question_type's near-tie set is iteration-order
                dependent, violating its own ">= 0.9 * best" contract
  S7  LOW       base.independence_family() has ZERO callers and no name
                normalisation — a dead duplicate of retrieval.in_family
                that will drift exactly like why.py did

Honest negatives (attacks that did NOT land) are kept as passing pins at
the bottom, including a seeded corrupt-one-field sweep asserting the gate's
text path is corruption-stable.

No test in this file opens a socket: transports are injected or urlopen is
monkeypatched and restored.
"""

import json
import time

import pytest

from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    numeric_window_matches,
)
from tools.sources.base import RestSource, SourceSpec
from tools.sources.query_builder import (
    build_plan,
    classify_fetch_failure,
)


# ── fixtures ──────────────────────────────────────────────────────────────

QUESTION = "What do recent federal rules say about semiconductor supply chain resilience?"

# Recorded-shape bodies: zero results, but metadata echoing the question's
# topical vocabulary (query echoes / condition echoes are real API shapes).
OPENALEX_EMPTY_ECHO = json.dumps({
    "meta": {"query": "federal rules semiconductor supply chain resilience",
             "count": 0},
    "results": [],
})
FEDREGISTER_EMPTY_ECHO = json.dumps({
    "count": 0,
    "results": [],
    "conditions": {"term": "federal rules semiconductor supply chain "
                           "resilience"},
})

# health.py's own live-probe shapes for 200-with-error-envelope sources
BEA_ERROR_BODY = json.dumps({
    "BEAAPIs": {"Error": [{"ErrorDetail": {
        "Type": "INVALID_PARAMETER",
        "Description": "TableName GROSSOUTPUT invalid for DataSetName "
                       "GDPBYINDUSTRY; see gross output industry catalogue",
    }}]},
})
SOCRATA_ERROR_BODY = json.dumps({
    "error": True,
    "message": "query.soql.datasetId : Invalid dataset id "
               "cftc_contract_market_code='088691' gold futures",
})


class FakeQuestion:
    question_id = "q_redteam"
    text = QUESTION
    evidence_requirements = None


class NullLedger:
    def record_tool_result(self, *a, **k):
        pass

    def record_gate_rejection(self, *a, **k):
        pass


def host_transport(bodies_by_host):
    def transport(url, headers):
        for host, body in bodies_by_host.items():
            if host in url:
                return 200, body
        return 404, "{}"
    return transport


def retrieve_over(registry, question, transport, min_independent=2,
                  max_rounds=3):
    r = IterativeRetriever(registry=registry, ledger=NullLedger(),
                           transport=transport, adaptive_gain=False,
                           max_rounds=max_rounds)
    return r.retrieve(question, question.question_id and "rules",
                      min_independent=min_independent)


RESULT_ARRAYS = {  # where each adapter keeps its actual result items
    "openalex": ("results",),
    "federalregister": ("results",),
}


def n_result_items(fetch):
    parsed = fetch.parsed
    keys = RESULT_ARRAYS.get(fetch.source_name)
    if not keys:
        return None
    arr = parsed.get(keys[0]) if isinstance(parsed, dict) else None
    return len(arr) if isinstance(arr, list) else None


@pytest.fixture(scope="module")
def registry():
    from tools.sources.registry import get_source_registry
    return get_source_registry()


# ── S1 CRITICAL: zero-result evidence & sufficiency on empties ────────────

class TestS1ZeroResultAdmission:
    def test_gate_admits_zero_result_metadata_echo(self):
        """A body with count=0 and an empty results array must never be
        admissible — yet the gate scores its METADATA echo at 80% coverage."""
        gate = RelevanceGate()
        body = json.loads(OPENALEX_EMPTY_ECHO)
        ok, cov, reason = gate.judge(
            "What does recent research say about semiconductor supply "
            "chain resilience?", "scholarly work search", body)
        assert not ok, (
            f"zero-result body admitted at coverage {cov:.0%}: {reason}")

    def test_sufficiency_declared_on_two_zero_result_hosts(
            self, registry):
        """THE HEADLINE: openalex (family scholarly-aggregator) plus
        federalregister (own host) = two 'independent voices', both bodies
        containing ZERO results — and the loop declares itself sufficient."""
        t = host_transport({
            "api.openalex.org": OPENALEX_EMPTY_ECHO,
            "federalregister.gov": FEDREGISTER_EMPTY_ECHO,
        })
        trace = retrieve_over(registry, FakeQuestion(), t,
                              min_independent=2)
        assert not trace.stop_reason.startswith("sufficient"), (
            f"sufficiency declared on empty evidence: {trace.stop_reason}; "
            f"admitted={[f.body[:80] for f in trace.admitted]}")
        # ...and nothing admitted may be a zero-result body:
        for f in trace.admitted:
            n = n_result_items(f)
            assert n, f"{f.source_name} admitted a body with {n} results"

    def test_refinement_refetches_empty_bodies(self, registry):
        """The refine loop re-fetches the SAME empties: echoed metadata
        feeds relevant_titles/refinement, so rounds burn budget re-admitting
        nothing. Admissions must never grow across rounds for empty hosts."""
        t = host_transport({"api.openalex.org": OPENALEX_EMPTY_ECHO})
        q = FakeQuestion()
        q.text = ("What does recent research say about semiconductor "
                  "supply chain resilience?")
        trace = retrieve_over(registry, q, host_transport({
            "api.openalex.org": json.dumps({
                "meta": {"query": "semiconductor supply chain resilience "
                                  "research scholarly", "count": 0},
                "results": []}),
        }), min_independent=1, max_rounds=3)
        assert trace.n_admitted <= 1, (
            f"{trace.n_admitted} admissions across rounds, all of them "
            f"zero-result bodies")

    def test_numeric_route_admits_empty_envelope_with_date(self):
        """The strict structural route exists to admit REAL data windows;
        it also admits a body whose only content is total=0 plus one year
        matching the question — an empty envelope with a date."""
        vacuous = {"total": 0, "page": 1, "date": "2023"}
        assert not numeric_window_matches("unemployment in 2023", vacuous), (
            "numeric window route admitted a body carrying zero results")


# ── S2 HIGH: the BLS-only error-envelope chokepoint ───────────────────────

class TestS2EnvelopeChokepoint:
    def test_classify_failure_covers_bea(self):
        """health.py's own BEA probe treats BEAAPIs.Error as an error
        payload; the retrieval path's classifier does not."""
        assert classify_fetch_failure("bea", json.loads(BEA_ERROR_BODY)), (
            "BEA 200-error-envelope classified as ordinary data")

    def test_classify_failure_covers_socrata_cftc(self):
        """Same for the Socrata/CFTC shape health.py detects."""
        assert classify_fetch_failure("cftc_cot",
                                      json.loads(SOCRATA_ERROR_BODY)), (
            "Socrata/CFTC 200-error-envelope classified as ordinary data")

    def test_bea_error_envelope_admitted_end_to_end(
            self, registry, monkeypatch):
        """End to end: the planner authors a BEA call, the API answers 200
        with an error envelope whose Description names the very parameters
        the question authored, the chokepoint lets it through, and the gate
        ADMITS it as evidence."""
        monkeypatch.setenv("CALLISTO_BEA_API_KEY", "test-key")
        plan = build_plan("bea", "What does the gross output data say "
                                 "about gdp by industry?")
        assert plan.plannable, plan.reason
        t = host_transport({"bea.gov": BEA_ERROR_BODY})
        q = FakeQuestion()
        q.text = "What does the gross output data say about gdp by industry?"
        trace = retrieve_over(registry, q, t, min_independent=1)
        assert trace.n_admitted == 0, (
            f"BEA error envelope admitted as evidence: "
            f"{[f.body[:100] for f in trace.admitted]}")


# ── S3 HIGH: politeness limiter is per-instance ───────────────────────────

class TestS3LimiterNotShared:
    SPEC = SourceSpec(name="gdelt_test", base_url="https://g.example",
                      description="d", min_interval_s=0.4)

    def test_two_instances_share_no_politeness(self):
        """Docstring: 'Thread-safe minimum-interval limiter shared per
        source per process.' Two instances of ONE spec must therefore wait
        the interval between their requests. They do not: no production
        caller passes _limiter, so parallel leaves and successive rounds
        multiply the request rate past the declared ceiling."""
        hits = []

        def transport(url, headers):
            hits.append(time.monotonic())
            return 200, "{}"

        s1 = RestSource(self.SPEC, transport=transport)
        s2 = RestSource(self.SPEC, transport=transport)
        s1.get("https://g.example/a")
        s2.get("https://g.example/b")
        gap = hits[1] - hits[0]
        assert gap >= self.SPEC.min_interval_s - 0.05, (
            f"two instances of one spec fired {gap*1000:.0f}ms apart; "
            f"declared minimum interval {self.SPEC.min_interval_s}s")

    def test_same_instance_does_wait(self):
        """Contrast pin: within ONE instance the limiter works — proving
        the defect is sharing, not the limiter itself."""
        hits = []

        def transport(url, headers):
            hits.append(time.monotonic())
            return 200, "{}"

        s = RestSource(self.SPEC, transport=transport)
        s.get("https://g.example/a")
        s.get("https://g.example/b")
        assert hits[1] - hits[0] >= self.SPEC.min_interval_s - 0.05


# ── S4 MEDIUM-HIGH: redirect laundering of provenance ─────────────────────

class TestS4RedirectProvenance:
    def test_record_must_bind_bytes_to_final_url_after_redirect(
            self, monkeypatch):
        """urllib follows redirects transparently; FetchRecord records the
        REQUESTED url. A redirect to attacker bytes therefore mints
        PRIMARY provenance citing api.openalex.org. The record must carry
        the FINAL url the bytes actually came from."""
        import urllib.request

        REQUESTED = "https://api.openalex.org/works?search=x"
        FINAL = "https://evil.example/mirror/works"

        class FakeResp:
            status = 200
            body = b'{"attacker": "controlled"}'

            def geturl(self):
                return FINAL

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        captured = {}
        real_urlopen = urllib.request.urlopen

        def fake_urlopen(req, timeout=None, context=None):
            captured["requested"] = req.full_url
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        spec = SourceSpec(name="openalex", base_url="https://api.openalex.org",
                          description="d")
        src = RestSource(spec, ledger=None)
        status, body = src.get(REQUESTED)
        rec = src.last_record
        try:
            assert captured["requested"] == REQUESTED
            assert rec.url == FINAL, (
                f"provenance bound to requested {rec.url!r}; bytes came "
                f"from final {FINAL!r} — redirect launders attacker bytes "
                f"into PRIMARY provenance under the real host")
        finally:
            monkeypatch.setattr(urllib.request, "urlopen", real_urlopen)


# ── S5 MEDIUM: entity-resolution precedence ───────────────────────────────

class TestS5EntityResolutionPrecedence:
    def test_uppercase_bypass_overrides_curated_table(self):
        """"GDP" appears fully uppercase, so the exact-id bypass returns
        the NOMINAL level series (Candidate GDP, conf 0.85) before the
        curated table is ever consulted — GDPC1 (0.95) is unreachable for
        any natural question that spells GDP in caps. Confident nonsense,
        which this module's own docstring forbids."""
        plan = build_plan("fred", "How did GDP growth compare with "
                                  "inflation in 2023?")
        resolved = plan.resolved.get("series_id")
        assert resolved != "GDP", (
            f"'GDP growth' question resolved to nominal level series "
            f"{resolved!r}; table top candidate GDPC1 bypassed")

    def test_longest_concept_wins_picks_wrong_subject(self):
        """Multi-topic questions resolve to whichever concept STRING is
        longest, not to the subject: gdp growth vs inflation -> CPI."""
        plan = build_plan("fred", "How did gdp growth compare with "
                                  "inflation in 2023?")
        cands = plan.candidates.get("series_id") or []
        resolved = plan.resolved.get("series_id")
        picked = resolved or (cands[0].key if cands else "")
        assert str(picked).startswith(("GDPC1", "GDP")), (
            f"gdp-growth subject resolved toward {picked!r}")


# ── S6 LOW-MED: near-tie membership depends on iteration order ────────────

class TestS6NearTieOrderDependence:
    SPECS = [("s_low", 0.50), ("s_mid", 0.47), ("s_high", 0.51)]

    def _translated(self, order):
        from types import SimpleNamespace

        class FakeRegistry:
            def get(self, name):
                return None

            def select_explained(self, text):
                d = {n: SimpleNamespace(name=n, score=sc, included=True,
                                        spec=None)
                     for n, sc in self.SPECS}
                return [d[n] for n in order]

        from tools.pipeline.retrieval import translate_question_type
        FakeRegistry.SPECS = self.SPECS
        _, names = translate_question_type(FakeRegistry(),
                                           "capital of france", "x")
        return sorted(names)

    def test_near_tie_set_is_order_invariant(self):
        """Contract: sources scoring >= 0.9 * best belong in the candidate
        set. Membership must not depend on dict iteration order."""
        sets = [self._translated(o) for o in (
            ["s_low", "s_mid", "s_high"],
            ["s_high", "s_mid", "s_low"],
            ["s_mid", "s_low", "s_high"],
        )]
        assert len({tuple(s) for s in sets}) == 1, (
            f"near-tie membership is order-dependent: {sets}")


# ── S7 LOW: dead un-normalised duplicate of the family rule ───────────────

class TestS7DeadIndependenceHelper:
    def test_base_independence_family_matches_normalised_rule(self):
        """tools/sources/base.py:362 independence_family() has zero callers
        and does not normalise names, while retrieval.in_family() does —
        two copies of one rule already disagree ('semantic_scholar' vs
        'semanticscholar'). The copies must agree or one must go."""
        from tools.sources.base import INDEPENDENCE_FAMILIES
        from tools.sources.base import independence_family
        from tools.pipeline.retrieval import in_family

        members = INDEPENDENCE_FAMILIES["scholarly-aggregator"]
        for spelling in ("semanticscholar", "semantic_scholar",
                         "Semantic-Scholar"):
            via_base = independence_family(spelling)
            via_retrieval_collapses = any(
                in_family(spelling, members) for _ in [0])
            if via_retrieval_collapses:
                assert via_base == "scholarly-aggregator", (
                    f"base.independence_family({spelling!r}) -> "
                    f"{via_base!r} while retrieval.in_family collapses it: "
                    f"two membership rules disagree")


# ══ HONEST NEGATIVES — attacks that did NOT land (kept as pins) ═══════════

class TestHonestNegatives:
    BODY_OPENALEX_HIT = json.dumps({
        "meta": {"count": 1},
        "results": [{"title": "Semiconductor supply chain resilience: a "
                              "review of policy instruments",
                     "id": "W123"}],
    })
    BODY_FRED_OBS = json.dumps({
        "observations": [{"date": "2023-01-01", "value": "3.4"}],
    })

    def _corruptions(self, obj):
        """Every single-field corruption of a nested structure, from a
        fixed degradation alphabet (remove, blank, zero, empty containers,
        duplicate sibling). Yields (path, corrupted)."""
        alphabet = [None, "", 0, [], {}]

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    for c in alphabet:
                        d = json.loads(json.dumps(
                            {kk: vv for kk, vv in node.items()}))
                        d[k] = c
                        yield path + [k], d
                    yield from walk(v, path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node[:20]):
                    yield path + [i], json.loads(
                        json.dumps(node[:i] + node[i + 1:] or node))
                    yield from walk(v, path + [i])

        yield from walk(obj, [])

    def test_gate_text_path_is_corruption_stable(self):
        """PROPERTY (corrupt-one-field replay): degrading any single field
        of an admitted body can never RAISE coverage, and can never admit
        a previously rejected body. Sweeps every leaf of two recorded
        shapes; violations would mean the gate synthesises relevance from
        nowhere."""
        gate = RelevanceGate(min_coverage=0.25)
        question = ("What does recent research say about semiconductor "
                    "supply chain resilience?")
        qtype = "scholarly work search"
        cases = 0
        for body_s in (self.BODY_OPENALEX_HIT, self.BODY_FRED_OBS):
            body = json.loads(body_s)
            base_ok, base_cov, _ = gate.judge(question, qtype, body)
            for path, corrupted in self._corruptions(body):
                cases += 1
                ok, cov, _ = gate.judge(question, qtype, corrupted)
                assert cov <= base_cov + 1e-9, (
                    f"corrupting {path} raised coverage {base_cov:.2f} -> "
                    f"{cov:.2f}")
                if not base_ok:
                    assert not ok, (
                        f"corruption {path} admitted a rejected body")
        assert cases > 50, f"sweep degenerated ({cases} cases)"

    def test_in_family_normalisation_holds(self):
        from tools.pipeline.retrieval import in_family
        members = {"openalex", "semanticscholar"}
        assert in_family("Semantic_Scholar", members)
        assert in_family("semantic-scholar", members)
        assert not in_family("openalex_mirror", members)

    def test_diagnostic_floor_never_lowers_caller_min_score(self):
        from tools.sources.registry import SourceAdapter, SourceRegistry
        reg = SourceRegistry()
        reg.register(SourceAdapter(
            SourceSpec(name="lonely", base_url="https://x.example",
                       description="d", answers=("quantum dotometry",)),
            make_adapter=lambda s: None))
        specs = reg.select("quantum dotometry advances", min_score=0.99)
        assert specs == [], "diagnostic floor lowered an explicit min_score"

    def test_execute_stale_plan_raises_loudly(self):
        from tools.sources.query_builder import PlannedQuery, PlanResult, \
            execute
        plan = PlanResult(True, queries=[PlannedQuery(
            source="x", method="method_that_does_not_exist")])
        with pytest.raises(AttributeError):
            execute(object(), plan)
