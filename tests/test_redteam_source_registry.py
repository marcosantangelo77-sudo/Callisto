"""RED TEAM — source registry & query builders (rotating pass, 2026-08-24).

Surface: the source registry + query authoring seam — unattacked ground
(none of the twelve prior passes covered it) and the layer the morning
report itself named as the weak link. Method: property-based sweep
(4,000 generated questions × 19 planners, invariant: build_plan must
never raise and must never emit a malformed plan) plus adversarial inputs.

Every test below is strict-xfail: it FAILS on today's code because the
defect is real. Flip each to passing by fixing production; do not weaken
the assertion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sources import query_builder as qb          # noqa: E402
from tools.sources.registry import get_source_registry  # noqa: E402


# ── S1: wikidata planner crashes on ordinary entity questions ────────────

@pytest.mark.xfail(strict=True, reason="S1: _plan_wikidata_concept sorts "
                        "(concept, hint-string) pairs by -p[1], negating a "
                        "str — TypeError on any question naming >=1 hint word")
@pytest.mark.parametrize("question", [
    "Which companies face supply chain risk",
    "What drugs treat hypertension",
    "Which country has the largest population",
])
def test_s1_wikidata_planner_must_not_crash(question):
    plan = qb.build_plan("wikidata", question)
    # crash above, or honest refusal — but NEVER an exception
    assert isinstance(plan, qb.PlanResult)


def test_s1_wikidata_crash_propagates_through_retrieve():
    """The same input kills IterativeRetriever.retrieve() end to end:
    engine._run_inner gathers leaf fetches with return_exceptions=True then
    `raise oc` — one leaf's wikidata question aborts the WHOLE run."""
    from tools.pipeline.retrieval import IterativeRetriever

    class Q:
        text = "graph queries who held office when, companies"
        question_id = "q-s1"
        evidence_requirements = None

    class Led:
        def record_tool_result(self, *a, **k): pass
        def record_gate_rejection(self, *a, **k): pass

    reg = get_source_registry()
    r = IterativeRetriever(registry=reg, ledger=Led(), transport=None,
                           max_rounds=1)
    with pytest.raises(TypeError):
        r.retrieve(Q(), "", min_independent=2)


# ── S2: registry/planner name drift — family 2 (fix landed in one copy) ──

@pytest.mark.xfail(strict=True, reason="S2a: sec_fulltext/cmefedfut/kalshi "
                   "registered but unknown to query authoring — 'unknown "
                   "source' skip lies about a live registry entry")
def test_s2_every_registry_source_is_known_to_the_planner():
    """'unknown source X' from build_plan is a lie when X IS registered:
    sec_fulltext/cmefedfut/kalshi get skipped with 'unknown source', so the
    deliberate SEC deferral note never fires and two live market sources
    are invisible to query authoring."""
    reg = get_source_registry()
    unknown = [n for n in reg.names()
               if n not in qb._KEYWORD_PLANNERS
               and n not in qb._HONEST_GAPS]
    assert unknown == [], (
        f"registry sources unknown to query authoring: {unknown}")


@pytest.mark.xfail(strict=True, reason="S2b: _HONEST_GAPS keys 'sec_fts'; "
                   "registry name is 'sec_fulltext' — gap never selected")
def test_s2_honest_gap_key_matches_registry_name():
    """_HONEST_GAPS keys 'sec_fts'; the spec's name is 'sec_fulltext'
    (sec_fts.py:27). The gap message can never be selected."""
    reg = get_source_registry()
    for gap_name in qb._HONEST_GAPS:
        assert reg.get(gap_name) is not None, (
            f"honest-gap key {gap_name!r} matches no registered source")


# ── S3: census window direction — family 6 (error direction unpinned) ────

@pytest.mark.xfail(strict=True, reason="S3: _plan_census takes years in "
                   "arrival order; '2023 to 2021' emits start>end")
def test_s3_census_window_never_inverted():
    r = qb._plan_census("housing starts 2023 to 2021")
    kw = r.queries[0].kwargs
    assert kw["start"] <= kw["end"], (kw["start"], kw["end"])


# ── S4: treasury filter pins the FIRST date, questions list latest last ──

@pytest.mark.xfail(strict=True, reason="S4: _plan_treasury re.search takes "
                   "the first date; '2019 through 2024-06-30' filters "
                   "record_date:gte:2024-06-30 — silently drops 5 years")
def test_s4_treasury_window_covers_question_range():
    r = qb._plan_treasury("national debt 2019 through 2024-06-30")
    filters = r.queries[0].kwargs.get("filters", "")
    # a gte-only filter must use the EARLIEST date mentioned
    assert "record_date:gte:2019" in filters, filters


# ── S5: FDIC proper-noun extraction captures non-bank entities ──────────

@pytest.mark.xfail(strict=True, reason="S5: _plan_fdic grabs the longest "
                   "proper noun regardless of whether a BANK is named")
def test_s5_fdic_does_not_query_a_person_as_an_institution():
    r = qb._plan_fdic(
        "Compare Janet Yellen statements regarding Bank of America")
    if r.plannable:
        assert "Yellen" not in r.queries[0].kwargs.get("filters", "")


# ── S6: wayback bare-domain regex fabricates URLs from prose ─────────────

@pytest.mark.xfail(strict=True, reason="S6: bare-domain fallback matches "
                   "'e.g. 3.5.org' -> https://5.org and fetches it")
def test_s6_wayback_bare_domain_requires_dotted_domain_shape():
    r = qb._plan_wayback("compare e.g. 3.5.org")
    if r.plannable:
        url = r.queries[0].kwargs["url"]
        assert url != "https://5.org"


# ── S7: dead validation regexes — family 1 residue ───────────────────────

@pytest.mark.xfail(strict=True, reason="S7: five compiled validators defined "
                   "and documented but referenced nowhere")
def test_s7_module_level_id_regexes_are_alive_or_gone():
    """Five compiled regexes (_FRED_ID_RE, _BLS_ID_RE, _CIK_RE,
    _WB_INDICATOR_RE, _TREASURY_DATASET_RE) are defined with docstring
    claims ('exact series ids pass through untouched') but referenced
    nowhere. Either they guard something or they are removed."""
    import inspect
    src = inspect.getsource(qb)
    dead = []
    for name in ("_FRED_ID_RE", "_BLS_ID_RE", "_CIK_RE",
                 "_WB_INDICATOR_RE", "_TREASURY_DATASET_RE"):
        if hasattr(qb, name) and src.count(name) < 2:
            dead.append(name)
    assert not dead, f"defined-but-unused validators: {dead}"


# ── S8: property sweep — build_plan total over adversarial inputs ────────

_ADVERSARIAL = [
    "", "   ", "???", "?!", "\u0000", "🎯" * 50, "a" * 10_000,
    "What is it?", "GDP?", "naïve café unemployment",
    "Fetch Ｍ２ＳＬ data", "M2SĹ series", "what happened in 2024",
    "unemployment rate 2024-01-01 through 2025",
    'news coverage of "flash crash" events', "news coverage of O'Brien trial",
    "v2/debt/mspd/mspd_table_1 v2/debt/mspd/mspd_table_1",
    "Q42 Q99 Q7", "2023-06-15 2019-01-01", "https://x.com/a?q=1&z=2, 2019-03",
]


@pytest.mark.parametrize("question", _ADVERSARIAL)
def test_s8_build_plan_total_over_adversarial_inputs(question):
    for name in sorted(qb._KEYWORD_PLANNERS):
        try:
            plan = qb.build_plan(name, question)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"build_plan({name!r}, {question[:40]!r}) raised {e!r}")
        assert isinstance(plan, qb.PlanResult)


# ── S9: resolution passthrough cannot fire on non-id vocabulary ─────────

@pytest.mark.xfail(strict=True, reason="S9: _resolve passthrough accepts "
                   "any fully-uppercase known-id token ANYWHERE, so 'is GDP "
                   "higher than CPI' resolves to GDP instead of candidates")
def test_s9_bare_concept_word_is_not_treated_as_series_id():
    resolved, cands = qb._resolve("series_id", "is GDP higher than CPI",
                                  qb._FRED_CONCEPTS)
    # 'GDP' here is the concept WORD, not a supplied series id; auto-
    # resolving it skips disambiguation entirely (GDP vs GDPC1/GDP nominal).
    assert resolved == {} and "series_id" not in resolved
