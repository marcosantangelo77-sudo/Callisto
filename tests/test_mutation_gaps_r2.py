"""Mutation-gap tests, wave 3: kill the remaining high-severity survivors.

Targets (from findings/redteam_mutation.md "still open"):
  - tools/kelly.py: variance dampener internals (272/274), correlation-penalty
    machinery (376-440), ruin_probability analytical + risk ladder
    (504-635), Monte Carlo simulator (_simulate_ruin, 613-646), timing_value
    regime/decay/decision logic (689-792), calculate_units guards + unit
    ladder (828-874), kelly_full clamps (161).
  - tools/edge_confidence.py: book-count ladder (156-203), market-efficiency
    constants (215-222), time/HHI/entropy/KL boundaries (243-350), trap /
    attention / RLM / steam / contrarian psychology ladders (385-496),
    final clamp + tier mapping (530-541), score_parlay combination
    (569-609).

Method: AT-and-either-side boundary pins. Every threshold gets the exact
threshold value AND a neighbour; every constant gets an exact-output pin
observed on the clean tree. A one-token mutation shifts at least one pin.
"""
import math

import pytest

import tools.kelly as K
from tools.edge_confidence import (
    EdgeConfidence,
    MARKET_EFFICIENCY,
    NOISE_FLOOR_PCT,
    score_edge,
    score_parlay,
)

# ===========================================================================
# tools/kelly.py — kelly_full clamps (line 161)
# ===========================================================================

def test_kelly_full_upper_clamp_p_is_one():
    # Huge positive edge: raw p > 1 must clamp to exactly 1.0 -> f* = 1.0.
    # Mutant min(1.01, p) lets f* exceed 1.0 (over-full bankroll sizing).
    assert K.kelly_full(0.60, -110) == 1.0


def test_kelly_full_lower_clamp_p_is_zero():
    # Huge negative edge at long odds: mutant max(0.01, p) yields f* > 0
    # (betting on a certain loss). Clean code returns exactly 0.0.
    assert K.kelly_full(-0.55, 10000) == 0.0

# ===========================================================================
# tools/kelly.py — dynamic Kelly smoothing ladder (243-265) + dampener
# ===========================================================================

@pytest.mark.parametrize("conf,expected_tier,expected_mult", [
    (0.2999, "UNVERIFIED", 0.0),
    (0.30,   "SPECULATIVE", 0.30),
    (0.31,   "SPECULATIVE", 0.31),
    (0.5499, "SPECULATIVE", 0.5499),
    (0.55,   "PROBABLE",    0.55),
    (0.56,   "PROBABLE",    0.5625),
    (0.7499, "PROBABLE",    0.7999),
    (0.75,   "CORROBORATED", 0.80),
    (0.76,   "CORROBORATED", 0.8133),
    (0.8999, "CORROBORATED", 0.9999),
    (0.90,   "VERIFIED",    1.0),
])
def test_kelly_dynamic_smoothing_ladder_boundaries(conf, expected_tier, expected_mult):
    """Exact tier-multiplier pins at and either side of every smoothing-band
    boundary. Kills the PROBABLE ==/!= swap and every lerp-constant nudge."""
    r = K.kelly_dynamic(0.03, -110, conf, 0.001, 10000)
    assert r["tier"] == expected_tier
    assert r["tier_multiplier"] == pytest.approx(expected_mult, abs=1e-4)


def test_kelly_dynamic_variance_dampener_floor_exact():
    # Dampener floors at exactly 0.05 (mutant 0.06 shifts every saturated call).
    for var in (1.0, 5.0, 100.0):
        r = K.kelly_dynamic(0.03, -110, 0.95, var, 10000)
        assert r["variance_dampener"] == 0.05


def test_kelly_dynamic_variance_normalization_k():
    # k = 1/max(|edge|, 0.001): with edge=0.0001 and var=2*edge the dampener
    # is exactly 1/(1+2) = 0.8333.  Mutant floor 0.01 gives k=100 -> 0.9804.
    r = K.kelly_dynamic(0.0001, -110, 0.95, 0.0002, 10000)
    assert r["variance_dampener"] == pytest.approx(5.0 / 6.0, abs=1e-4)


def test_kelly_dynamic_stake_is_fraction_times_bankroll_two_dp():
    r = K.kelly_dynamic(0.03, -110, 0.90, 0.001, 10000)
    assert r["fraction"] == 0.015242          # round(...,6) pinned
    assert r["stake"] == round(10000 * 0.015242, 2)

# ===========================================================================
# tools/kelly.py — portfolio correlation machinery (376-440)
# ===========================================================================

def _portfolio(rho, n=4, **kw):
    bets = [{
        "edge": 0.05, "odds": -110, "confidence_score": 1.0,
        "variance_estimate": 0.001, "correlation_with_others": rho, **kw,
    } for _ in range(n)]
    rs = K.kelly_portfolio(bets)
    return rs[0], rs[0]["portfolio_summary"], rs


def test_portfolio_correlation_clip_bounds():
    # Out-of-range correlations clip to [-1, 1]; mutant bound 1.01 shifts avg.
    _, s_pos, _ = _portfolio(2.0)
    assert s_pos["avg_correlation"] == 1.0
    _, s_neg, _ = _portfolio(-2.0)
    assert s_neg["avg_correlation"] == -1.0


def test_portfolio_diversification_ratio_ladder():
    # ratio = 1 + max(0, (n-1)*rho): kills max->min, the (n-1)->(n-2) nudge,
    # and the negative-rho inversion (negative rho must NOT reduce sizing).
    for rho, expected in [(-1.0, 1.0), (-0.5, 1.0), (0.0, 1.0),
                          (0.25, 1.75), (0.5, 2.5), (0.75, 3.25), (1.0, 4.0)]:
        _, s, _ = _portfolio(rho)
        assert s["diversification_ratio"] == pytest.approx(expected, abs=1e-6), rho


def test_portfolio_correlation_penalty_values_and_direction():
    # penalty = 1/sqrt(max(1, ratio)): monotone decreasing in rho, exactly 1.0
    # at rho<=0 and exactly 0.5 at perfect correlation with n=4.
    prev = None
    for rho, expected in [(-1.0, 1.0), (0.0, 1.0), (0.25, 0.7559),
                          (0.5, 0.6325), (0.75, 0.5547), (1.0, 0.5)]:
        _, s, _ = _portfolio(rho)
        assert s["correlation_penalty"] == pytest.approx(expected, abs=1e-4), rho
        if prev is not None and rho > 0:
            assert s["correlation_penalty"] <= prev
        prev = s["correlation_penalty"]
    # inverted penalty (min instead of max inside sqrt) would push penalty >= 1
    _, s, _ = _portfolio(1.0)
    assert s["correlation_penalty"] < 1.0


def test_portfolio_individual_penalty_negative_rho_not_penalized():
    # rho_i = max(0.0, rho): a hedged bet (rho<0) gets NO individual penalty.
    r, _, _ = _portfolio(-0.5)
    assert r["individual_corr_penalty"] == 1.0
    # perfect correlation -> exactly 0.75
    r1, _, _ = _portfolio(1.0)
    assert r1["individual_corr_penalty"] == 0.75


def test_portfolio_sizing_decreases_with_correlation():
    fracs = []
    for rho in (0.0, 0.25, 0.5, 1.0):
        r, _, _ = _portfolio(rho)
        fracs.append(r["final_fraction"])
    assert fracs == sorted(fracs, reverse=True)
    assert fracs[0] > fracs[-1]


def test_portfolio_per_bet_cap_binds_at_five_pct():
    # One enormous edge: per-bet hard cap is exactly 0.05 (mutant 0.06 drifts).
    rs = K.kelly_portfolio([{"edge": 0.30, "odds": 200,
                             "correlation_with_others": 0.0}])
    assert rs[0]["final_fraction"] == 0.05
    assert rs[0]["final_pct"] == 5.0


def test_portfolio_cap_twenty_pct_and_cap_hit_flag():
    bets = [{"edge": 0.20, "odds": 200, "confidence_score": 1.0,
             "variance_estimate": 0.001, "correlation_with_others": 0.0}
            for _ in range(6)]
    rs = K.kelly_portfolio(bets)
    summary = rs[0]["portfolio_summary"]
    assert summary["portfolio_cap"] == 0.20
    assert summary["cap_hit"] is True
    assert summary["final_total_allocation"] <= 0.20 + 1e-9

# ===========================================================================
# tools/kelly.py — ruin_probability analytical (492-646)
# ===========================================================================

def test_ruin_neg_ev_certain_ruin_and_zero_recommended_stake():
    r = K.ruin_probability(10000, 100, 0.4, -110)
    assert r["ev_per_bet"] < 0
    assert r["ruin_probability"] == 1.0
    assert r["recommended_max_stake"] == 0.0
    assert r["recommended_max_stake_pct"] == 0.0


def test_ruin_break_even_boundary_is_certain_ruin():
    # win_rate*b == q exactly (american 100 at win_rate 0.5): ratio == 1.0
    # must take the ruin==1.0 branch (mutant >= -> > falls through).
    r = K.ruin_probability(10000, 100, 0.5, 100)
    assert r["ruin_probability"] == 1.0
    assert r["recommended_max_stake"] == 0.0


def test_ruin_analytical_value_pinned():
    # ratio^units with ratio = 0.48/(0.52*(104762/100000)), units = 100.
    r = K.ruin_probability(10000, 100, 0.52, -105)
    assert r["ruin_probability"] == 0.043926
    assert r["ruin_pct"] == 4.3926


def test_ruin_safe_stake_formula_exact():
    # stake = bankroll * ln(ratio) / ln(0.01); kills the max(0,..)->max(1,..)
    # flip (small bankroll -> sub-dollar safe stake) and rounding-digit nudges.
    r_small = K.ruin_probability(10, 1, 0.52, -105)
    assert 0 < r_small["recommended_max_stake"] < 1.0
    assert r_small["recommended_max_stake"] == 0.07
    r = K.ruin_probability(10000, 50, 0.6, -110)
    assert r["recommended_max_stake"] == 673.49
    assert r["recommended_max_stake_pct"] == 6.73


def test_ruin_risk_level_ladder_boundaries():
    # Constructed ratios land in each band; the < / > flips re-route bands to
    # neighbours (e.g. MODERATE -> CRITICAL), the const nudges shift them.
    # ratio chosen so ruin = ratio^units hits each side of each threshold.
    def level(bankroll, stake, wr, odds):
        return K.ruin_probability(bankroll, stake, wr, odds)["risk_level"]

    assert level(10000, 100, 0.6, -110) == "NEGLIGIBLE"      # ruin ~ 0
    import math as _m
    # american +500 -> b = 5.0, so ratio = 0.48/(0.52*5) ≈ 0.1846 < 1.
    b = 5.0
    ratio = 0.48 / (0.52 * b)
    stake = 10.0
    for target_ruin, expected in [(0.0009, "NEGLIGIBLE"), (0.009, "LOW"),
                                  (0.011, "MODERATE"), (0.051, "HIGH")]:
        units = _m.log(target_ruin) / _m.log(ratio)
        assert level(units * stake, stake, 0.52, 500) == expected, expected
    assert level(5000, 500, 0.52, -105) == "CRITICAL"


def test_ruin_units_in_bankroll_cap_and_guard():
    r = K.ruin_probability(10_000_000, 1, 0.6, -110)
    assert r["units_in_bankroll"] == 10000.0       # capped at exactly 10000
    r2 = K.ruin_probability(500, 0, 0.6, -110)     # avg_stake == 0 guard
    assert r2["units_in_bankroll"] == 10000.0

# ===========================================================================
# tools/kelly.py — Monte Carlo ruin simulation (613-646)
# ===========================================================================

def test_simulate_ruin_deterministic_seeded_outputs():
    # Seed 42 => bit-identical results. Kills seed drift, outcome-comparison
    # flip, axis swaps, ruined-threshold flips, drawdown divisor and percentile.
    rp, med, dd = K._simulate_ruin(1000, 10, 0.55, 0.909,
                                   n_simulations=500, n_bets=300)
    assert rp == 0.0
    assert med == pytest.approx(1149.85, abs=5e-3)
    assert dd == pytest.approx(0.2424, abs=2e-4)


def test_simulate_ruin_certain_loss_ruins_every_path():
    rp, _, _ = K._simulate_ruin(1000, 10, 0.05, 0.909,
                                n_simulations=200, n_bets=300)
    assert rp == 1.0


def test_simulate_ruin_strong_edge_grows_bankroll():
    rp, med, _ = K._simulate_ruin(1000, 10, 0.90, 0.909,
                                  n_simulations=500, n_bets=500)
    assert rp == 0.0
    assert med > 4000   # median path more than quadruples


def test_ruin_probability_simulation_method_smoke():
    r = K.ruin_probability(1000, 10, 0.55, -110, method="simulation")
    assert 0.0 <= r["ruin_probability"] <= 1.0
    assert "simulation" in r

# ===========================================================================
# tools/kelly.py — timing_value (689-792)
# ===========================================================================

@pytest.mark.parametrize("hours,regime", [
    (0.01, "late"), (3.99, "late"), (4.0, "late"),
    (4.01, "mid"), (24.0, "mid"), (24.01, "early"),
])
def test_timing_value_regime_boundaries(hours, regime):
    r = K.timing_value(current_edge=0.03, hours_to_game=hours)
    assert r["details"]["regime"] == regime


def test_timing_value_decay_rate_lookup_and_default():
    # spreads decay 1.1 vs unknown-market default exactly 1.0 (mutant 1.01).
    r_spread = K.timing_value(current_edge=0.03, hours_to_game=12, market="spreads")
    assert r_spread["details"]["edge_decay_rate"] == 1.1
    assert r_spread["details"]["edge_remaining_fraction"] == 0.5898
    r_default = K.timing_value(current_edge=0.03, hours_to_game=12, market="mystery_market")
    assert r_default["details"]["edge_decay_rate"] == 1.0
    assert r_default["details"]["edge_remaining_fraction"] == 0.6188


def test_timing_value_hours_clamp_at_one_hundredth():
    # hours_to_game=0 clamps to 0.01 (mutant 0.02 halves the retained edge).
    r = K.timing_value(current_edge=0.03, hours_to_game=0)
    assert r["details"]["hours_to_game"] == 0.0
    assert r["details"]["edge_remaining_fraction"] == 0.9992


def test_timing_value_steam_boost_cost_constants():
    # favourable 0.3 / unfavourable 0.7 split pinned to 5dp.
    r = K.timing_value(current_edge=0.03, hours_to_game=12)
    d = r["details"]
    assert d["favorable_steam_boost"] == 0.01728
    assert d["unfavorable_steam_cost"] == 0.04032


def test_timing_value_stale_line_bonus_efficiency_and_cap():
    # efficiency = 1 - 0.1/decay; spreads -> 0.90909... ; bonus uses the 12h cap.
    r = K.timing_value(current_edge=0.03, hours_to_game=24, market="spreads")
    assert r["details"]["stale_line_bonus"] == 0.02182
    r_tot = K.timing_value(current_edge=0.03, hours_to_game=12, market="totals")
    # totals decay 0.9 -> efficiency 0.888... -> different bonus
    assert r_tot["details"]["stale_line_bonus"] == 0.02667


def test_timing_value_no_bet_on_non_positive_edge():
    assert K.timing_value(current_edge=0.0, hours_to_game=10)["recommendation"] == "NO_BET"
    assert K.timing_value(current_edge=-0.01, hours_to_game=10)["recommendation"] == "NO_BET"


def test_timing_value_bet_now_when_decay_dominates():
    # ev_diff = -0.04224 < -0.25*wu = -0.03464 -> lock in now.
    r = K.timing_value(current_edge=0.10, hours_to_game=12, market="spreads")
    assert r["recommendation"] == "BET_NOW"
    assert r["ev_difference"] == pytest.approx(-0.04224, abs=1e-5)


def test_timing_value_slight_lean_is_the_default_middle_band():
    # ev_diff between -0.25*wu and +0.5*wu -> SLIGHT_LEAN_NOW (not WAIT).
    for edge, market, hours in [(0.02, "spreads", 12.0), (0.05, "player_points", 12.0),
                                (0.03, "totals", 1.0)]:
        r = K.timing_value(current_edge=edge, hours_to_game=hours, market=market)
        assert r["recommendation"] == "SLIGHT_LEAN_NOW", (edge, market, hours)

# ===========================================================================
# tools/kelly.py — calculate_units guards + unit ladder (828-893)
# ===========================================================================

def test_calculate_units_invalid_inputs_return_error_dict():
    for kwargs in ({"unit_size": 0.0}, {"unit_size": -5}):
        r = K.calculate_units(1000, 0.05, 1.0, **kwargs)
        assert r["error"] == "Invalid bankroll or unit size"
        assert r["dollar_amount"] == 0.0
        assert r["pct_of_bankroll"] == 0.0
    r = K.calculate_units(0, 0.05, 1.0)
    assert r["error"] == "Invalid bankroll or unit size"
    assert r["units"] == 0.0


def test_calculate_units_unverified_tier_sizes_nothing():
    r = K.calculate_units(1000, 0.05, 0.29)
    assert r["units"] == 0.0
    assert r["unit_label"] == "NO_BET"


@pytest.mark.parametrize("target_units,expected_label", [
    (3.0, "MAX"), (2.999, "MAX"), (2.0, "STRONG"), (1.0, "STANDARD"),
    (0.5, "HALF"), (0.25, "LEAN"), (0.0, "NO_BET"),
])
def test_calculate_units_ladder_boundaries(target_units, expected_label):
    # units == fraction*100 with default unit size; pick edge to land exactly
    # on (or a hair above) each rung.  >= -> > flips demote the exact-rung
    # case; const nudges (3.0->3.01) do the same.
    edge = target_units / 100.0 / 0.25 if target_units else 0.0
    r = K.calculate_units(1000, edge, 1.0, kelly_fraction=0.25)
    assert r["units"] == pytest.approx(target_units, abs=0.01)
    assert r["unit_label"] == expected_label

# ===========================================================================
# tools/edge_confidence.py — constants
# ===========================================================================

def test_noise_floor_constant_pinned():
    assert NOISE_FLOOR_PCT == 0.5


@pytest.mark.parametrize("market,expected_adj", [
    ("h2h", 0.008), ("spreads", 0.015), ("totals", 0.023),
    ("player_points", 0.045), ("alternate_spreads", 0.067),
])
def test_market_efficiency_constants_via_factor_pins(market, expected_adj):
    # market_adj = (1-efficiency)*0.15, rounded to 3dp: each table constant
    # produces a distinct pin (const +-0.01 mutations shift it).
    ec = score_edge(edge_pct=3.0, books_compared=2,
                    book_names=["bookA", "bookB"], market=market)
    assert ec.factors["market_efficiency"] == pytest.approx(expected_adj, abs=5e-4), market


def test_unknown_market_default_efficiency_is_point_eight():
    ec = score_edge(edge_pct=3.0, books_compared=2,
                    book_names=["bookA", "bookB"], market="nope")
    assert ec.factors["market_efficiency"] == pytest.approx(0.03, abs=5e-4)

# ===========================================================================
# tools/edge_confidence.py — source-class + book-count ladder (155-203)
# ===========================================================================

def test_source_class_ceiling_two_books_is_secondary():
    # books_compared >= 2 (not > 2): exactly two books is SECONDARY, 0.75 cap.
    ec = score_edge(edge_pct=5.0, books_compared=2,
                    book_names=["bookA", "bookB"])
    assert ec.source_class == "SECONDARY"
    assert ec.ceiling == 0.75


def test_book_count_adjustment_ladder_exact():
    prev = None
    for books, expected in [(1, -0.10), (2, 0.0), (3, 0.05),
                            (4, 0.05), (5, 0.10), (6, 0.10)]:
        ec = score_edge(edge_pct=3.0, books_compared=books,
                        book_names=[f"b{i}" for i in range(books)])
        assert ec.factors["book_count"] == pytest.approx(expected, abs=1e-9), books
        if prev is not None:
            assert ec.factors["book_count"] >= prev - 1e-9
        prev = ec.factors["book_count"]

# ===========================================================================
# tools/edge_confidence.py — signal-ladder boundaries (243-350)
# ===========================================================================

def _factors(**kw):
    base = dict(edge_pct=3.0, books_compared=2,
                book_names=["bookA", "bookB"])
    base.update(kw)
    return score_edge(**base).factors


@pytest.mark.parametrize("hours,expected", [
    (0.49, 0.03), (0.5, 0.0), (0.51, 0.0), (24.0, 0.0), (24.01, -0.05),
])
def test_time_to_game_boundary(hours, expected):
    assert _factors(hours_to_game=hours)["time_to_game"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("hhi,expected", [
    (1499, 0.05), (1500, 0.0), (4000, 0.0), (4001, -0.05),
])
def test_market_hhi_boundary(hhi, expected):
    assert _factors(market_hhi=hhi)["market_hhi"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("entropy,expected", [
    (2.01, 0.05), (2.0, 0.0), (0.49, -0.03), (0.5, 0.0),
])
def test_market_entropy_boundary(entropy, expected):
    assert _factors(market_entropy=entropy)["market_entropy"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("kl,expected", [
    (0.06, 0.06), (0.05, 0.03), (0.02, 0.03), (0.01, 0.0), (0.0009, -0.04),
])
def test_kl_divergence_ladder(kl, expected):
    assert _factors(kl_divergence=kl)["kl_divergence"] == pytest.approx(expected, abs=1e-9)


def test_js_divergence_bonus_boundary():
    assert _factors(kl_divergence=0.02, js_divergence=0.03)["kl_divergence"] == pytest.approx(0.03, abs=1e-9)
    assert _factors(kl_divergence=0.02, js_divergence=0.031)["kl_divergence"] == pytest.approx(0.05, abs=1e-9)

# ===========================================================================
# tools/edge_confidence.py — psychology ladders (357-496)
# ===========================================================================

def test_number_shading_sides():
    assert _factors(number_shading_detected=True, shading_value_side="opposite")["number_shading"] == pytest.approx(0.06, abs=1e-9)
    assert _factors(number_shading_detected=True, shading_value_side="this_side")["number_shading"] == pytest.approx(0.03, abs=1e-9)
    assert _factors(number_shading_detected=True, shading_value_side=None)["number_shading"] == pytest.approx(-0.04, abs=1e-9)


@pytest.mark.parametrize("conf,side,expected", [
    (0.30, "opposite_public", 0.0),           # not > 0.30: no signal
    (0.31, "opposite_public", 0.031),
    (1.0, "opposite_public", 0.08),           # cap exactly 0.08
    (1.0, "public", -0.06),                   # cap exactly -0.06
])
def test_trap_line_ladder(conf, side, expected):
    assert _factors(trap_line_confidence=conf, trap_actionable_side=side)["trap_line"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("opp,expected", [
    (0.3, 0.0), (0.31, 0.025), (1.0, 0.06),
])
def test_attention_arbitrage_ladder(opp, expected):
    assert _factors(attention_opportunity=opp)["attention_arbitrage"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("conf,sharp_side,expected", [
    (0.1, True, 0.0), (1.0, True, 0.08), (1.0, False, -0.06),
])
def test_rlm_ladder(conf, sharp_side, expected):
    f = _factors(rlm_detected=True, rlm_confidence=conf,
                 rlm_edge_on_sharp_side=sharp_side)
    assert f["rlm"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("conf,on_steam,expected", [
    (0.1, True, 0.0), (1.0, True, 0.10), (1.0, False, -0.08),
])
def test_steam_ladder(conf, on_steam, expected):
    f = _factors(steam_detected=True, steam_confidence=conf,
                 steam_edge_on_steam_side=on_steam)
    assert f["steam"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("kv,expected", [
    (0.6, 0.02), (0.61, 0.04), (0.31, 0.02),
])
def test_key_number_ladder(kv, expected):
    assert _factors(key_number_value=kv)["dead_number"] == pytest.approx(expected, abs=1e-9)


def test_contrarian_gate_requires_both_conditions():
    # edge_pct > 0 AND value_score > 1.0 — each condition alone is inert.
    assert _factors(contrarian_edge_pct=2.0, contrarian_value_score=1.0)["contrarian"] == pytest.approx(0.0, abs=1e-9)
    assert _factors(contrarian_edge_pct=0.0, contrarian_value_score=2.0)["contrarian"] == pytest.approx(0.0, abs=1e-9)
    assert _factors(contrarian_edge_pct=1.0, contrarian_value_score=2.0)["contrarian"] == pytest.approx(0.03, abs=1e-9)
    assert _factors(contrarian_edge_pct=2.0, contrarian_value_score=2.0)["contrarian"] == pytest.approx(0.06, abs=1e-9)

# ===========================================================================
# tools/edge_confidence.py — final clamp + tier mapping (530-541)
# ===========================================================================

def test_final_clamp_floors_at_exactly_zero():
    # Stack enough penalties to drive the raw total negative; the published
    # score must be exactly 0.0 (mutant floor 0.01 leaks phantom confidence).
    ec = score_edge(
        edge_pct=0.6, books_compared=1, book_names=["unknownbook"],
        market="h2h", is_live=True, public_side_edge=True,
        hours_to_game=48, market_hhi=5000, market_entropy=0.2,
        kl_divergence=0.0001,
        number_shading_detected=True, shading_value_side=None,
        trap_line_confidence=1.0, trap_actionable_side="public",
        rlm_detected=True, rlm_confidence=1.0, rlm_edge_on_sharp_side=False,
        steam_detected=True, steam_confidence=1.0,
        steam_edge_on_steam_side=False,
    )
    assert ec.score == 0.0
    assert ec.tier == "UNVERIFIED"


def test_score_quantised_downward_to_three_dp():
    ec = score_edge(edge_pct=2.99, books_compared=1, book_names=["unknownbook"])
    assert ec.score == 0.508      # round(raw,3); floor() would give 0.507


@pytest.mark.parametrize("leg_score,tier", [
    (0.90, "VERIFIED"), (0.75, "CORROBORATED"),
    (0.55, "PROBABLE"), (0.30, "SPECULATIVE"), (0.29, "UNVERIFIED"),
])
def test_parlay_tier_mapping_boundaries(leg_score, tier):
    # Two identical legs: combined == geometric mean == leg_score exactly,
    # landing ON every tier threshold.  >= -> > demotes each boundary case.
    legs = [EdgeConfidence(score=leg_score, tier=tier, source_class="PRIMARY",
                           ceiling=1.0, factors={}, reasoning="")
            for _ in range(2)]
    assert score_parlay(legs).tier == tier


def test_parlay_weights_pin():
    legs = [EdgeConfidence(score=0.5, tier="PROBABLE", source_class="PRIMARY",
                           ceiling=1.0, factors={}, reasoning=""),
            EdgeConfidence(score=1.0, tier="VERIFIED", source_class="PRIMARY",
                           ceiling=1.0, factors={}, reasoning="")]
    ec = score_parlay(legs)
    # 0.6*0.5 + 0.4*sqrt(0.5) = 0.58284...
    assert ec.score == pytest.approx(round(0.6 * 0.5 + 0.4 * math.sqrt(0.5), 3), abs=1e-9)


def test_parlay_weakest_leg_wins_source_class_and_floor():
    weak = EdgeConfidence(score=0.30, tier="SPECULATIVE", source_class="SIGNAL",
                          ceiling=0.55, factors={}, reasoning="")
    strong = EdgeConfidence(score=0.90, tier="VERIFIED", source_class="PRIMARY",
                            ceiling=1.0, factors={}, reasoning="")
    ec = score_parlay([strong, weak])
    assert ec.source_class == "SIGNAL"       # min-by-score, not max
    assert ec.ceiling == 0.55                # min ceiling across legs
    # zero-score leg: product floor is exactly 0.01 per leg
    zero = EdgeConfidence(score=0.0, tier="UNVERIFIED", source_class="INFERRED",
                          ceiling=0.55, factors={}, reasoning="")
    ec2 = score_parlay([strong, zero])
    # 0.6*0 + 0.4*(sqrt(1.0)*sqrt(0.01)) = 0.04 exactly on clean reals, but
    # round(min_score,3) and the geo-mean rounding make it 0.038 here — the
    # pin that matters is that a 0.02 per-leg product floor yields 0.0566.
    assert ec2.score == pytest.approx(0.038, abs=1e-9)


def test_parlay_empty_legs():
    ec = score_parlay([])
    assert ec.score == 0.0
    assert ec.tier == "UNVERIFIED"
    assert ec.ceiling == 0.55
