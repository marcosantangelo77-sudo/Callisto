"""RED TEAM — money path property sweeps (READ-ONLY: no execution path armed).

Surface: the sizing/CLV arithmetic — tools/kelly.py, tools/clv_tracker.py,
tools/order_reconciler.py, tools/hypothesis.py CLV gate, tools/cache_manager.py.

Method: property-based sweeps over a parameter space, attacking INVARIANTS
the module docstrings claim:

  I1. "A clamp may never move a stake UP." (kelly_full/kelly_dynamic caps)
  I2. "Kelly fraction is monotone non-decreasing in edge."
  I3. "Every numeric input path is finite-safe: NaN/Inf cannot produce a
      positive stake or a positive Kelly fraction."  (adversarial numerics)
  I4. "clv_prob_bp in clv_log has ONE unit and ONE sign convention across
      all three writers."  (cross-module: same rule implemented twice)
  I5. "A bet at the SAME price as its close shows ~zero CLV regardless of
      which book each came from."  (vig asymmetry must not mint signal)
  I6. "The promotion gate's canonical CLV rate counts only rows written by
      the canonical devigged writer."

Run: python3 -m pytest tests/test_redteam_money_sweeps.py -q
"""

import math
import random

import pytest

from tools.kelly import (
    AGP_TIER_MULTIPLIERS,
    _confidence_tier_from_score,
    calculate_units,
    kelly_dynamic,
    kelly_fractional,
    kelly_full,
    kelly_portfolio,
)
from tools.odds_api import calculate_implied_probability
from tools.clv_tracker import _half_vig_devig

ODDS = [-400, -300, -200, -150, -110, -105, 100, 105, 110, 150, 200, 300, 500, 1000]


# ---------------------------------------------------------------------------
# I2 — monotonicity in edge
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("odds", ODDS)
def test_kelly_full_monotone_in_edge(odds):
    prev = -1.0
    for i in range(200):
        e = -0.4 + i * 0.005
        v = kelly_full(e, odds)
        assert v >= prev - 1e-9, f"edge={e} odds={odds}: {prev} -> {v}"
        prev = v


# ---------------------------------------------------------------------------
# I1 — caps and bounds
# ---------------------------------------------------------------------------

def test_kelly_full_never_negative_anywhere():
    rng = random.Random(7)
    for _ in range(5000):
        e = rng.uniform(-0.6, 0.8)
        o = rng.choice(ODDS)
        assert kelly_full(e, o) >= 0.0


def test_calculate_units_cap_holds_across_space():
    rng = random.Random(11)
    for _ in range(3000):
        r = calculate_units(
            bankroll=rng.uniform(10, 1e7),
            edge=rng.uniform(0, 0.5),
            confidence=rng.random(),
        )
        assert r["pct_of_bankroll"] <= 5.0 + 1e-6


# ---------------------------------------------------------------------------
# I3 — finite-safety: NaN / Inf must not size a bet
# ---------------------------------------------------------------------------

def test_kelly_full_nan_edge_returns_zero_not_one():
    """kelly_full(nan, -110) == 1.0 today: p = implied + nan = nan;
    max(0.0, min(1.0, nan)) == 1.0 (min/max with NaN are order-dependent),
    so a NaN edge sizes FULL KELLY on the bankroll."""
    assert kelly_full(float("nan"), -110) == 1.0  # documents the defect


def test_kelly_dynamic_nan_edge_sizes_max_bet():
    r = kelly_dynamic(edge=float("nan"), odds=-110, confidence_score=0.8,
                      variance_estimate=0.01, bankroll=10000)
    assert r["stake"] > 0          # DEFECT: NaN edge produced a live stake
    assert r["stake"] == 500.0     # ...at the hard cap


def test_kelly_dynamic_inf_edge_sizes_max_bet():
    r = kelly_dynamic(edge=float("inf"), odds=-110, confidence_score=0.8,
                      variance_estimate=0.01, bankroll=10000)
    assert r["stake"] == 500.0     # DEFECT: inf edge capped to max bet, not zero


def test_calculate_units_nan_edge_is_no_bet():
    r = calculate_units(bankroll=10000, edge=float("nan"), confidence=0.8)
    assert r["dollar_amount"] == 0.0   # this sibling DOES fail closed


def test_nan_edge_inconsistency_between_sizers():
    """Same NaN input: kelly_dynamic bets $500, calculate_units bets $0.
    Two copies of 'no edge -> no bet' disagree; the dangerous one feeds
    compute_stake()."""
    dyn = kelly_dynamic(edge=float("nan"), odds=-110, confidence_score=0.8,
                        variance_estimate=0.01, bankroll=10000)
    units = calculate_units(bankroll=10000, edge=float("nan"), confidence=0.8)
    assert dyn["stake"] > 0
    assert units["dollar_amount"] == 0.0


# ---------------------------------------------------------------------------
# I5 — vig-asymmetry phantom CLV
# ---------------------------------------------------------------------------

def test_same_price_same_close_retail_vs_sharph_mints_positive_clv():
    """Bet -110 at a retail book (5% vig), close -110 at Pinnacle (2.5%).
    The price did NOT move. Canonical writer (_log_clv) still logs ~+63bp of
    positive CLV — pure book-vig arithmetic, no signal. Over the MIN_CLV_RATE
    gate's 50% positive-rate bar this hands every retail-book bet free credit."""
    place = _half_vig_devig(round(calculate_implied_probability(-110), 4), 0.05)
    close = _half_vig_devig(round(calculate_implied_probability(-110), 4), 0.025)
    bp = round((close - place) * 10000, 1)
    assert bp > 50  # DEFECT: >half a point of probability from nothing


def test_vig_asymmetry_phantom_exceeds_real_edges():
    """The phantom (+63bp) is the same order as a genuinely good bet's CLV
    (~+30-60bp vs close). The noise floor sits AT the signal level."""
    place = _half_vig_devig(round(calculate_implied_probability(-110), 4), 0.05)
    close = _half_vig_devig(round(calculate_implied_probability(-110), 4), 0.025)
    phantom = (close - place) * 10000
    assert abs(phantom) > 30


# ---------------------------------------------------------------------------
# I4/I6 — cross-module unit/sign audit of the three clv_log writers
# ---------------------------------------------------------------------------

def _writer_signatures():
    """What each clv_log writer actually computes for clv_prob_bp."""
    imp = lambda p: (-p if p < 0 else p) / ((abs(p)) + 100.0)

    # clv_tracker._log_clv: devigged both sides, closing_fair - placement_fair
    def tracker(place_odds, close_odds, pv=0.05, cv=0.025):
        pf = _half_vig_devig(round(imp(place_odds), 4), pv)
        cf = _half_vig_devig(round(imp(close_odds), 4), cv)
        return round((cf - pf) * 10000, 1)

    # order_reconciler._record_clv: RAW implied delta, NO devig anywhere.
    # Also NOTE THE SIGN: closing_implied - placement_implied.
    def reconciler(place_odds, close_odds):
        return round((imp(close_odds) - imp(place_odds)) * 10000, 1)

    return tracker, reconciler


def test_two_writers_disagree_on_identical_price_move():
    tracker, reconciler = _writer_signatures()
    # Bet -110, closed -120 (line moved against us → negative true CLV).
    t = tracker(-110, -120)
    r = reconciler(-110, -120)
    # Same market, same prices, two "canonical" columns: different values.
    # Reconciler's raw delta overstates magnitude by the removed-vig gap.
    assert t != r
    assert abs(r - t) > 20  # tens of basis points apart on the same event


def test_gate_reads_both_writers_as_one_statistic():
    """tools/resolvers/betting.py mean_clv_prob_bp() AVGs the whole column and
    tools/hypothesis.py's CLV gate treats >=3 rows as 'canonical'. Nothing in
    that query distinguishes devigged rows from order_reconciler's raw rows:
    one raw writer inside the sample poisons the gate statistic silently."""
    # Documented via source inspection: order_reconciler.py:626-628 writes
    # raw-implied delta into clv_log.clv_prob_bp; betting.py:83-88 reads
    # AVG(clv_prob_bp) with no writer filter. Assert the structural fact:
    import inspect
    import tools.order_reconciler as orc
    src = inspect.getsource(orc._record_clv)
    assert "_half_vig_devig" not in src and "devig" not in src.lower()
    import tools.resolvers.betting as bet
    bsrc = inspect.getsource(bet.BettingOutcomeResolver.mean_clv_prob_bp)
    assert "close_reliable" not in bsrc  # not even reliability-filtered


def test_legacy_fallback_reads_raw_implied_column():
    """hypothesis.py's legacy fallback uses get_clv_report()['positive_clv_rate'],
    computed from bets.clv_implied — RAW implied deltas (clv_tracker.py:295).
    The gate can therefore pass paper→live on exactly the statistic the audit
    already condemned as carrying a 1-4% phantom edge."""
    import inspect, tools.hypothesis as hyp
    src = inspect.getsource(hyp.PromotionGate.evaluate if hasattr(hyp, "PromotionGate") else hyp)
    assert "positive_clv_rate" in src  # fallback path present


# ---------------------------------------------------------------------------
# Honest negatives / pins
# ---------------------------------------------------------------------------

def test_pin_kelly_full_reference_values():
    assert kelly_full(0.03, -110) == pytest.approx(0.063, abs=1e-6)
    assert kelly_full(0.05, 150) == pytest.approx(0.0833333, abs=1e-6)


def test_pin_unverified_confidence_bets_nothing():
    r = kelly_dynamic(edge=0.03, odds=-110, confidence_score=0.10,
                      variance_estimate=0.01, bankroll=10000)
    assert r["stake"] == 0.0


def test_pin_half_vig_devig_direction():
    # fair < raw always for positive vig
    raw = 0.55
    assert _half_vig_devig(raw, 0.05) < raw


def test_pin_tier_table_matches_boundaries():
    assert _confidence_tier_from_score(0.90) == "VERIFIED"
    assert _confidence_tier_from_score(0.75) == "CORROBORATED"
    assert AGP_TIER_MULTIPLIERS["UNVERIFIED"] == 0.0
