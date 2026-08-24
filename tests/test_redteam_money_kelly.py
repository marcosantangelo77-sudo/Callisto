"""RED TEAM — THE MONEY PATH, H2/H3/H5: Kelly edges, cap binding, float
accumulation.

kelly.py is "proven correct for the standard case". These tests live on the
edges: NaN/inf inputs, p at 0/1, negative edge, b<=0, push mass, portfolio
correlation, and every cap under random sweeps.
"""
import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.kelly import (
    kelly_full, kelly_fractional, kelly_dynamic, kelly_portfolio,
    calculate_units,
)
from tools.sizing import kelly_binary, kelly_with_push, bet_size

BANKROLL = st.floats(min_value=1.0, max_value=1_000_000.0)
EDGE = st.one_of(
    st.floats(min_value=-0.5, max_value=0.5, allow_nan=False),
    st.just(float("nan")),
)
AMERICAN = st.integers(min_value=-10000, max_value=10000)


def sane_american(x):
    return x != 0


# ---------------------------------------------------------------------------
# H2a: NaN / inf edge must never size a position
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("odds", [-110, 150])
def test_kelly_full_rejects_non_finite_edge(bad, odds):
    f = kelly_full(bad, odds)
    assert isinstance(f, (int, float)) and math.isfinite(f) and f == 0.0, (
        f"kelly_full(edge={bad}) returned {f!r} — a non-finite or nonzero "
        f"fraction from a poisoned input")


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_kelly_dynamic_stake_is_zero_on_nan_edge(bad):
    r = kelly_dynamic(bad, -110, 0.9, 0.01, 10_000)
    assert r["stake"] == 0.0, (
        f"NaN edge produced a REAL DOLLAR STAKE of ${r['stake']} "
        f"(fraction {r['fraction']}) — money path accepted a poisoned input")


def test_calculate_units_nan_edge():
    u = calculate_units(10_000, float("nan"), 0.9)
    assert u["dollar_amount"] == 0.0 and u["units"] == 0.0


# ---------------------------------------------------------------------------
# H2b: p exactly 0 / 1, negative edge, b <= 0
# ---------------------------------------------------------------------------

@given(EDGE.filter(lambda e: math.isfinite(e) and e <= 0), AMERICAN.filter(sane_american))
@settings(max_examples=200, deadline=None)
def test_negative_edge_never_bets(edge, odds):
    """Negative edge means the market prices it better than we do.
    Fraction must be exactly 0."""
    # clamp p into a legal range implied by the edge
    from tools.odds_api import calculate_implied_probability
    impl = calculate_implied_probability(odds)
    p = max(0.001, min(0.999, impl + edge))
    if p >= impl:
        return
    f = kelly_full(edge, odds)
    assert f == 0.0, f"negative edge {edge} sized {f}"


@pytest.mark.parametrize("p", [0.0, 1.0])
def test_sizing_at_degenerate_probabilities(p):
    """p=1 on any finite odds is 'risk-free' by construction of the input;
    Kelly would go all-in. Downstream must still bound it. p=0 must stake 0."""
    b = bet_size(bankroll=10_000, fair_prob=p, decimal_odds=2.0,
                 confidence="high")
    assert 0.0 <= b["recommended_stake"] <= 10_000 * 0.05 + 1e-6, (
        f"fair_prob={p} produced stake {b['recommended_stake']} outside "
        f"the documented 5% hard-cap envelope")


@given(st.floats(min_value=0.999999, max_value=1.0))
@settings(max_examples=30, deadline=None)
def test_near_certain_prob_capped(p):
    b = bet_size(bankroll=50_000, fair_prob=p, decimal_odds=1.5,
                 confidence="high")
    assert b["recommended_stake"] <= 50_000 * 0.05 + 1e-6


def test_decimal_odds_of_one_is_no_bet():
    """decimal=1.0 -> b=0 -> division by zero in the naive formula.
    Must return 0, not raise, not inf."""
    assert kelly_binary(0.9, 1.0) == 0.0


# ---------------------------------------------------------------------------
# H2c: push-heavy markets — invalid probability masses accepted silently
# ---------------------------------------------------------------------------

@given(st.floats(min_value=0.0, max_value=1.0),
       st.floats(min_value=0.0, max_value=1.0))
@settings(max_examples=300, deadline=None)
def test_push_mass_never_exceeds_valid_range(p_win, p_push):
    """p_win + p_push > 1 makes p_loss NEGATIVE; the naive formula then
    treats an impossible market as hugely +EV. kelly_with_push /
    bet_size must refuse or zero these."""
    if p_win + p_push > 1.0:
        f = kelly_with_push(p_win, p_push, 1.909)
        assert f == 0.0, (
            f"impossible mass p_win={p_win}+push={p_push}>1 gave Kelly {f}")
        r = bet_size(bankroll=10_000, fair_prob=p_win, decimal_odds=1.909,
                     confidence="high", p_push=p_push)
        assert r["recommended_stake"] == 0.0, (
            f"bet_size staked ${r['recommended_stake']} on a market with "
            f"negative loss probability")


# ---------------------------------------------------------------------------
# H3: every cap actually binds — random sweep
# ---------------------------------------------------------------------------

@given(BANKROLL,
       st.floats(min_value=-0.4, max_value=0.6),
       st.sampled_from([-400, -200, -110, 100, 150, 300, 800]),
       st.floats(min_value=0.0, max_value=1.0),
       st.floats(min_value=0.0, max_value=0.5))
@settings(max_examples=400, deadline=None)
def test_kelly_dynamic_hard_cap_always_binds(bankroll, edge, odds, conf, var):
    """The 5% single-bet hard cap must bind for EVERY input, including
    poisoned ones that slip through as finite."""
    if not math.isfinite(edge) or not math.isfinite(var):
        return
    try:
        r = kelly_dynamic(edge, odds, conf, var, bankroll)
    except (ValueError, OverflowError):
        return
    stake, frac = r["stake"], r["fraction"]
    assert frac <= 0.05 + 1e-6, f"fraction {frac} breached the 5% cap"
    # dollar check catches rounding-direction bugs too
    assert stake <= bankroll * 0.05 + 0.01, (
        f"${stake} on bankroll ${bankroll} breaches 5% ({frac})")


@given(BANKROLL,
       st.floats(min_value=-0.4, max_value=0.6),
       st.floats(min_value=0.05, max_value=0.99),
       st.floats(min_value=0.0, max_value=1.0))
@settings(max_examples=200, deadline=None)
def test_calculate_units_cap_always_binds(bankroll, edge, confidence, kf):
    try:
        u = calculate_units(bankroll, edge, confidence, kelly_fraction=kf)
    except (ValueError, OverflowError):
        return
    if u.get("error"):
        return
    assert u["dollar_amount"] <= bankroll * 0.05 + 0.01, (
        f"calculate_units staked ${u['dollar_amount']}/{bankroll}")


@given(st.lists(st.fixed_dictionaries({
                    "edge": st.floats(min_value=0.0, max_value=0.15),
                    "odds": st.sampled_from([-110, 100, 150]),
                    "confidence_score": st.just(0.95),
                    "correlation_with_others": st.floats(min_value=0, max_value=1),
                }), min_size=1, max_size=40))
@settings(max_examples=150, deadline=None)
def test_portfolio_total_allocation_capped(bets):
    """H3+H5 combined: sum of final fractions must respect the portfolio
    cap (20%) regardless of N, correlation mix, or float accumulation."""
    res = kelly_portfolio(bets)
    total = math.fsum(r["final_fraction"] for r in res)
    assert total <= 0.20 + 1e-6, (
        f"{len(bets)} bets summed to {total:.6f} of bankroll — above the "
        f"20% portfolio cap (cap_hit flag said "
        f"{res[0]['portfolio_summary']['cap_hit']})")
    for r in res:
        assert r["final_fraction"] <= 0.05 + 1e-6


def test_portfolio_cap_binds_with_many_small_stakes():
    """Deterministic version of H5: 40 identical small correlated bets each
    pass their own cap but the SUM must not exceed the portfolio cap."""
    bets = [{"edge": 0.03, "odds": -105, "confidence_score": 0.95,
             "correlation_with_others": 0.9} for _ in range(40)]
    res = kelly_portfolio(bets)
    total = sum(r["final_fraction"] for r in res)
    assert total <= 0.20 + 1e-6, (
        f"40 small stakes summed to {total:.4%} of bankroll — float-scale "
        f"leakage through the portfolio cap")


# ---------------------------------------------------------------------------
# H2d: fractional-Kelly ordering — quarter of capped vs capped quarter
# ---------------------------------------------------------------------------

@given(st.floats(min_value=0.0, max_value=0.5),
       st.sampled_from([-110, 100, 200]),
       st.floats(min_value=0.01, max_value=1.0))
@settings(max_examples=200, deadline=None)
def test_fractional_kelly_monotone_in_fraction(edge, odds, fraction):
    full = kelly_full(edge, odds)
    frac = kelly_fractional(edge, odds, fraction)
    assert abs(frac - round(full * fraction, 6)) < 1e-9
    assert 0.0 <= frac <= full + 1e-12
