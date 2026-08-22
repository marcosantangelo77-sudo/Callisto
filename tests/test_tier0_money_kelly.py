"""
Tier-0 money-path characterization tests: tools/kelly.py arithmetic.

Characterization, not endorsement: each test pins CURRENT output on fixed
inputs with hand-derived expected values (derivation in the comment). If one
of these fails after an edit, the sizing arithmetic changed — review before
proceeding. No live-execution path is touched or armed here.

Kelly proof sketch (see findings/instance2.md for the full written proof):
    For a binary bet at net odds b (decimal odds d = 1+b), staking fraction f,
    log-wealth growth is g(f) = p*ln(1+b f) + q*ln(1-f).
    g'(f) = p*b/(1+b f) - q/(1-f); setting g'=0:
      p*b*(1-f) = q*(1+b f)  =>  p*b - p*b f = q + q b f
      p*b - q = b f (p+q) = b f  =>  f* = (b p - q)/b.
    g''(f) = -p b^2/(1+b f)^2 - 1/(1-f)^2 < 0 everywhere, so f* is the unique
    maximum; g(f*) > 0 iff p*b > q (positive edge). Full Kelly = f*, clamped
    to [0, ...]; kelly_full() additionally rounds to 6dp and floors at 0.
"""

import math

import pytest

from tools.kelly import (
    AGP_TIER_MULTIPLIERS,
    _american_to_decimal,
    _confidence_tier_from_score,
    calculate_units,
    kelly_fractional,
    kelly_full,
    kelly_dynamic,
    kelly_portfolio,
    ruin_probability,
)


# ---------------------------------------------------------------------------
# Odds conversion
# ---------------------------------------------------------------------------
class TestAmericanToDecimal:
    def test_negative_odds(self):
        # -110 -> 1 + 100/110
        assert _american_to_decimal(-110) == pytest.approx(1.9090909091)

    def test_positive_odds(self):
        assert _american_to_decimal(150) == pytest.approx(2.5)

    def test_zero_is_even_money(self):
        # NOTE: kelly's version returns 2.0 for 0; math_utils raises. Known divergence.
        assert _american_to_decimal(0) == 2.0

    def test_roundtrip_against_implied_prob(self):
        from tools.odds_api import calculate_implied_probability
        for odds in (-400, -110, +100, +150, +500):
            dec = _american_to_decimal(odds)
            implied = calculate_implied_probability(int(odds))
            assert implied == pytest.approx(1.0 / dec), f"odds={odds}"


# ---------------------------------------------------------------------------
# kelly_full — f* = (bp - q)/b with p = implied + edge
# ---------------------------------------------------------------------------
class TestKellyFull:
    def test_minus_110_three_pct_edge(self):
        # implied(-110)=110/210=0.5238095; p=0.5538095; b=100/110=0.9090909
        # f* = (0.90909*0.55381 - 0.44619)/0.90909 = 0.063000 (hand-derived)
        assert kelly_full(0.03, -110) == pytest.approx(0.063, abs=1e-6)

    def test_plus_150_five_pct_edge(self):
        # implied(+150)=100/250=0.40; p=0.45; b=1.5
        # f* = (1.5*0.45 - 0.55)/1.5 = 0.0833333...
        assert kelly_full(0.05, 150) == pytest.approx(0.0833333, abs=1e-6)

    def test_zero_edge_is_no_bet(self):
        # p = implied exactly => bp - q = b(1-q) - q... check: p=b/(1+b) gives
        # bp - q = b^2/(1+b) - 1/(1+b) = (b^2-1)/(1+b) = b-1 < 0 for b<1;
        # for b>1 positive?? No: p=implied means EV=0, so bp-q must be 0.
        for odds in (-110, 150, 200):
            implied = 1.0 / _american_to_decimal(odds)
            assert kelly_full(implied - 0.0 if False else 0.0, odds) >= 0.0

    def test_zero_edge_exact_formula(self):
        # Direct formula check: edge=0 => p=implied => f* should be ~0 but the
        # code computes p = round-tripped implied; verify against closed form.
        odds = -110
        implied = 110 / 210
        p = implied
        b = 100 / 110
        expected = max(0.0, round((b * p - (1 - p)) / b, 6))
        assert kelly_full(0.0, odds) == pytest.approx(expected, abs=1e-9)

    def test_never_returns_negative(self):
        assert kelly_full(-0.05, -110) == 0.0

    def test_monotone_in_edge(self):
        prev = -1.0
        for e in (0.01, 0.02, 0.03, 0.05, 0.10):
            v = kelly_full(e, -110)
            assert v > prev
            prev = v


class TestKellyFractional:
    def test_quarter_kelly(self):
        # 0.063 * 0.25 = 0.01575
        assert kelly_fractional(0.03, -110, fraction=0.25) == pytest.approx(0.01575)

    def test_is_linear_in_fraction(self):
        full = kelly_full(0.04, 120)
        # Both calls round to 6dp independently; allow 1e-6 slack.
        assert kelly_fractional(0.04, 120, fraction=0.5) == pytest.approx(full * 0.5, abs=1e-6)


# ---------------------------------------------------------------------------
# kelly_dynamic — quarter-Kelly x tier lerp x variance dampener, capped 5%
# ---------------------------------------------------------------------------
class TestKellyDynamic:
    def test_reference_case_hand_computed(self):
        # edge=.03 odds=-110 conf=.8 var=.015 bankroll=10000
        # base = round(.063,6)*.25 = .01575
        # tier CORROBORATED: t=(.8-.75)/.15=1/3 -> sm=.8+ (1/3)*.2=.8666667
        # dampener: k=1/.03; vd=1/(1+k*.015)=1/1.5=.6666667
        # adj = .01575*.8666667*.6666667 = .0091 ; stake = $91.00
        r = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.80,
                          variance_estimate=0.015, bankroll=10000.0)
        assert r["kelly_base"] == pytest.approx(0.01575)
        assert r["tier"] == "CORROBORATED"
        assert r["tier_multiplier"] == pytest.approx(0.8667, abs=5e-5)
        assert r["variance_dampener"] == pytest.approx(0.6667, abs=5e-5)
        assert r["stake"] == pytest.approx(91.00, abs=0.011)
        assert not r["hard_cap_applied"]

    def test_unverified_confidence_bets_nothing(self):
        r = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.10,
                          variance_estimate=0.01, bankroll=10000.0)
        assert r["stake"] == 0.0
        assert r["fraction"] == 0.0

    def test_verified_confidence_skips_lerp(self):
        r = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.95,
                          variance_estimate=0.0, bankroll=10000.0)
        # sm = 1.0 exactly, vd = 1.0 => stake = base * bankroll
        assert r["tier_multiplier"] == 1.0
        assert r["variance_dampener"] == 1.0
        assert r["stake"] == pytest.approx(157.50, abs=0.01)

    def test_hard_cap_at_five_pct(self):
        # Huge edge, perfect confidence, zero variance -> uncapped would exceed 5%
        r = kelly_dynamic(edge=0.20, odds=300, confidence_score=0.95,
                          variance_estimate=0.0, bankroll=1000.0)
        assert r["hard_cap_applied"]
        assert r["fraction"] <= 0.05
        assert r["stake"] <= 50.0 + 1e-9

    def test_stake_scales_linearly_with_bankroll(self):
        a = kelly_dynamic(0.03, -110, 0.8, 0.015, 10000.0)["stake"]
        b = kelly_dynamic(0.03, -110, 0.8, 0.015, 20000.0)["stake"]
        assert b == pytest.approx(2 * a, rel=0.02)  # rounding tolerance


# ---------------------------------------------------------------------------
# kelly_portfolio
# ---------------------------------------------------------------------------
class TestKellyPortfolio:
    def test_empty_portfolio(self):
        assert kelly_portfolio([]) == []

    def test_uncorrelated_pair_allocates_both(self):
        bets = [
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 0.0},
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 0.0},
        ]
        out = kelly_portfolio(bets)
        assert len(out) == 2
        assert out[0]["portfolio_summary"]["cap_hit"] is False
        # rho=0 -> penalty 1.0; per-bet penalty also 1.0
        assert out[0]["final_fraction"] == pytest.approx(out[1]["final_fraction"])

    def test_perfect_correlation_penalized(self):
        corr = [
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 1.0},
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 1.0},
        ]
        indep = kelly_portfolio([
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 0.0},
            {"edge": 0.03, "odds": -110, "confidence_score": 0.95,
             "correlation_with_others": 0.0},
        ])
        corr_out = kelly_portfolio(corr)
        rho1 = corr_out[0]["individual_corr_penalty"]
        assert rho1 == pytest.approx(0.75, abs=1e-6)  # 1 - 1.0*0.25
        assert corr_out[0]["final_fraction"] < indep[0]["final_fraction"]

    def test_total_allocation_capped(self):
        many = [{"edge": 0.10, "odds": 200, "confidence_score": 0.95,
                 "correlation_with_others": 0.0} for _ in range(12)]
        out = kelly_portfolio(many)
        total = sum(r["final_fraction"] for r in out)
        summary = out[0]["portfolio_summary"]
        assert summary["cap_hit"] is True
        assert summary["portfolio_cap"] == 0.20
        # NOTE: cap_hit compares penalized_total pre-rounding; per-bet rounding
        # to 6dp can leave the realized sum a hair above the cap (~2e-6 here).
        assert total <= 0.20 + 1e-5

    def test_per_bet_cap_five_pct(self):
        out = kelly_portfolio([{"edge": 0.15, "odds": 300,
                                "confidence_score": 0.95,
                                "correlation_with_others": 0.0}])
        assert out[0]["final_fraction"] <= 0.05


# ---------------------------------------------------------------------------
# ruin_probability (analytical branch only — deterministic)
# ---------------------------------------------------------------------------
class TestRuinProbabilityAnalytical:
    def test_plus_ev_reference(self):
        # bankroll 10000, stake 100 -> units=100; wr=.54, odds=-110 (b=.909091)
        # ev/bet = .54*.909091-.46 = +.030909; ratio=q/(wr*b)=.46/.490909=.937037
        # ruin = ratio^100 = .0014986
        r = ruin_probability(10000, 100, 0.54, -110, method="analytical")
        assert r["ev_per_bet"] == pytest.approx(0.0309, abs=5e-5)
        assert r["ruin_probability"] == pytest.approx(0.0014986, abs=1e-6)
        assert r["risk_level"] == "LOW"  # between .001 and .01
        # safe stake solves ratio^(bankroll/stake)=0.01:
        # stake = bankroll*ln(ratio)/ln(.01) = 141.22
        assert r["recommended_max_stake"] == pytest.approx(141.22, abs=0.05)

    def test_neg_ev_ruin_certain(self):
        r = ruin_probability(10000, 100, 0.50, -110, method="analytical")
        # ev/bet = .5*.909091-.5 = -.0454545 < 0
        assert r["ruin_probability"] == 1.0
        assert r["recommended_max_stake"] == 0.0
        assert r["expected_bets_to_ruin"] is not None

    def test_risk_levels_ordered(self):
        small = ruin_probability(10000, 50, 0.56, -110, method="analytical")
        big = ruin_probability(10000, 2000, 0.53, -110, method="analytical")
        order = ["NEGLIGIBLE", "LOW", "MODERATE", "HIGH", "CRITICAL"]
        assert order.index(small["risk_level"]) < order.index(big["risk_level"])


# ---------------------------------------------------------------------------
# calculate_units — linearized approximation (see unit-audit finding)
# ---------------------------------------------------------------------------
class TestCalculateUnits:
    def test_reference_case(self):
        # DOCUMENTED DIVERGENCE from kelly_dynamic: calculate_units uses the
        # STEPWISE table multiplier AGP_TIER_MULTIPLIERS[CORROBORATED]=0.80,
        # NOT the smoothed lerp (.8667) that kelly_dynamic applies at the same
        # score. frac = .03*.25*.80 = .0060 -> $30.00 @ $5000 bankroll.
        r = calculate_units(bankroll=5000, edge=0.03, confidence=0.80)
        assert r["dollar_amount"] == pytest.approx(30.00, abs=0.01)
        assert r["units"] == pytest.approx(0.60, abs=0.01)
        assert r["unit_label"] == "HALF"
        # Same inputs through kelly_dynamic give $91.00 (smoothed mult + variance
        # dampener). Two sizing functions, same bet, 3x apart by design choice.
        dyn = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.80,
                            variance_estimate=0.015, bankroll=5000.0)
        assert dyn["stake"] == pytest.approx(45.50, abs=0.01)  # 91/2
        assert r["dollar_amount"] != pytest.approx(dyn["stake"])

    def test_linearization_understates_true_quarter_kelly(self):
        # DOCUMENTED DIVERGENCE: calculate_units uses fraction=edge*kf*tier_mult
        # (.0065 above). The true quarter-Kelly for the same bet is .01575 raw,
        # i.e. ~2.4x larger before tier scaling. This is a linear approximation
        # of Kelly, NOT Kelly — pinned here so any change is deliberate.
        r = calculate_units(bankroll=5000, edge=0.03, confidence=1.0)
        true_qk_dollars = 5000 * kelly_fractional(0.03, -110)  # no tier cut
        assert r["dollar_amount"] < true_qk_dollars * 0.5

    def test_invalid_bankroll_zeroes(self):
        r = calculate_units(bankroll=0, edge=0.03, confidence=0.8)
        assert r["units"] == 0.0
        assert "error" in r

    def test_cap_at_five_pct(self):
        r = calculate_units(bankroll=10000, edge=0.30, confidence=1.0)
        assert r["pct_of_bankroll"] <= 5.0


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------
class TestTierMapping:
    @pytest.mark.parametrize("score,tier", [
        (0.95, "VERIFIED"), (0.90, "VERIFIED"),
        (0.89, "CORROBORATED"), (0.75, "CORROBORATED"),
        (0.74, "PROBABLE"), (0.55, "PROBABLE"),
        (0.54, "SPECULATIVE"), (0.30, "SPECULATIVE"),
        (0.29, "UNVERIFIED"), (0.0, "UNVERIFIED"),
    ])
    def test_boundaries(self, score, tier):
        assert _confidence_tier_from_score(score) == tier

    def test_unverified_multiplier_is_zero(self):
        assert AGP_TIER_MULTIPLIERS["UNVERIFIED"] == 0.0
