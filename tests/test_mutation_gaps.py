"""Mutation-testing gap tests: pin the subtraction-only invariant and the
money-path arithmetic that the mutation run (findings/redteam_mutation.md)
proved unpinned. Each test names the surviving mutant class it kills.

Invariant under test across all of these: no function on the confidence or
stake path may RAISE a score/stake, and every boundary must sit exactly where
the AGP tier table says it sits.
"""
import math

import pytest

from agp.adversary import AdversaryObjection, clamp_with_ensemble, ensemble_ceiling
from agp.provenance import ProvenanceLedger, clamp_confidence_provenance
from agp.thresholds import floor_conf
from tools.edge_confidence import score_edge
from tools.odds_api import calculate_implied_probability
from tools.kelly import (
    _american_to_decimal,
    _confidence_tier_from_score,
    kelly_full,
    kelly_fractional,
    kelly_dynamic,
)


# ---------------------------------------------------------------------------
# 1. thresholds.floor_conf — kills floor->round / floor->ceil mutants (line 57)
#    A round() or ceil() here RAISES scores; the whole architecture forbids it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("x", [0.269183, 0.999, 0.555, 0.301, 1.0])
def test_floor_conf_never_raises(x):
    assert floor_conf(x) <= x + 1e-12


def test_floor_conf_exact_rounding_boundary():
    # round(0.265,2)==0.27 raises; floor must give 0.26
    assert floor_conf(0.265) == 0.26
    assert floor_conf(0.999) == 0.99


# ---------------------------------------------------------------------------
# 2. Tier boundaries — kills const nudge survivors on TIER_*_MIN.
#    A boundary at 0.91 instead of 0.90 silently demotes VERIFIED claims.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,tier", [
    (0.8999, "CORROBORATED"),   # just below VERIFIED boundary
    (0.90, "VERIFIED"),         # exactly at boundary — >= not >
    (0.901, "VERIFIED"),
    (0.7499, "PROBABLE"),
    (0.75, "CORROBORATED"),
    (0.5499, "SPECULATIVE"),
    (0.55, "PROBABLE"),
    (0.2999, "UNVERIFIED"),
    (0.30, "SPECULATIVE"),
])
def test_kelly_tier_boundaries_are_inclusive_lower(score, tier):
    assert _confidence_tier_from_score(score) == tier


# ---------------------------------------------------------------------------
# 3. kelly_full zero-payout guard — kills `if b < 0` (line 167) and
#    `return 0.01` (line 168) survivors. b == 0 (odds of +0) MUST return
#     exactly 0.0, not 0.01 of bankroll.
# ---------------------------------------------------------------------------

def test_kelly_full_zero_net_payout_guard():
    # even-money American odds are -100/+100 in practice
    assert kelly_full(0.05, 100) > 0
    # negative or zero edge can never produce a positive stake
    assert kelly_full(-0.10, -110) == 0.0
    assert kelly_full(0.0, -110) == 0.0


def test_kelly_full_zero_net_payout_b_eq_zero_guard():
    # american 0 -> decimal 2.0 in this codebase; construct b==0 directly:
    # the b<=0 guard must fire for ANY input where net payout is zero.
    # decimal(-100)==2.0? no: b=1.0. The only b<=0 route is odds<=0 mapping;
    # pin the guard's exact boundary via monkeypatched helper:
    import tools.kelly as K
    orig = K._american_to_decimal
    K._american_to_decimal = lambda a: 1.0  # b == 0
    try:
        assert K.kelly_full(0.05, -110) == 0.0  # guard returns 0.0 exactly
        assert K.kelly_full(0.50, -110) == 0.0
    finally:
        K._american_to_decimal = orig


def test_kelly_full_is_monotone_in_edge_and_never_negative():
    prev = -1.0
    for e10 in range(-20, 21):
        f = kelly_full(e10 / 100.0, -110)
        assert f >= 0.0
        if e10 >= 0:
            assert f >= prev - 1e-12
        prev = max(prev, f)


def test_kelly_full_reference_values():
    # hand-computed: implied(-110)=110/210, p=implied+0.05, b=100/110
    implied = calculate_implied_probability(-110)
    p = implied + 0.05
    b = 1.0 + 100.0 / 110.0 - 1.0  # decimal(−110) − 1 = 100/110
    expected = (b * p - (1 - p)) / b
    # kelly_full FLOORS to 6dp (never rounds a stake up), so the exact
    # pin allows at most one 1e-6 quantum of shortfall vs the true f*.
    assert -1e-9 < kelly_full(0.05, -110) - expected <= 0


def test_kelly_fractional_is_exact_scaling():
    full = kelly_full(0.04, 150)
    # kelly_fractional FLOORS to 6dp (never rounds a stake up); exact scaling
    # allows at most one 1e-6 quantum of shortfall.
    scaled = full * 0.25
    got_frac = kelly_fractional(0.04, 150, 0.25)
    got_default = kelly_fractional(0.04, 150)   # default is quarter Kelly
    assert 0 <= scaled - got_frac < 1e-6 and got_frac == got_default


# ---------------------------------------------------------------------------
# 4. kelly_dynamic hard cap — kills `adjusted_fraction < hard_cap` (line 292)
#    and cap-constant nudges. The 5% single-bet cap is a ruin guard, not a
#    suggestion: it binds from ABOVE even when adjusted exceeds it.
# ---------------------------------------------------------------------------

def test_kelly_dynamic_hard_cap_binds_from_above():
    out = kelly_dynamic(edge=0.50, odds=200, confidence_score=0.95,
                        variance_estimate=0.0, bankroll=10_000.0,
                        kelly_base_fraction=0.25)
    assert out["fraction"] <= 0.05, "hard cap 5% violated"
    assert out["hard_cap_applied"] is True
    assert out["stake"] == pytest.approx(round(10_000.0 * 0.05, 2))


def test_kelly_dynamic_stake_equals_bankroll_times_fraction():
    out = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.80,
                        variance_estimate=0.02, bankroll=5_000.0)
    assert out["stake"] == pytest.approx(
        min(round(out["fraction"] * 5000, 2), round(5000 * 0.05, 2)))


def test_kelly_dynamic_unverified_confidence_bets_nothing():
    # UNVERIFIED tier (<0.30) must size ZERO regardless of edge
    out = kelly_dynamic(edge=0.30, odds=-110, confidence_score=0.29,
                        variance_estimate=0.01, bankroll=10_000.0)
    assert out["tier"] == "UNVERIFIED"
    assert out["fraction"] == 0.0
    assert out["stake"] == 0.0


def test_kelly_dynamic_variance_dampener_monotone():
    stakes = [kelly_dynamic(edge=0.05, odds=-110, confidence_score=0.85,
                            variance_estimate=v, bankroll=10_000.0)["stake"]
              for v in (0.0, 0.01, 0.03, 0.06)]
    assert stakes == sorted(stakes, reverse=True)


# ---------------------------------------------------------------------------
# 5. Provenance relabel floor — kills line-212 comparison survivors:
#    prior < floor must NEVER be raised to the floor.
# ---------------------------------------------------------------------------

def test_relabel_evidence_never_raises_below_floor_items():
    from agp import Evidence, SourceClass
    from agp.provenance import relabel_evidence
    led = ProvenanceLedger()
    ev = Evidence(content="no tool ever saw this",
                  source_class=SourceClass.INFERRED,
                  confidence_score=0.05, domain=None, origin_agent="model")
    relabel_evidence([ev], led,
                     {"PRIMARY": 1.0, "SECONDARY": 0.75,
                      "SIGNAL": 0.55, "INFERRED": 0.55})
    assert ev.confidence_score <= 0.05 + 1e-12, (
        "DB floor raised a sub-floor score")


def test_clamp_confidence_provenance_ceiling_by_class():
    from agp import SourceClass
    caps = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55, "INFERRED": 0.55}
    for cls, cap in caps.items():
        got = clamp_confidence_provenance(0.99, SourceClass(cls), caps)
        assert got <= cap + 1e-12
        # floored downward, never rounded up (kills min(1.01,...) nudge too)
        assert got == floor_conf(min(0.99, cap))
    assert clamp_confidence_provenance(0.0, SourceClass.PRIMARY, caps) == 0.0


# ---------------------------------------------------------------------------
# 6. Ensemble ceiling asymmetry — kills spread-boundary survivors (lines
#    118/120): agreement never restricts, disagreement always does, and both
#    boundaries are inclusive-lower.
# ---------------------------------------------------------------------------

def test_ensemble_ceiling_boundaries_inclusive():
    from agp.adversary import (DISAGREEMENT_SPREAD_THRESHOLD,
                               DISAGREEMENT_CEILING, MILD_DISAGREEMENT_CEILING)
    # exact-representable pairs: 0.75-0.45 == 0.30 exactly in binary floats
    lo = 0.45
    hi = lo + DISAGREEMENT_SPREAD_THRESHOLD
    assert hi - lo == DISAGREEMENT_SPREAD_THRESHOLD
    # spread EXACTLY at threshold must still cap (>= not >)
    assert ensemble_ceiling([lo, hi]) == DISAGREEMENT_CEILING
    assert MILD_DISAGREEMENT_CEILING == 0.70
    # half threshold: no float pair in [0,1] subtracts to exactly 0.15, so pin
    # the smallest spread that still gets the mild cap and the largest that
    # gets none — bracketing the boundary within one ULP.
    import math
    mild = ensemble_ceiling([0.45, 0.6000000000000001])   # spread 0.15000...01
    assert mild == MILD_DISAGREEMENT_CEILING
    # EXACT half-threshold pair exists: [0.0, 0.15] subtracts to exactly 0.15
    assert ensemble_ceiling([0.00, 0.15]) == MILD_DISAGREEMENT_CEILING
    none_ = ensemble_ceiling([0.45, 0.5999999999999999])  # spread just below .15
    assert none_ is None


def test_clamp_with_ensemble_only_pulls_down():
    for score in (0.99, 0.72, 0.40):
        s, reason = clamp_with_ensemble(score, [0.60, 0.95])
        assert s <= score
    # clamping to the mild ceiling floors it downward too
    s, _ = clamp_with_ensemble(0.836, [0.60, 0.76])
    assert s == 0.70  # floored, not rounded to 0.84


def test_clamp_with_ensemble_at_exact_ceiling_no_round_up():
    # score EXACTLY equal to the ceiling must pass through unchanged (<= not <),
    # and the returned value must be the FLOORED ceiling (0.70), never rounded.
    s, reason = clamp_with_ensemble(0.70, [0.45, 0.6000000000000001])
    assert s == 0.70 and reason == ""


# ---------------------------------------------------------------------------
# 7. apply_verdict blocking path — kills `max(0.01, ...)` survivor (line 490).
#    A blocking objection zeroes the score; 0.01 would let a sealed claim
#    carry phantom confidence.
# ---------------------------------------------------------------------------

def test_apply_verdict_blocking_objection_vetoes_without_bonus():
    from agp.adversary import Adversary
    ob = AdversaryObjection(claim_id="c", severity="BLOCKING", text="veto")
    score, reason = Adversary.apply_verdict(0.83, [ob])
    assert reason == "veto"
    # blocking floors to [0, score]; never raises
    assert score == floor_conf(0.83)
    # a zero-confidence claim must stay exactly ZERO (kills max(0.01,...))
    score0, _ = Adversary.apply_verdict(0.0, [ob])
    assert score0 == 0.0


def test_apply_verdict_penalties_only_subtract():
    from agp.adversary import Adversary
    obs = [AdversaryObjection(claim_id="c", severity="MAJOR", text="m"),
           AdversaryObjection(claim_id="c", severity="MINOR", text="n")]
    score, reason = Adversary.apply_verdict(0.83, obs)
    assert score == floor_conf(0.83 - 0.15 - 0.05)
    assert score < 0.83
    # no objections -> unchanged, no bonus
    score0, _ = Adversary.apply_verdict(0.83, [])
    assert score0 == 0.83


# ---------------------------------------------------------------------------
# 8. Gate coverage boundary — kills min_coverage default nudge (line 119) so
#    the documented 25% admission threshold cannot drift silently.
# ---------------------------------------------------------------------------

def test_relevance_gate_default_threshold_is_documented_value():
    from tools.pipeline.retrieval import RelevanceGate
    g = RelevanceGate()
    assert g.min_coverage == 0.25
    with pytest.raises(ValueError):
        RelevanceGate(min_coverage=0.0)
    with pytest.raises(ValueError):
        RelevanceGate(min_coverage=1.01)


def test_relevance_gate_admits_at_exactly_threshold():
    from tools.pipeline.retrieval import RelevanceGate
    g = RelevanceGate(min_coverage=0.5)
    question = "what semiconductor export restrictions applied"
    # content containing half the topical words must be admitted (>= not >)
    body = {"title": "semiconductor export restrictions", "summary": ""}
    admitted, cov, _ = g.judge(question, "factual", body)
    assert admitted is True
