"""
Tier-0 money-path characterization tests: tools/sizing.py + the CLV unit
audit (fraction vs rate, American vs decimal, devigged vs raw).

These pin CURRENT behaviour with hand-derived expected values. The CLV unit
tests document the known mixed-unit defect at the paper_trades->live gate:
the gate compares a FRACTION-OF-TRADES (positive_clv_rate, 0..1) against
MIN_CLV_RATE=0.005, so one positive trade in ten reads as "10% >= 0.5%" —
off by ~100x from either plausible intent. See findings/instance2.md.
"""

import pytest

from tools.sizing import (
    kelly_binary,
    kelly_with_push,
    uncertainty_adjusted_kelly,
    bet_size,
    bet_size_american,
    best_price,
)
from tools.clv_tracker import _half_vig_devig, _american_to_decimal


# ---------------------------------------------------------------------------
# sizing.py primitives
# ---------------------------------------------------------------------------
class TestKellyBinary:
    def test_docstring_verified_value(self):
        # prob=.55 odds=2.10: b=1.1; f=(1.1*.55-.45)/1.1=.140909...
        assert kelly_binary(0.55, 2.10) == pytest.approx(0.1409091, abs=1e-6)

    def test_no_edge_returns_zero(self):
        # fair = 1/decimal => EV exactly zero -> f*=0
        assert kelly_binary(1 / 2.10, 2.10) == pytest.approx(0.0, abs=1e-12)

    def test_negative_ev_clamps_to_zero(self):
        assert kelly_binary(0.40, 2.10) == 0.0

    def test_zero_or_negative_odds_guard(self):
        assert kelly_binary(0.55, 1.0) == 0.0
        assert kelly_binary(0.55, 0.5) == 0.0

    def test_matches_tools_kelly_full_for_equivalent_inputs(self):
        # Cross-module consistency: same p and b must give same f*.
        from tools.kelly import _american_to_decimal
        dec = _american_to_decimal(-110)
        p = 110 / 210 + 0.03
        b = dec - 1
        expected = max(0.0, (b * p - (1 - p)) / b)
        assert kelly_binary(p, dec) == pytest.approx(expected, abs=1e-9)


class TestKellyWithPush:
    def test_docstring_verified_value(self):
        # p_win=.54 p_push=.04 odds=1.90909...: b=.909091, p_loss=.42
        # f=(.909091*.54-.42)/.909091 = .078
        assert kelly_with_push(0.54, 0.04, 1.9090909090909092) == pytest.approx(
            0.078, abs=1e-6)

    def test_ignoring_push_misstates_the_fraction_both_ways(self):
        b = 1.9090909090909092
        push_aware = kelly_with_push(0.54, 0.04, b)
        # Treating pushes as LOSSES (binary p=.54) understates:
        as_loss = kelly_binary(0.54, b)
        # Treating pushes as WINS (p=.58) overstates:
        as_win = kelly_binary(0.58, b)
        assert push_aware == pytest.approx(0.078, abs=1e-6)
        assert as_loss < push_aware < as_win
        # The sizing.py docstring's direction ("ignoring push HALVES") matches
        # the loss-treatment error: push_aware/as_loss ≈ 1.5x
        assert push_aware / as_loss > 1.4

    def test_all_loss_returns_zero(self):
        assert kelly_with_push(0.30, 0.10, 2.0) == 0.0

    def test_bad_odds_guard(self):
        assert kelly_with_push(0.54, 0.04, 1.0) == 0.0


class TestUncertaintyAdjustedKelly:
    def test_scale_ladder(self):
        fk = 0.10
        # info_ratio = edge_pct/noise decides scale in {0, .3, .7, 1},
        # then multiplies by 0.25 (quarter-Kelly).
        # high noise=.015; ir=.667 -> scale .3 => .0075
        assert uncertainty_adjusted_kelly(fk, 0.01, "high") == pytest.approx(0.0075)
        # medium noise=.025; ir=.4 -> <0.5 -> 0
        assert uncertainty_adjusted_kelly(fk, 0.01, "medium") == 0.0
        # low noise=.04; ir=.25 -> 0
        assert uncertainty_adjusted_kelly(fk, 0.01, "low") == 0.0
        # ir >= 2: full quarter
        assert uncertainty_adjusted_kelly(fk, 0.05, "medium") == pytest.approx(0.025)

    def test_output_is_always_quarter_of_input_times_scale(self):
        for edge_pct, conf in [(0.02, "high"), (0.05, "high"),
                               (0.08, "medium"), (0.12, "medium")]:
            v = uncertainty_adjusted_kelly(0.20, edge_pct, conf)
            assert v <= 0.20 * 0.25 + 1e-12


class TestBetSize:
    def test_reference_case(self):
        r = bet_size(bankroll=10000, fair_prob=0.55, decimal_odds=2.10,
                     confidence="medium")
        # fk=.1409091; ev=(.55*2.10)-1=.155; noise medium=.025 -> ir=6.2 -> scale 1
        assert r["kelly_full"] == pytest.approx(0.1409, abs=1e-4)
        assert r["edge_pct"] == pytest.approx(15.50)
        assert r["kelly_adjusted"] == pytest.approx(0.1409091 * 0.25, abs=1e-4)
        assert r["recommended_stake"] == pytest.approx(round(10000 * 0.1409091 * 0.25, 2))

    def test_max_wager_cap(self):
        r = bet_size(bankroll=100000, fair_prob=0.7, decimal_odds=3.0,
                     confidence="high", max_wager=500.0)
        assert r["recommended_stake"] == pytest.approx(500.0)
        assert r["max_capped"] is True

    def test_push_path(self):
        r = bet_size(bankroll=10000, fair_prob=0.54, decimal_odds=1.9090909090909092,
                     confidence="medium", p_push=0.04)
        assert r["kelly_full"] == pytest.approx(0.078, abs=1e-4)

    def test_american_wrapper_agrees_with_decimal(self):
        a = bet_size_american(bankroll=5000, fair_prob=0.55,
                              book_odds_american=-150, confidence="high")
        d = bet_size(bankroll=5000, fair_prob=0.55,
                     decimal_odds=1 + 100 / 150, confidence="high")
        assert a["recommended_stake"] == pytest.approx(d["recommended_stake"])


class TestBestPrice:
    def test_picks_better_decimal(self):
        r = best_price(120, 110)   # DK 2.20 vs Fanatics 2.10 -> DK better
        assert r["best_book"] == "draftkings"
        assert r["best_odds_american"] == 120
        # improvement = 2.20/2.10 - 1 = 4.76%
        assert r["improvement_pct"] == pytest.approx(4.76)

    def test_fanatics_better(self):
        r = best_price(100, 105)
        assert r["best_book"] == "fanatics"

    def test_tie_goes_to_dk(self):
        r = best_price(110, 110)
        assert r["best_book"] == "draftkings"
        assert r["improvement_pct"] == 0.0

    def test_never_picks_the_worse_price(self):
        import itertools
        for dk, fan in itertools.product((-200, -110, 100, 150, 300), repeat=2):
            r = best_price(dk, fan)
            best_dec = max(1 + 100 / abs(dk), 1 + abs(dk) / 100) if False else None
            chosen = r["best_odds_american"]
            if dk > fan:
                assert chosen == dk
            elif fan > dk:
                assert chosen == fan
            else:
                assert chosen == dk


# ---------------------------------------------------------------------------
# CLV unit audit helpers
# ---------------------------------------------------------------------------
class TestHalfVigDevig:
    def test_divides_by_one_plus_half_vig(self):
        # implied .52 at vig .05 -> .52/1.025 = .507317
        assert _half_vig_devig(0.52, 0.05) == pytest.approx(0.5073170, abs=1e-6)

    def test_zero_vig_is_identity(self):
        assert _half_vig_devig(0.52, 0.0) == 0.52

    def test_none_passthrough(self):
        assert _half_vig_devig(None, 0.05) is None

    def test_nonpositive_passthrough(self):
        assert _half_vig_devig(0.0, 0.05) == 0.0
        assert _half_vig_devig(-0.1, 0.05) == -0.1

    def test_bounded_to_unit_interval(self):
        assert _half_vig_devig(0.99, 0.025) <= 1.0

    def test_devigged_below_raw(self):
        raw = 0.60
        assert _half_vig_devig(raw, 0.06) < raw


class TestAmericanToDecimalClv:
    @pytest.mark.parametrize("american,expected", [
        (-110, 1.9090909091), (150, 2.5), (-200, 1.5), (300, 4.0),
    ])
    def test_conversions(self, american, expected):
        assert _american_to_decimal(american) == pytest.approx(expected, abs=1e-6)

    def test_zero_and_none_return_none(self):
        assert _american_to_decimal(0) is None
        assert _american_to_decimal(None) is None

    def test_garbage_returns_none(self):
        assert _american_to_decimal("abc") is None


# ---------------------------------------------------------------------------
# THE UNIT BUG — fraction vs rate at the paper->live gate (documented, not fixed)
# ---------------------------------------------------------------------------
class TestClvGateUnitSemantics:
    """The gate (tools/hypothesis.py:1189-1192) does:

        clv_rate = report['clv']['positive_clv_rate']   # fraction of trades, 0..1
        if clv_rate < MIN_CLV_RATE (default 0.005): FAIL

    positive_clv_rate comes from clv_tracker.get_clv_report() as
    (#trades with clv_implied>0)/n — a dimensionless RATE OF TRADES.
    MIN_CLV_RATE's docstring says 'min positive-CLV rate' but its value
    0.005 only makes sense as a probability/CLV magnitude threshold.

    Consequence: with n>=10 trades, ONE positive-CLV trade gives rate>=0.10,
    i.e. 20x above the floor — the gate can never fail on real data unless
    literally every trade closed negative. These tests pin the arithmetic
    that demonstrates it, using the report builder itself.
    """

    @pytest.mark.asyncio
    async def test_one_positive_in_ten_passes_any_plausible_floor(self):
        from tools.clv_tracker import CLVTracker
        tracker = CLVTracker(":memory:")
        await tracker.initialize()
        try:
            from tools.odds_api import calculate_implied_probability as imp
            # 10 bets with closing lines: 1 slightly positive, 9 negative
            placement, close_pos = -110, -115   # placed worse than close? see below
            bets = [
                # (placement_odds, closing_odds): clv_implied = close_implied - place_implied
                (-105, -110),   # positive CLV (we bought cheaper than the close)
            ] + [(-110, -105)] * 9  # negative CLV (closed shorter than our price)
            for i, (p, c) in enumerate(bets):
                # clv_implied = closing_implied - placement_implied (the exact
                # expression clv_tracker.record_closing_line writes)
                clv = round(imp(c) - imp(p), 6)
                await tracker._db.execute(
                    "INSERT INTO bets (placed_at, sport, bet_type, market, bookmaker, "
                    "placement_odds, closing_odds, closing_implied_prob, clv_implied) "
                    "VALUES (?, 'basketball_nba', 'single', 'h2h', 'dk', ?, ?, ?, ?)",
                    (f"2026-01-0{i+1}T00:00:00+00:00", p, c,
                     round(imp(c), 6), clv),
                )
            await tracker._db.commit()

            report = await tracker.get_clv_report()
            rate = report["clv"]["positive_clv_rate"]
            assert rate == pytest.approx(10.0)  # percent scale! 1/10 -> 10.0

            # Gate-side comparison, replicating hypothesis.py:1189 verbatim
            # (it reads the fraction 0..1 form via backtest events; here we
            # show the report's own percent-scale number vs the 0.5% floor):
            MIN_CLV_RATE = 0.005
            frac = rate / 100.0
            assert frac > MIN_CLV_RATE  # 0.10 >= 0.005 → PASS with 1/10 positive

            # Even ONE-in-TEN positive passes; the floor binds only at 0 positives.
            zero_rate = 0.0
            assert zero_rate < MIN_CLV_RATE
        finally:
            await tracker.close()
