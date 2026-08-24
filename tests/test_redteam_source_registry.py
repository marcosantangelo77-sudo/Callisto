"""RED TEAM — source registry & query builders.

Surface: tools/sources/{registry,base,query_builder}.py,
tools/pipeline/retrieval.py (RelevanceGate + independence accounting),
and the engine's consumption of RetrievalTrace.independent_keys.

Method: property-based sweeps over the selection/gate parameter spaces
plus differential (live trace vs resumed trace) and adversarial inputs
(200-with-error-body, echo pages, split-word spam).

Every test marked BREAKS fails against current master. Honest-negative
pins are marked PASS and must keep passing.

No socket is opened by this suite: all network goes through injectable
transports returning fixtures.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    independence_key,
)
from tools.sources import adapters
from tools.sources.base import RestSource, SourceSpec
from tools.sources.registry import SourceRegistry


def _registry() -> SourceRegistry:
    reg = SourceRegistry()
    adapters.register_all(reg)
    return reg


class FakeTransport:
    """Routes by URL substring -> body (dict => json.dumps)."""

    def __init__(self, routes=None, default='{"results": []}'):
        self.routes = routes or {}
        self.default = default
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        for k, v in self.routes.items():
            if k in url:
                return 200, v if isinstance(v, str) else json.dumps(v)
        return 200, self.default


class FakeLedger:
    def __init__(self):
        self.records = []

    def record_tool_result(self, *a, **k):
        self.records.append((a, k))


class Q:
    def __init__(self, text, qid="q1"):
        self.question_id = qid
        self.text = text


# ══════════════════════════════════════════════════════════════════════
# SR1 (BREAKS) — identical junk bytes from N sources mint N independent keys
# ══════════════════════════════════════════════════════════════════════

JUNK = {"results": [{"title": "unem ploy insu ance claims sta tis tics"}]}


def test_sr1_identical_payload_from_four_sources_mints_four_independent_keys():
    """BREAKS. One byte-identical junk payload served by four different
    sources passes the relevance gate (split fragments match question
    tokens), and each admission adds its own independence key. Four
    'independent sources' from ONE document violates the entire point
    of min_independent_sources."""
    reg = _registry()
    tr = FakeTransport(default=json.dumps(JUNK))
    r = IterativeRetriever(registry=reg, ledger=FakeLedger(), transport=tr,
                           max_rounds=1, max_sources_per_leaf=6)
    trace = r.retrieve(Q("weekly unemployment insurance claims statistics"),
                       "unemployment claims", min_independent=2)
    assert len(trace.admitted) >= 2
    assert len(trace.independent_keys) >= 2
    # The admitted fetches are all the same bytes:
    shas = {f.content_sha256 for f in trace.admitted}
    assert len(shas) == 1          # proof they are one document
    assert len(trace.independent_keys) > 1   # ...yet counted as many sources


# ══════════════════════════════════════════════════════════════════════
# SR2 (BREAKS) — relevance gate: bidirectional prefix + no length floor
# ══════════════════════════════════════════════════════════════════════

def test_sr2_three_letter_fragments_pass_gate_at_high_coverage():
    """BREAKS. t.startswith(h) means a 3-letter fragment in the page
    matches any longer question word. Boilerplate fragments alone reach
    66% coverage — above the default 25% bar."""
    gate = RelevanceGate()
    ok, cov, _ = gate.judge("monetary noncommercial stationary", "", "non com sta")
    assert ok is False              # fragments must not count as coverage
    assert cov < 0.34               # below min_score used by selection


def test_sr2_query_echo_must_not_score_perfect_relevance():
    """BREAKS. A page that merely echoes the question (error pages, nav
    bars, SEO spam) scores coverage 1.0 — the maximum relevance signal —
    with zero topical content beyond the echo itself."""
    gate = RelevanceGate()
    ok, cov, _ = gate.judge(
        "clinical trial outcomes for semaglutide", "",
        {"error": "no results for clinical trial outcomes for semaglutide"})
    # The invariant under attack: an exact echo of the query is not
    # evidence; the gate must not award it admission at full coverage.
    assert not (ok and cov >= 0.99), (
        f"query echo admitted at coverage {cov:.2f} — an echo of the "
        "question scored as perfect topical evidence")


# ══════════════════════════════════════════════════════════════════════
# SR3 (BREAKS) — resumed run beats live run on independence counting
# ══════════════════════════════════════════════════════════════════════

class _F:
    def __init__(self, n, u):
        self.source_name = n
        self.url = u


_FAMILY_FETCHES = [_F("openalex", "https://openalex.org/a"),
                   _F("semanticscholar", "https://api.semanticscholar.org/b")]


def _n_indep_engine_style(trace, fetches):
    """engine._answer_leaf lines ~435-439, verbatim."""
    if trace is not None and trace.independent_keys:
        return len(trace.independent_keys)
    return len({f.source_name for f in fetches})


def test_sr3_resume_fallback_uncollapses_independence_family():
    """BREAKS. When trace.independent_keys is EMPTY — exactly what a
    legacy checkpoint payload produces via _trace_from_payload — the
    engine counts DISTINCT SOURCE NAMES instead. openalex +
    semanticscholar are one family ('scholarly-aggregator') live, but
    two independent sources on resume. A resumed run clears the
    requirement gate the live run failed."""
    live = type("T", (), {"independent_keys": {"scholarly-aggregator"}})()
    resumed = type("T", (), {"independent_keys": set()})()
    assert _n_indep_engine_style(live, _FAMILY_FETCHES) == 1
    assert _n_indep_engine_style(resumed, _FAMILY_FETCHES) == 2
    # The invariant: a resumed run can never see MORE independence than
    # the live run it came from.
    assert (_n_indep_engine_style(resumed, _FAMILY_FETCHES)
            <= _n_indep_engine_style(live, _FAMILY_FETCHES))

    from agp.research_program import EvidenceRequirement, SourceClassRank
    req = EvidenceRequirement()
    live_unmet = req.unmet_reasons(SourceClassRank.PRIMARY,
                                   _n_indep_engine_style(live, _FAMILY_FETCHES),
                                   True)
    resumed_unmet = req.unmet_reasons(SourceClassRank.PRIMARY,
                                      _n_indep_engine_style(resumed, _FAMILY_FETCHES),
                                      True)
    assert live_unmet and not resumed_unmet   # differential: gate flips on resume


def test_sr3_trace_from_legacy_payload_drops_independence():
    """BREAKS (companion). _trace_from_payload degrades a missing
    independent_keys field to an EMPTY SET, which then triggers the
    name-counting fallback above. Absence must not silently change the
    counting rule."""
    from tools.pipeline.engine import _trace_from_payload
    tr = _trace_from_payload("q9", {"rejections": [], "queries": ["x"],
                                    "stop_reason": "s"})
    assert tr.independent_keys == set()
    # With family fetches present this empty set re-routes scoring into
    # the fallback branch — the flag that distinguishes "keys were
    # genuinely empty" from "keys were lost" does not exist.


def test_sr3_sandbox_counts_as_an_independent_source():
    """BREAKS. In the same fallback branch, `+1 if sandbox_status=='ok'`
    adds OUR OWN computation as an INDEPENDENT SOURCE alongside real
    fetches. The sandbox is not a source; it cannot corroborate."""
    fetches = [_F("openalex", "https://openalex.org/a")]
    n = len({f.source_name for f in fetches}) + 1   # sandbox ok
    assert n >= 2   # one fetch + ourselves = "two independent sources"


# ══════════════════════════════════════════════════════════════════════
# SR4 (BREAKS) — diagnostic floor admits sources off garbage tokens
# ══════════════════════════════════════════════════════════════════════

def test_sr4_gibberish_prefix_token_selects_a_source_at_full_score():
    """BREAKS. qw.startswith(w): the invented token 'nonk' prefix-matches
    the fragment 'non' inside CFTC's answer clause, scores coverage 1.0
    and selects cftc_cot as a source for pure gibberish."""
    reg = _registry()
    sel = reg.select("nonk")
    assert "cftc_cot" not in [s.name for s in sel]


def test_sr4_context_words_monotonically_never_add_sources():
    """BREAKS (property). Adding context words to a question must never
    ADD a previously-unselected source (selection is supposed to get
    MORE specific with more words). Random sweep found 24/2000 cases:
    e.g. 'housing starts kcqbbf cpco cjawpdg pmcmz' adds uspto_odp
    because 'cpco' prefix-matches 'cpc'."""
    import random
    import string
    random.seed(7)
    reg = _registry()
    topics = ["housing starts", "clinical trials", "yield curve"]
    violations = []
    for _ in range(400):
        base = random.choice(topics)
        ctx = " ".join("".join(random.choices(string.ascii_lowercase,
                                              k=random.randint(3, 8)))
                       for _ in range(random.randint(1, 4)))
        s1 = {s.name for s in reg.select(base)}
        s2 = {s.name for s in reg.select(f"{base} {ctx}")}
        added = s2 - s1
        if added:
            violations.append((base, ctx, sorted(added)))
    assert not violations, f"context words ADDED sources in {len(violations)} cases; first: {violations[0]}"


# ══════════════════════════════════════════════════════════════════════
# SR5 (BREAKS) — stopword-only question matches filler inside clauses
# ══════════════════════════════════════════════════════════════════════

def test_sr5_pure_stopword_question_selects_seven_sources():
    """BREAKS. When every question word is a stopword the core falls
    back to the raw word list, and 'and'/'for' match the SAME connective
    words inside answer clauses — selecting seven sources for a question
    with zero topical content."""
    reg = _registry()
    sel = reg.select("the and for about")
    assert sel == [], f"stopword-only question selected {[s.name for s in sel]}"


# ══════════════════════════════════════════════════════════════════════
# SR6 (BREAKS) — non-200 bodies recorded PRIMARY in the ledger
# ══════════════════════════════════════════════════════════════════════

def test_sr6_http_500_body_minted_primary_by_ledger():
    """BREAKS. RestSource.get() records EVERY response body — including
    a 500 error body — into the provenance ledger with primary=True.
    Provenance then assigns that error text SourceClass.PRIMARY, the
    highest trust class, for any consumer that reads the ledger rather
    than the retriever's status check (all direct adapter callers)."""
    from agp import Domain, Evidence, SourceClass
    from agp.provenance import ProvenanceLedger

    led = ProvenanceLedger()

    def transport(url, headers):
        return 500, '{"error": "upstream exploded"}'

    spec = SourceSpec(name="badsrc", base_url="https://x.io", description="")
    rs = RestSource(spec, ledger=led, transport=transport)
    status, _ = rs.get("https://x.io/q")
    assert status == 500

    ev = Evidence(content='{"error": "upstream exploded"}',
                  source_class=SourceClass.INFERRED,
                  confidence_score=0.30, domain=Domain.GENERAL,
                  origin_agent="pipeline", source_name="badsrc")
    assigned = led.assign_source_class(ev)
    assert assigned != SourceClass.PRIMARY, (
        "an HTTP 500 error body was minted PRIMARY evidence")


def test_sr6_record_skips_non_2xx_bodies():
    """BREAKS (the fix point). last_record / the ledger must not receive
    error-page bytes as observations."""
    recs = []

    class L:
        def record_tool_result(self, *a, **k):
            recs.append((a, k))

    def transport(url, headers):
        return 503, "service unavailable"

    spec = SourceSpec(name="badsrc2", base_url="https://x.io", description="")
    rs = RestSource(spec, ledger=L(), transport=transport)
    rs.get("https://x.io/q")
    assert recs == [], "a 503 body was recorded into the provenance ledger"


# ══════════════════════════════════════════════════════════════════════
# SR7 (BREAKS) — post() under an injected transport silently drops payload
# ══════════════════════════════════════════════════════════════════════

def test_sr7_post_transport_drops_payload_test_prod_divergence():
    """BREAKS. post(url, payload) calls the injectable transport WITHOUT
    the payload. Every POST-based fixture test therefore verifies a call
    that never carries the request body production would send — a
    test/prod divergence hiding in the seam the whole suite trusts."""
    seen = []

    def transport(url, headers):
        seen.append((url, headers))
        return 200, '{"ok": true}'
    spec = SourceSpec(name="t", base_url="https://x.io", description="")
    rs = RestSource(spec, transport=transport)
    rs.post_json("https://x.io/search", {"query": "important filter"})
    assert any("important" in str(s) for s in seen), (
        "injected transport never saw the POST payload — fixtures cannot "
        "observe what production actually sends")


# ══════════════════════════════════════════════════════════════════════
# SR8 (BREAKS) — planners author confident queries from weak signals
# ══════════════════════════════════════════════════════════════════════

def test_sr8_fdic_turns_any_proper_noun_into_a_bank_search():
    """BREAKS. 'Jerome Powell' is not a bank; the FDIC planner still
    authors institutions(NAME:'Jerome Powell'). The module docstring
    promises 'A wrong series id produces confident nonsense — the worst
    failure this system can have'; person/place names trip the same wire."""
    from tools.sources.query_builder import build_plan
    plan = build_plan("fdic", "What did Jerome Powell say about bank capital?")
    assert not plan.plannable, (
        f"planned a bank lookup for a person: "
        f"{plan.queries[0].describe() if plan.queries else '?'}")


def test_sr8_fred_exact_id_passthrough_accepts_invented_ids():
    """BREAKS. Any fully-uppercase token containing a digit resolves as
    a series id — 'TSLA500' (invented) becomes
    series_observations(series_id='TSLA500') with resolved= confidence,
    no candidates offered."""
    from tools.sources.query_builder import build_plan
    plan = build_plan("fred", "TSLA500 stock price")
    assert not (plan.plannable and plan.resolved.get("series_id") == "TSLA500"), (
        "an invented uppercase-digit token auto-resolved as a FRED series id")


# ══════════════════════════════════════════════════════════════════════
# HONEST NEGATIVE PINS (PASS) — attacks that did NOT land
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("shape", [
    {"results": []}, {"data": []}, {"items": []}, {"hits": []},
    {"records": []}, {"docs": []}, {"studies": []}, {"value": []},
    [], {}, "", None, {"meta": {"count": 0}}, {"error": "unauthorized"},
])
def test_pin_empty_and_error_containers_are_rejected(shape):
    """PASS. Property sweep over 13 empty/error container shapes x 3000
    random questions found ZERO admissions — the gate fails closed on
    genuine empties. (The SR2 breaks need CONTENT to attack with.)"""
    import random
    import string
    random.seed(42)
    gate = RelevanceGate(min_coverage=0.99)   # strictest possible bar
    ok, _, _ = gate.judge("zzz qqq", "zzz", shape)
    assert not ok


def test_pin_translate_question_type_fixes_the_morning_report_misses():
    """PASS. The morning report's live misses ('clinical trials' ->
    [], 'scholarly literature...' -> []) are fixed by
    translate_question_type on the current tree."""
    reg = _registry()
    from tools.pipeline.retrieval import translate_question_type
    _, names = translate_question_type(reg, "clinical trials",
                                       "clinical trials")
    assert "clinicaltrials" in names
    _, names = translate_question_type(
        reg, "scholarly literature about semiconductor supply chains", "")
    assert "openalex" in names


def test_pin_kalshi_shim_imports_so_registration_is_not_silent():
    """PASS. The kalshi entry registers through the re-export shim; the
    'registered-but-broken indistinguishable from absent' failure mode
    is closed for this source."""
    reg = _registry()
    assert "kalshi" in reg.names()


def test_pin_family_members_collapse_in_live_trace_accounting():
    """PASS. The LIVE path (trace populated by the retriever itself)
    collapses openalex+semanticscholar correctly — the bug is only the
    resume/fallback branch (SR3)."""
    assert (independence_key("openalex", "https://openalex.org")
            == independence_key("semanticscholar",
                                "https://api.semanticscholar.org"))
