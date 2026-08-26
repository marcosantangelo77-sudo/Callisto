"""
Autofill characterization #0086 — dual Kelly (LONG).

Characterizes the two distinct Kelly paths that must remain UNMERGED:

  PATH A (rounded):
      tools.kellypkg.core.kelly_full(edge, american_odds)
        -> round(kelly_core(p, b), 6)
      Everything built on it (kelly_fractional, bet_size's kelly_full field
      in tools.sizing) inherits the rounding.

  PATH B (unrounded):
      tools.kellypkg.core.kelly_core(p, b)
        -> kelly_core_unrounded(p, b), full float precision
      tools.sizing.kelly_binary delegates to it via
      ``kelly_core(float(fair_prob), float(decimal_odds) - 1.0)``
      and must stay unrounded.

Contract under test:
1. kelly_full output is always a multiple of 1e-6 (i.e., round(x, 6) == x).
2. kelly_binary / kelly_core preserve full precision — for inputs whose
   exact fraction needs more than 6 decimal places the returned value
   differs from its own 6-decimal rounding.
3. The paths do NOT merge: kelly_binary is not routed through kelly_full,
   and kelly_full does not expose an unrounded surface.
4. No live-betting surface is introduced or armed by these helpers.

These are characterization tests: they pin CURRENT behavior so refactors
that silently merge the rounded and unrounded paths fail loudly.
"""

import math

import pytest

import tools.kelly as facade
import tools.kellypkg.core as core
from tools.kellypkg.odds import _american_to_decimal
from tools.odds_api import calculate_implied_probability
from tools.sizing import kelly_binary, bet_size, NOISE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_multiple_of_1e6(x: float) -> bool:
    """True when x equals itself rounded to 6 decimals (bit-exact)."""
    return round(x, 6) == x


def _exact_kelly_fraction(p: float, b: float) -> float:
    """Reference implementation of f* = (b*p - q)/b, clamped at 0."""
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(f, 0.0)


# ---------------------------------------------------------------------------
# Path A: kelly_full rounds to 6 decimal places
# ---------------------------------------------------------------------------

class TestKellyFullRoundsToSixDecimals:
    """Every value returned by kelly_full must be 6-decimal stable."""

    @pytest.mark.parametrize(
        "edge,odds",
        [
            (0.05, 100),
            (0.03, -110),
            (0.01, 150),
            (0.075, 200),
            (0.02, -150),
            (0.10, 300),
            (-0.05, 100),
            (0.0499, 137),
            (0.0612, -123),
            (0.0033, 411),
            (0.0888, -205),
            (0.001, 999),
            (0.12, -400),
            (0.025, 175),
        ],
    )
    def test_output_equals_own_six_decimal_rounding(self, edge, odds):
        fk = core.kelly_full(edge, odds)
        assert fk == round(fk, 6)

    @pytest.mark.parametrize(
        "edge,odds",
        [
            (0.05, 100),
            (0.03, -110),
            (0.02, -150),
            (0.075, 200),
            (0.0033, 411),
        ],
    )
    def test_matches_reference_formula_then_rounds(self, edge, odds):
        implied = calculate_implied_probability(int(odds))
        p = max(0.0, min(1.0, implied + edge))
        b = _american_to_decimal(int(odds)) - 1.0
        expected = round(_exact_kelly_fraction(p, b), 6)
        assert core.kelly_full(edge, odds) == expected

    def test_negative_edge_clamps_to_zero(self):
        assert core.kelly_full(-0.05, 100) == 0.0
        assert core.kelly_full(0.0, 100) == 0.0

    @pytest.mark.parametrize("edge", [-1e-9, -0.0001, -0.5, -1.0])
    def test_all_negative_edges_return_exact_zero(self, edge):
        assert core.kelly_full(edge, 100) == 0.0
        assert isinstance(core.kelly_full(edge, 100), float)

    def test_probability_clamped_at_one(self):
        # Huge edge pushes p above 1.0; clamp keeps math finite.
        fk = core.kelly_full(0.95, 100)
        assert 0.0 <= fk <= 1.0
        assert fk == round(fk, 6)

    def test_result_is_float_not_decimal(self):
        assert type(core.kelly_full(0.05, 100)) is float


class TestKellyFractionalInheritsRounding:
    """kelly_fractional is built on kelly_full -> also 6-decimal stable."""

    @pytest.mark.parametrize(
        "fraction",
        [0.25, 0.5, 0.1, 1.0],
    )
    def test_fractional_output_is_rounded(self, fraction):
        val = core.kelly_fractional(0.05, 100, fraction=fraction)
        assert val == round(val, 6)

    def test_quarter_kelly_is_quarter_of_full(self):
        full = core.kelly_full(0.05, 100)
        assert core.kelly_fractional(0.05, 100, 0.25) == round(full * 0.25, 6)


# ---------------------------------------------------------------------------
# Path B: kelly_core / kelly_binary stay UNROUNDED
# ---------------------------------------------------------------------------

class TestKellyBinaryUnrounded:
    """tools.sizing.kelly_binary delegates to kelly_core with no rounding."""

    # (fair_prob, decimal_odds) pairs whose exact f* needs > 6 dp.
    UNROUNDED_CASES = [
        (0.55, 2.10),
        (0.52, 1.95),
        (0.60, 2.50),
        (0.53, 1.91),
        (0.57, 2.20),
        (0.58, 2.35),
    ]

    @pytest.mark.parametrize("p,d", UNROUNDED_CASES)
    def test_value_differs_from_six_decimal_rounding_when_precision_demands(
        self, p, d
    ):
        exact = _exact_kelly_fraction(p, d - 1.0)
        got = kelly_binary(p, d)
        # Sanity: the reference says this case genuinely has sub-1e-6 detail.
        assert exact != round(exact, 6)
        # The contract: kelly_binary does NOT collapse to the rounded form.
        assert got == exact
        assert got != round(got, 6)

    @pytest.mark.parametrize("p,d", UNROUNDED_CASES)
    def test_bit_identical_to_kelly_core_delegation(self, p, d):
        assert kelly_binary(p, d) == core.kelly_core(p, d - 1.0)

    @pytest.mark.parametrize("p,d", UNROUNDED_CASES)
    def test_not_routed_through_kelly_full(self, p, d):
        # If someone reroutes kelly_binary through the rounded path this dies.
        assert kelly_binary(p, d) != round(kelly_binary(p, d), 6)

    def test_no_edge_returns_zero(self):
        # p clearly below break-even -> f* <= 0 clamps to exactly 0.0.
        assert kelly_binary(0.30, 2.50) == 0.0
        assert kelly_binary(0.45, 2.10) == 0.0

    def test_breakeven_boundary_yields_tiny_nonnegative_value(self):
        # Characterization: floating-point noise at exact break-even can
        # produce a denormal-scale positive value, never negative.
        v = kelly_binary(0.40, 2.50)
        assert 0.0 <= v < 1e-12

    @pytest.mark.parametrize("d", [1.0, 0.5, -2.0])
    def test_nonpositive_net_payout_returns_zero(self, d):
        assert kelly_binary(0.55, d) == 0.0

    def test_string_inputs_coerced_like_the_wrapper_does(self):
        # kelly_binary wraps args in float(); verify coercion behavior holds.
        assert kelly_binary("0.55", "2.10") == kelly_binary(0.55, 2.10)


class TestKellyCoreUnroundedPrimitive:
    """kelly_core itself carries no rounding anywhere."""

    @pytest.mark.parametrize(
        "p,b",
        [(0.55, 1.10), (0.52, 0.95), (0.61, 1.44), (0.49, 1.30)],
    )
    def test_equals_closed_form_exactly(self, p, b):
        exact = _exact_kelly_fraction(p, b)
        assert core.kelly_core(p, b) == pytest.approx(exact, rel=0, abs=0)
        # bit-level equality against plain Python arithmetic
        assert core.kelly_core(p, b) == max((b * p - (1 - p)) / b, 0.0)

    def test_sub_micro_detail_survives(self):
        p, b = 0.5501, 1.1003
        exact = (b * p - (1 - p)) / b
        assert core.kelly_core(p, b) == exact
        assert core.kelly_core(p, b) != round(exact, 6)

    def test_negative_b_returns_zero(self):
        assert core.kelly_core(0.9, -1.0) == 0.0
        assert core.kelly_core(0.9, 0.0) == 0.0

    def test_return_type_is_plain_float(self):
        assert type(core.kelly_core(0.55, 1.10)) is float


# ---------------------------------------------------------------------------
# The paths do not merge
# ---------------------------------------------------------------------------

class TestPathsRemainDistinct:
    """
    kelly_full and kelly_binary/kelly_core are separate code paths:
    different inputs (edge+american vs p+decimal), different rounding.
    """

    def test_same_mathematical_bet_gives_different_bit_values(self):
        # Bet: p=0.55, decimal 2.10 == american +110, edge chosen so that
        # kelly_full reconstructs the same true probability.
        p, dec, am = 0.55, 2.10, 110
        implied = calculate_implied_probability(am)
        edge = p - implied
        rounded_val = core.kelly_full(edge, am)
        unrounded_val = kelly_binary(p, dec)
        exact = _exact_kelly_fraction(p, dec - 1.0)
        assert rounded_val == round(exact, 6)
        assert unrounded_val == exact
        assert unrounded_val != rounded_val

    @pytest.mark.parametrize(
        "am,p",
        [(110, 0.55), (150, 0.45), (-110, 0.56), (200, 0.37)],
    )
    def test_reconstruction_pairing_holds_both_ways(self, am, p):
        implied = calculate_implied_probability(am)
        edge = p - implied
        dec = _american_to_decimal(am)
        assert core.kelly_full(edge, am) == round(_exact_kelly_fraction(p, dec - 1.0), 6)
        assert kelly_binary(p, dec) == _exact_kelly_fraction(p, dec - 1.0)

    def test_kelly_binary_source_has_no_round_call(self):
        import ast
        import inspect

        for fn in (core.kelly_core, kelly_binary):
            src = inspect.getsource(fn)
            tree = ast.parse(src)
            calls = [
                n.func.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            ]
            assert "round" not in calls, f"{fn.__name__} must not call round()"

    def test_kelly_full_source_contains_explicit_rounding(self):
        import inspect
        src = inspect.getsource(core.kelly_full)
        assert "round(" in src and ", 6)" in src

    def test_facade_exports_both_paths(self):
        assert callable(facade.kelly_full)
        assert callable(facade.kelly_core)
        assert facade.kelly_full is core.kelly_full
        assert facade.kelly_core is core.kelly_core

    def test_paths_agree_after_normalizing_precision(self):
        # They describe the same formula even though precision differs.
        for am, p in [(120, 0.50), (-105, 0.54), (185, 0.38)]:
            implied = calculate_implied_probability(am)
            edge = p - implied
            dec = _american_to_decimal(am)
            assert core.kelly_full(edge, am) == pytest.approx(
                kelly_binary(p, dec), abs=1e-6
            )


# ---------------------------------------------------------------------------
# bet_size surfaces (downstream of both paths)
# ---------------------------------------------------------------------------

class TestBetSizeSurfaces:
    """bet_size reports rounded fields while using the unrounded path."""

    def test_kelly_fields_are_four_decimals(self):
        res = bet_size(1000.0, 0.55, 2.10, "high")
        assert res["kelly_full"] == round(res["kelly_full"], 4)
        assert res["kelly_quarter"] == round(res["kelly_quarter"], 4)
        assert res["kelly_adjusted"] == round(res["kelly_adjusted"], 4)

    def test_kelly_quarter_is_quarter_of_reported_full(self):
        res = bet_size(1000.0, 0.55, 2.10, "high")
        assert res["kelly_quarter"] == round(round(res["kelly_full"] * 0.25, 4), 4)

    def test_stake_two_decimals(self):
        res = bet_size(1000.0, 0.55, 2.10, "high")
        assert res["recommended_stake"] == round(res["recommended_stake"], 2)

    def test_no_push_routes_through_unrounded_binary_path(self):
        # For a case where the raw binary fraction has >6dp detail, the
        # reported kelly_full (rounded to 4) must still reflect real math.
        res = bet_size(5000.0, 0.53, 1.91, "medium")
        exact = _exact_kelly_fraction(0.53, 0.91)
        assert exact != round(exact, 6)  # genuinely needs more precision
        assert res["kelly_full"] == round(exact, 4)

    def test_max_wager_cap(self):
        res = bet_size(1_000_000.0, 0.60, 2.50, "high", max_wager=10.0)
        assert res["max_capped"] is True
        assert res["recommended_stake"] == 10.0

    @pytest.mark.parametrize("conf,noise", sorted(NOISE.items()))
    def test_confidence_levels_accepted(self, conf, noise):
        res = bet_size(1000.0, 0.55, 2.10, conf)
        assert res["confidence"] == conf
        assert res["kelly_full"] >= 0.0


# ---------------------------------------------------------------------------
# Safety / fail-closed posture
# ---------------------------------------------------------------------------

class TestNoLiveBettingSurface:
    """The dual-Kelly helpers must not arm live betting."""

    def test_kelly_functions_are_pure_math(self):
        # Pure functions: same inputs -> same outputs, no global state.
        a1, a2 = core.kelly_full(0.05, 100), core.kelly_full(0.05, 100)
        b1, b2 = kelly_binary(0.55, 2.10), kelly_binary(0.55, 2.10)
        assert a1 == a2 and b1 == b2

    def test_outputs_are_fractions_within_unit_interval(self):
        for edge, odds in [(0.05, 100), (0.5, 500), (0.02, -110)]:
            assert 0.0 <= core.kelly_full(edge, odds) <= 1.0
        for p, d in [(0.55, 2.10), (0.9, 3.0)]:
            assert 0.0 <= kelly_binary(p, d) <= 1.0

    def test_never_negative_even_for_extreme_bad_input(self):
        assert core.kelly_full(-10.0, 100) == 0.0
        assert kelly_binary(0.0, 2.0) == 0.0
        assert kelly_binary(-1.0, 2.0) == 0.0

    def test_nan_free_outputs_for_valid_domains(self):
        vals = [core.kelly_full(e, o) for e, o in [(0.01, -200), (0.09, 350)]]
        vals += [kelly_binary(p, d) for p, d in [(0.5, 2.0), (0.62, 2.75)]]
        for v in vals:
            assert not math.isnan(v)
            assert not math.isinf(v)


# ---------------------------------------------------------------------------
# Cross-check against the facade re-exports
# ---------------------------------------------------------------------------

class TestFacadeParity:
    def test_facade_kelly_full_identical_results(self):
        for e, o in [(0.04, -115), (0.06, 160)]:
            assert facade.kelly_full(e, o) == core.kelly_full(e, o)

    def test_facade_kelly_core_identical_results(self):
        for p, b in [(0.55, 1.1), (0.48, 0.8)]:
            assert facade.kelly_core(p, b) == core.kelly_core(p, b)
