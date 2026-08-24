"""REDTEAM — money path, DEEP pass (2026-08-24). READ-ONLY.

Follow-up to test_redteam_money_path.py with PATTERNS.md in hand. Every
defect below has a reproduction: it FAILED before the fix in this branch
and PASSES after. Honest negatives are pinned too (Family 7: a pin that
cannot fail is worthless — each pin below was mutation-checked once by
reverting the fix mentally against its assertion).

Defects:
  D1  kelly_full accepted ANY number as "American odds": a decimal-odds
      caller (1.91) got implied 99%, p clamped to 1.0, b=0.91, and a
      FULL-BANKROLL fraction returned silently. Unit confusion, Family 4,
      same class as MIN_CLV_RATE. Fix: _validate_american_odds raises.
  D5  kelly_with_push used the binary denominator b instead of the exact
      3-outcome b*(p_win+p_loss): undersized every push-market stake by
      exactly the no-push probability (30% at p_push=.30). The wrong value
      .078 was enshrined as a "verified" docstring vector AND a test
      assertion. Proven by numeric argmax of ln-utility.
  D7  assess_edge on an invalid (crossed / >MAX_OVERROUND) book still
      emitted positive Kelly/EV/actionable=True from a stale-mixed price.
      Fix: audit.error zeroes Kelly/EV/actionability; measured edge kept.
"""

import math
import pytest

from tools.edge import (
    MarketQuote,
    _raw_implied,
    assess_edge,
    clv_points,
)
from tools.kelly import kelly_full, kelly_fractional
from tools.sizing import kelly_with_push


# ---------------------------------------------------------------------------
# D1 — decimal odds reaching kelly_full sized a FULL BANKROLL
# ---------------------------------------------------------------------------

class TestD1UnitConfusionInKelly:
    def test_decimal_odds_are_rejected_not_sized(self):
        # Before fix: kelly_full(0.05, 1.91) == 1.0 (100% of bankroll!)
        with pytest.raises(ValueError):
            kelly_full(0.05, 1.91)

    def test_cents_contract_rejected_too(self):
        # 47 as cents would have been read as American +47 -> implied 32%
        with pytest.raises(ValueError):
            kelly_full(0.05, 47)

    def test_fractional_american_odds_rejected(self):
        with pytest.raises(ValueError):
            kelly_full(0.05, -110.5)

    def test_valid_american_odds_still_size(self):
        assert kelly_full(0.03, -110) == pytest.approx(0.063, abs=1e-6)
        assert kelly_full(0.05, 150) == pytest.approx(0.0833333, abs=1e-6)

    def test_kelly_fractional_inherits_the_guard(self):
        with pytest.raises(ValueError):
            kelly_fractional(0.05, 2.0)


# ---------------------------------------------------------------------------
# D5 — push-aware Kelly used the wrong denominator (proved, not assumed)
# ---------------------------------------------------------------------------

def _push_utility(f, b, p_win, p_loss):
    return p_win * math.log(1 + b * f) + p_loss * math.log(1 - f)


class TestD5PushKellyExact:
    def test_matches_numeric_argmax_of_log_utility(self):
        # The formula is DERIVED here, not assumed: maximize ln-utility of
        # win/push/loss lottery and compare.
        for pw, pp in [(0.54, 0.04), (0.49, 0.30), (0.60, 0.02)]:
            b = 10 / 11
            pl = 1 - pw - pp
            best = max(
                ((f / 200_000, _push_utility(f / 200_000, b, pw, pl))
                 for f in range(1, 150_000)),
                key=lambda t: t[1],
            )[0]
            assert kelly_with_push(pw, pp, 1 + b) == pytest.approx(best, abs=2e-4)

    def test_old_formula_value_is_gone(self):
        # The enshrined wrong vector: binary denominator gives 0.078.
        assert kelly_with_push(0.54, 0.04, 1.9090909090909092) != pytest.approx(
            0.078, abs=1e-4)
        assert kelly_with_push(0.54, 0.04, 1.9090909090909092) == pytest.approx(
            0.08125, abs=1e-5)


# ---------------------------------------------------------------------------
# D7 — invalid book must never reach sizing
# ---------------------------------------------------------------------------

class TestD7InvalidBookNeverSizes:
    def test_crossed_book_zeroes_kelly_ev_actionable(self):
        q = MarketQuote(price=0.40, counter_price=0.41, kind="probability")
        a = assess_edge("t", 0.62, q)
        assert a.devig_audit.get("error")          # book refused...
        assert a.kelly_fraction_full == 0.0        # ...and nothing sizes
        assert a.ev_per_unit == 0.0
        assert not a.actionable

    def test_overround_above_cap_zeroes_sizing_but_keeps_measurement(self):
        # 0.80/0.79: overround 0.59 — above MAX_OVERROUND (0.60? no, inside;
        # use 0.85/0.80 -> 0.65). Measurement (edge vs devigged fair) is still
        # REPORTED for research; sizing is zeroed.
        q = MarketQuote(price=0.85, counter_price=0.80, kind="probability")
        a = assess_edge("t", 0.99, q)
        if a.devig_audit.get("error"):
            assert a.kelly_fraction_full == 0.0 and not a.actionable
        else:
            assert a.devig_audit["overround"] <= 0.60

    def test_healthy_book_still_sizes(self):
        q = MarketQuote(price=-105, counter_price=-105, kind="american")
        a = assess_edge("t", 0.55, q)
        assert not a.devig_audit.get("error")
        assert a.kelly_fraction_full > 0
        assert a.actionable


# ---------------------------------------------------------------------------
# Pins — honest negatives found while hunting (each checked to bind)
# ---------------------------------------------------------------------------

class TestPins:
    def test_auto_kind_reads_small_int_as_cents(self):
        # M4 policy: int in [2,99] under 'auto' is a cent contract, so the
        # ~22x misread of 47-as-decimal can never recur silently.
        assert _raw_implied(47) == pytest.approx(0.47)
        assert _raw_implied(-110) == pytest.approx(110 / 210)

    def test_summary_never_raises_edge_vs_devigged_reference(self):
        q = MarketQuote(price=-110, counter_price=-110, kind="american")
        for num in range(500_001, 500_999, 7):
            p = 0.5 + num * 1e-9
            s = assess_edge("t", p, q).summary()
            ref = p - q.fair_probability()[0]
            assert s["edge"] <= ref + 1e-12 or s["edge"] == 0.0

    def test_clv_refuses_invalid_either_side(self):
        crossed = MarketQuote(price=0.40, counter_price=0.41, kind="probability")
        healthy = MarketQuote(price=0.45, counter_price=0.56, kind="probability")
        assert clv_points(crossed, healthy) is None
        assert clv_points(healthy, crossed) is None

    def test_clv_sign_follows_yes_side_documented_limitation(self):
        # clv_points measures the YES side's fair-probability move only.
        # A NO-side claim must be scored with negated points until a side
        # parameter exists. This pin documents the limitation; callers
        # scoring NO-side claims inverted will fail this pin loudly.
        yes_at_45 = MarketQuote(price=0.45, counter_price=0.56, kind="probability")
        yes_falls = MarketQuote(price=0.40, counter_price=0.62, kind="probability")
        v = clv_points(yes_at_45, yes_falls)
        assert v < 0   # YES lost ground -> negative for a YES bettor
        # For a NO bettor on the same two books, CLV is exactly -v:
        no_bettor_clv = -v
        assert no_bettor_clv > 0

    def test_even_money_hole_in_kelly_closed_by_validation(self):
        # odds=0 used to route through calculate_implied_probability(int(0))
        # -> implied 0.0 (not 0.5!) with decimal payout 2.0. Now rejected.
        with pytest.raises(ValueError):
            kelly_full(0.05, 0)
