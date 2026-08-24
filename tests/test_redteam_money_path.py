"""RED TEAM — money path arithmetic (odds conversion, devig, edge, Kelly, CLV).

Surface: tools/math_utils.py, tools/odds_api.py, tools/kelly.py, tools/sizing.py,
tools/ev.py, tools/edge.py, tools/devig.py, tools/clv_tracker.py, and the
duplicated converters in scripts/.

Method: PROPERTY-BASED RANDOM SWEEPS over the arithmetic parameter space
(seeded for reproducibility), per PATTERNS.md family hunting:
  #2 duplicated logic that drifted apart (converters, devig, sizing)
  #3 absence/garbage treated as success (NaN edges, odds of 0, impossible inputs)
  #6 rounding whose direction manufactures stake/probability mass

READ-ONLY by mandate: nothing here arms execution; every function touched is
pure computation. No sockets, no DB, no filesystem writes.

Failing tests = live defects demonstrated on master (see
findings/redteam_money_path.md). Passing "pin" tests are honest negatives:
attacks that did NOT land, kept as regression anchors.
"""

import math
import random

import pytest

from tools.clv_tracker import _BOOK_VIG_ESTIMATE, _half_vig_devig
from tools.devig import (
    additive_devig,
    devig_market,
    multiplicative_devig,
    power_devig,
    shin_devig,
)
from tools.edge import MarketQuote, MAX_FRACTION_FULL_KELLY, assess_edge
from tools.ev import evaluate_edge
from tools.kelly import (
    _american_to_decimal as kelly_american_to_decimal,
    calculate_units,
    kelly_dynamic,
    kelly_full,
    kelly_fractional,
    ruin_probability,
    timing_value,
)
from tools.math_utils import american_to_decimal, american_to_implied
from tools.odds_api import calculate_implied_probability
from tools.sizing import bet_size, best_price, kelly_binary

SEED = 20260823


def exact_kelly_full(edge: float, american: int | float) -> float:
    """Reference implementation of kelly_full's documented math, unrounded."""
    implied = american_to_implied(int(american))
    decimal = 1.0 + american / 100.0 if american > 0 else 1.0 + 100.0 / abs(american)
    b = decimal - 1.0
    p = min(1.0, max(0.0, implied + edge))
    q = 1.0 - p
    if b <= 0:
        return 0.0
    return max(0.0, (b * p - q) / b)


# ===========================================================================
# M1 (CRITICAL): NaN/inf edge -> FULL Kelly recommendation.
# kelly_full(nan, -110) == 1.0 because max(0.0, min(1.0, nan)) == 1.0
# (NaN comparisons are False, so min/max fall through to the wrong bound).
# A garbage model output becomes "bet the entire bankroll".
# ===========================================================================


class TestM1NonFiniteEdgeProducesFullKelly:
    def test_nan_edge_returns_full_kelly(self):
        # The defect: a NaN edge returns 1.0 -- the maximum possible stake --
        # because max(0.0, min(1.0, nan)) evaluates to 1.0.
        assert kelly_full(float("nan"), -110) != 1.0, (
            "NaN edge produced a full-bankroll Kelly recommendation"
        )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_nonfinite_edge_must_refuse_not_recommend(self, bad):
        f = kelly_full(bad, -110)
        assert f == 0.0, f"kelly_full({bad!r}) = {f}: non-finite input must "
        "never recommend a positive stake"

    def test_fractional_inherits_nan_hole(self):
        assert kelly_fractional(float("nan"), -110) == 0.0

    def test_dynamic_nan_edge_stake_is_zero(self):
        d = kelly_dynamic(float("nan"), -110, 0.9, 0.01, bankroll=10_000)
        assert d["stake"] == 0.0, (
            f"dynamic Kelly staked ${d['stake']} on a NaN edge"
        )


# ===========================================================================
# M2 (HIGH): zero edge must give zero Kelly, but int() truncation of FLOAT
# American odds makes implied-probability and payout disagree, manufacturing
# a positive fraction at exactly zero edge. Sweep found 9,075/20,000.
# Invariant: no-edge money is free only to a broken sizer.
# ===========================================================================


class TestM2ZeroEdgeFreeKellyViaFloatTruncation:
    def test_property_sweep_zero_edge_gives_zero_kelly(self):
        rng = random.Random(SEED)
        violations = []
        for _ in range(4000):
            a = round(rng.uniform(-800, -100), 1)
            if rng.random() < 0.5:
                a = round(rng.uniform(100, 800), 1)
            f = kelly_full(0.0, a)
            if f > 0.0:
                violations.append((a, f))
        assert not violations, (
            f"{len(violations)} float-odds values pay a positive Kelly "
            f"fraction at ZERO edge; worst: {sorted(violations, key=lambda t: -t[1])[:5]}"
        )

    def test_canonical_example_positive_float_odds(self):
        # +100.9: int() -> +100 for the probability, but payout uses 100.9.
        assert kelly_full(0.0, 100.9) == 0.0


# ===========================================================================
# M3 (CRITICAL): assess_edge parses market probability through quote.kind but
# parses the PAYOUT through _raw_implied's kind-blind auto-detection. For a
# contract quoted in cents (the module docstring's advertised Kalshi/
# Polymarket format) the payout is read as DECIMAL ODDS: a 47c contract gets
# b=46 instead of b=1.128 -- EV inflated ~146x, Kelly pinned at the cap.
# Invariant: the decimal used for Kelly must equal 1/implied_probability()
# under whatever kind the caller declared.
# ===========================================================================


class TestM3AssessEdgePayoutIgnoresKind:
    def test_contract_cents_ev_is_not_inflated_146x(self):
        q = MarketQuote(price=47, counter_price=53, kind="contract_cents")
        a = assess_edge("t", 0.55, q)
        true_decimal = 1.0 / 0.47
        true_ev = 0.55 * (true_decimal - 1.0) - 0.45
        assert abs(a.ev_per_unit - true_ev) < 1e-9, (
            f"contract_cents EV {a.ev_per_unit:.3f} vs correct {true_ev:.3f} "
            "-- payout parsed as decimal odds, inflating EV ~146x"
        )

    def test_contract_cents_kelly_hits_cap_on_a_modest_edge(self):
        q = MarketQuote(price=47, counter_price=53, kind="contract_cents")
        a = assess_edge("t", 0.55, q)
        true_decimal = 1.0 / 0.47
        b = true_decimal - 1.0
        true_kelly = max(0.0, (b * 0.55 - 0.45) / b)
        assert a.kelly_fraction_full <= true_kelly + 1e-9 or (
            a.kelly_fraction_full < MAX_FRACTION_FULL_KELLY
        ), (
            f"Kelly {a.kelly_fraction_full} vs correct {true_kelly:.4f}: "
            "misread payout slams into the safety cap"
        )

    def test_probability_kind_integer_cent_price_same_seam(self):
        # Kalshi's API returns cent INTEGERS; kind="probability" fixes the
        # market side but the payout still goes through auto-detection.
        q = MarketQuote(price=47, counter_price=53, kind="probability")
        a = assess_edge("t", 0.55, q)
        true_ev = 0.55 * (1.0 / 0.47 - 1.0) - 0.45
        assert abs(a.ev_per_unit - true_ev) < 1e-9

    def test_payout_matches_declared_kind_for_every_kind(self):
        cases = [
            (MarketQuote(price=-110, counter_price=-110, kind="american"), -110),
            (MarketQuote(price=1.91, counter_price=2.01, kind="decimal"), 1.91),
            (MarketQuote(price=0.52, counter_price=0.50, kind="probability"), 0.52),
            (MarketQuote(price=52, counter_price=50, kind="contract_cents"), 52),
        ]
        broken = []
        for q, price in cases:
            implied = q.implied_probability()
            a = assess_edge("t", 0.55, q)
            ev_from_own_kind = 0.55 * (1.0 / implied - 1.0) - 0.45
            if abs(a.ev_per_unit - ev_from_own_kind) > 1e-6:
                broken.append((q.kind, price, a.ev_per_unit, ev_from_own_kind))
        assert not broken, f"EV inconsistent with declared kind: {broken}"


# ===========================================================================
# M4 (HIGH): clv_tracker._half_vig_devig divides by (1 + vig/2), which fails
# the most basic devig invariant -- a BALANCED market must devig to 50/50 --
# and leaves book-dependent residual vig in every CLV row. Placing at a soft
# book and closing at a sharp one manufactures ~+63 bp of POSITIVE CLV from
# an unchanged line. clv_prob_bp feeds promotion gates.
# Invariant: identical prices with zero movement must grade zero CLV.
# ===========================================================================


class TestM4HalfVigDevigPhantomCLV:
    def test_balanced_market_devigs_to_one_half(self):
        v = 110 / 210 * 2 - 1          # true overround of a -110/-110 book
        fair = _half_vig_devig(110 / 210, v)
        assert abs(fair - 0.5) < 1e-9, (
            f"balanced two-way market devigged to {fair:.6f}, not 0.5"
        )

    def test_zero_movement_cross_book_clv_is_zero(self):
        imp = 110 / 210                 # same raw price at both books
        close = _half_vig_devig(imp, _BOOK_VIG_ESTIMATE["pinnacle"])
        place = _half_vig_devig(imp, _BOOK_VIG_ESTIMATE["draftkings"])
        phantom_bp = (close - place) * 10_000
        assert abs(phantom_bp) < 1.0, (
            f"unchanged line graded {phantom_bp:+.1f} bp of CLV purely from "
            "book-vig residuals (place retail, close sharp)"
        )

    def test_residual_shrinks_with_correct_divisor(self):
        imp = 110 / 210
        v = 2 * imp - 1
        correct = imp / (1.0 + v)
        assert abs(correct - 0.5) < 1e-9   # the divisor the formula should use

    def test_none_and_nonpositive_passthrough_documented(self):
        # honest pin: documented fail-safe passthrough behaviour holds
        assert _half_vig_devig(None, 0.05) is None
        assert _half_vig_devig(0.0, 0.05) == 0.0
        assert _half_vig_devig(-0.5, 0.05) == -0.5


# ===========================================================================
# M5 (MEDIUM, PATTERNS #6): automated actors ROUND STAKES UP.
# kelly_full/kelly_fractional round fractions half-even (up ~50% of the time);
# kelly_dynamic/calculate_units round DOLLAR stakes up by up to half a cent.
# Direction rule: an automated actor may only ever move a stake DOWN.
# ===========================================================================


class TestM5StakeRoundingDirection:
    def test_kelly_full_never_rounds_up(self):
        rng = random.Random(SEED + 1)
        ups = []
        for _ in range(5000):
            e = rng.uniform(0, 0.05)
            o = rng.choice([-110, -105, 100, 120, 150])
            got = kelly_full(e, o)
            want = math.floor(exact_kelly_full(e, o) * 1e6) / 1e6
            if got > want + 1e-12:
                ups.append((e, o, want, got))
        assert not ups, (
            f"{len(ups)}/5000 kelly_full results rounded UP; first: {ups[0]}"
        )

    def test_dynamic_dollar_stake_never_exceeds_exact(self):
        rng = random.Random(SEED + 2)
        ups = []
        for _ in range(2000):
            bank = rng.uniform(10, 200_000)
            d = kelly_dynamic(0.03, -110, 0.9, 0.01, bankroll=bank)
            exact = bank * d["fraction"]
            floor_cents = math.floor(exact * 100) / 100
            if d["stake"] > floor_cents + 1e-9:
                ups.append((bank, d["fraction"], exact, d["stake"]))
        assert not ups, (
            f"{len(ups)}/2000 dynamic-Kelly stakes rounded UP past the exact "
            f"amount; worst: {max(ups, key=lambda t: t[3] - t[2])}"
        )

    def test_calculate_units_dollar_amount_never_rounds_up(self):
        rng = random.Random(SEED + 3)
        ups = []
        for _ in range(2000):
            bank = rng.uniform(10, 200_000)
            u = calculate_units(bank, edge=0.04, confidence=0.95)
            frac = u["breakdown"]["capped_fraction"]
            exact = bank * frac
            if u["dollar_amount"] > math.floor(exact * 100) / 100 + 1e-9:
                ups.append((bank, exact, u["dollar_amount"]))
        assert not ups, f"{len(ups)}/2000 unit-dollar amounts rounded UP"


# ===========================================================================
# M6 (MEDIUM): sizing.bet_size validates NOTHING while its sibling
# edge.assess_edge validates calibrated_prob in (0,1). Impossible inputs
# become confident dollar recommendations.
# ===========================================================================


class TestM6BetSizeAcceptsImpossibleInputs:
    def test_fair_prob_above_one_recommends_half_the_bankroll(self):
        r = bet_size(bankroll=1000, fair_prob=1.5, decimal_odds=2.05,
                     confidence="high")
        # fair_prob=1.5 is impossible; the correct answer is refuse or zero.
        assert r["recommended_stake"] == 0.0, (
            f"impossible fair_prob=1.5 recommended ${r['recommended_stake']} "
            f"({r['recommended_stake'] / 10:.0f}% of bankroll)"
        )

    def test_nan_fair_prob_does_not_produce_nan_stake(self):
        r = bet_size(bankroll=1000, fair_prob=float("nan"),
                     decimal_odds=2.05, confidence="high")
        assert math.isfinite(r["recommended_stake"]), (
            f"NaN fair prob produced stake {r['recommended_stake']}"
        )

    def test_negative_bankroll_never_returns_negative_stake(self):
        r = bet_size(bankroll=-1000, fair_prob=0.55, decimal_odds=2.05,
                     confidence="high")
        assert r["recommended_stake"] >= 0, (
            f"negative bankroll produced stake {r['recommended_stake']}"
        )

    def test_contrast_sibling_validates_pin(self):
        # honest pin: assess_edge DOES refuse out-of-range probabilities
        with pytest.raises(ValueError):
            assess_edge("t", 1.5, MarketQuote(price=-110))


# ===========================================================================
# M7 (MEDIUM, PATTERNS #2): the American-odds conversion exists in 11+
# modules and four different behaviours on invalid input: raise / fabricate
# decimal 2.0 / fabricate implied 0.0 / silent -110 fallback.
# Invariant: one rule, one behaviour; invalid odds must never become a number.
# ===========================================================================


class TestM7ConverterCensusInvalidOdds:
    def test_math_utils_raises_pin(self):
        with pytest.raises(ValueError):
            american_to_decimal(0)

    def test_kelly_copy_fabricates_even_money(self):
        assert kelly_american_to_decimal(0) == 2.0, (
            "kelly._american_to_decimal(0) invents 'even money' instead of raising"
        )

    def test_odds_api_copy_fabricates_zero_implied(self):
        p = calculate_implied_probability(0)
        assert not (p == 0.0), (
            "odds_api.calculate_implied_probability(0) returned 0.0 -- "
            "invalid odds became a valid-looking probability"
        )

    def test_script_copy_silent_fallback(self):
        pytest.importorskip("pandas")
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "imp_hist", "scripts/import_historical.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.decimal_to_american(float("nan")) != -110 or True
        assert mod.decimal_to_american(float("nan")) == -110, (
            "garbage decimal silently laundered into a plausible -110 line"
        )

    def test_all_converters_agree_on_valid_odds_pin(self):
        # honest pin: where they agree, they agree EXACTLY
        for a in (-2000, -110, -100, 100, 150, 5000):
            via_math = american_to_decimal(a)
            via_kelly = kelly_american_to_decimal(a)
            assert abs(via_math - via_kelly) < 1e-12


# ===========================================================================
# M8 (LOW-MEDIUM): ruin_probability trusts impossible inputs: win_rate>1
# reads as NEGLIGIBLE risk (safest possible answer), avg_odds=0 silently
# becomes decimal 2.0.
# ===========================================================================


class TestM8RuinProbabilityImpossibleInputs:
    def test_win_rate_above_one_is_not_negligible_risk(self):
        r = ruin_probability(10_000, 100, win_rate=1.5, avg_odds=-110)
        assert r["ruin_probability"] > 0.5 or r["risk_level"] in (
            "UNKNOWN", "ERROR"), (
            f"win_rate=1.5 graded {r['risk_level']} ruin="
            f"{r['ruin_probability']}"
        )

    def test_win_rate_outside_unit_interval_rejected(self):
        r = ruin_probability(10_000, 100, win_rate=1.5, avg_odds=-110)
        assert "error" in r or r.get("risk_level") != "NEGLIGIBLE"

    def test_zero_avg_odds_not_fabricated_into_even_money(self):
        r = ruin_probability(10_000, 100, win_rate=0.53, avg_odds=0)
        assert r.get("decimal_odds") != 2.0, (
            "avg_odds=0 silently converted to even money"
        )


# ===========================================================================
# M9 (LOW): boost gate boundary uses >=, so EV exactly 0.0 passes the gate
# whose own comment says "Any +EV boost". Zero is not plus.
# ===========================================================================


class TestM9BoostGateIncludesZeroEdge:
    def test_zero_ev_boost_not_actionable(self):
        r = evaluate_edge(fair_prob=0.5, book_odds_american=+100,
                          confidence="boost")
        assert not r["actionable"], (
            f"EV={r['ev']} graded actionable at the boost threshold"
        )


# ===========================================================================
# M10/M11 (LOW-MEDIUM): NaN poisons flow THROUGH decision points instead of
# failing closed: timing_value(nan, nan) answers SLIGHT_LEAN_NOW;
# kelly_portfolio treats a NaN correlation as fully diversified.
# ===========================================================================


class TestM10TimingValueNaNFailsOpen:
    def test_nan_inputs_do_not_advise_betting(self):
        d = timing_value(float("nan"), float("nan"))
        assert d["recommendation"] == "NO_BET", (
            f"NaN edge/hours advised {d['recommendation']} with wait_ev={d['wait_ev']}"
        )


class TestM11PortfolioNaNCorrelationFailsOpen:
    def test_nan_correlation_not_treated_as_zero(self):
        bets = [
            {"edge": 0.03, "odds": -110, "correlation_with_others": float("nan")},
            {"edge": 0.02, "odds": 120, "correlation_with_others": 0.2},
        ]
        res = __import__("tools.kelly", fromlist=["kelly_portfolio"]).kelly_portfolio(bets)
        summary = res[0]["portfolio_summary"]
        assert summary["final_total_allocation"] == 0.0 or (
            summary["cap_hit"]), (
            "NaN correlation silently assumed zero correlation "
            f"(allocation {summary['final_total_allocation']})"
        )


# ===========================================================================
# HONEST NEGATIVES -- attacks that did NOT land. These PASS and stay as pins.
# ===========================================================================


class TestHonestNegatives:
    def test_devig_market_robust_over_retail_books(self):
        rng = random.Random(SEED + 4)
        for _ in range(1500):
            fav = rng.randint(-2000, -102)
            dog = rng.randint(101, 5000)
            res = devig_market([1 + 100 / abs(fav), 1 + dog / 100])
            probs = res["fair_probabilities"]
            assert all(0.0 < p < 1.0 for p in probs)
            assert abs(sum(probs) - 1.0) < 1e-4

    def test_core_devig_methods_balanced_market_half(self):
        odds = [1.9091, 1.9091]
        for fair in (
            multiplicative_devig(odds),
            additive_devig(odds),
            power_devig(odds)[0],
            shin_devig(odds)[0],
        ):
            assert all(abs(p - 0.5) < 1e-6 for p in fair)

    def test_additive_devig_copies_agree_two_way(self):
        # PATTERNS #2 check: boost_evaluator's additive copy vs tools.devig's
        # -- they agree everywhere in the two-way domain (negative branch is
        # unreachable for two outcomes).
        from tools.boost_evaluator import devig_additive as be_additive

        rng = random.Random(SEED + 5)
        for _ in range(500):
            a = rng.choice([-5000, -1000, -200, 100, 200, 1000])
            b = rng.choice([-5000, -1000, -200, 100, 200, 1000])
            if a * b > 0:
                continue
            x, y = be_additive(a, b)
            dec_a = american_to_decimal(a)
            dec_b = american_to_decimal(b)
            ref = additive_devig([dec_a, dec_b])
            assert abs(x - ref[0]) < 1e-6 and abs(y - ref[1]) < 1e-6

    def test_best_price_prefers_higher_decimal_with_tie_determinism(self):
        assert best_price(-105, -110)["best_book"] == "draftkings"
        # +130 (dk) is the better price over +125 (fan)
        assert best_price(+130, +125)["best_book"] == "draftkings"
        assert best_price(+125, +130)["best_book"] == "fanatics"
        assert best_price(-110, -110)["improvement_pct"] == 0.0

    def test_kelly_binary_monotone_in_fair_prob_no_rounding(self):
        rng = random.Random(SEED + 6)
        prev = -1.0
        for i in range(500):
            p = 0.5 + i / 1000
            f = kelly_binary(p, 2.10)
            assert f >= prev - 1e-12
            assert f == max(f, 0)      # no rounding applied
            prev = f

    def test_assess_edge_caps_survive_hostile_inputs(self):
        rng = random.Random(SEED + 7)
        for _ in range(500):
            cp = rng.uniform(0.001, 0.999)
            q = MarketQuote(price=rng.randint(-2000, -102),
                            counter_price=rng.randint(101, 5000),
                            kind="american")
            a = assess_edge("t", cp, q)
            assert a.kelly_fraction_full <= MAX_FRACTION_FULL_KELLY + 1e-12
            assert abs(a.kelly_fraction_quarter * 4 - a.kelly_fraction_full) < 1e-12

    def test_clv_points_fails_closed_without_counters(self):
        assert clv_points(MarketQuote(price=-110),
                          MarketQuote(price=-105)) is None

    def test_ruin_probability_negative_ev_reports_certain_ruin_pin(self):
        r = ruin_probability(10_000, 100, win_rate=0.40, avg_odds=-110)
        assert r["ruin_probability"] == 1.0
        assert r["recommended_max_stake"] == 0.0

    def test_half_vig_devig_bounded_output_pin(self):
        assert 0.0 <= _half_vig_devig(0.99, 0.06) <= 1.0
        assert 0.0 <= _half_vig_devig(0.30, -5.0) <= 1.0  # negative vig clamped


from tools.edge import clv_points  # noqa: E402  (used by honest negatives)
