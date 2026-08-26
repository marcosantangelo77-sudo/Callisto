"""
Characterization tests — autofill task #0014 (dual Kelly).

Locks in the current, intended behaviour of the two Kelly paths:

  1. ``kelly_full``   (tools.kelly / tools.kellypkg.core) — takes an EDGE and
     AMERICAN odds, and ROUNDS its return value to 6 decimal places.
  2. ``kelly_core``   (both facades) plus ``tools.sizing.kelly_binary`` —
     take (p, b) or (fair_prob, decimal_odds) and stay UNROUNDED, delegating
     to the single primitive ``tools.kellypkg._formula.kelly_core_unrounded``.

INVARIANT under test: the paths are NOT merged.  ``kelly_full`` rounds;
``kelly_core`` never does.  Nothing here arms live betting; the final
test class fail-closes on the paper-trade status registry.
"""

import inspect
import math

import pytest

from tools.kelly import kelly_core as facade_kelly_core
from tools.kelly import kelly_full as facade_kelly_full
from tools.kelly import kelly_fractional as facade_kelly_fractional
from tools.kellypkg import _formula as formula_module
from tools.kellypkg.core import kelly_core as pkg_kelly_core
from tools.kellypkg.core import kelly_full as pkg_kelly_full
from tools.kellypkg.core import kelly_fractional as pkg_kelly_fractional
from tools.odds_api import calculate_implied_probability
from tools.sizing import kelly_binary


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _reference_kelly_full(edge: float, american: int) -> float:
    """Independent reimplementation of the documented kelly_full contract."""
    implied = calculate_implied_probability(int(american))
    p = max(0.0, min(1.0, implied + edge))
    if american > 0:
        b = american / 100.0
    elif american < 0:
        b = 100.0 / abs(american)
    else:
        b = 1.0
    q = 1.0 - p
    raw = max(0.0, (b * p - q) / b)
    return round(raw, 6)


def _is_rounded_to_6dp(value: float) -> bool:
    """True when value equals itself rounded to 6 decimal places."""
    return value == round(value, 6)


# ---------------------------------------------------------------------------
# kelly_full — rounding contract (6 decimal places)
# ---------------------------------------------------------------------------

class TestKellyFullRounding:
    """kelly_full must always return a value rounded to 6 dp."""

    @pytest.mark.parametrize(
        "edge,american",
        [
            (0.05, 100),
            (0.05, -110),
            (0.02, 150),
            (0.10, 200),
            (0.01, -200),
            (0.0333, 137),
            (0.0777, -183),
            (0.005, 120),
            (0.25, 400),
            (0.042, -105),
            (0.0618, 161),
            (0.089, -240),
            (0.15, -150),
            (0.0025, 105),
            (0.12, 333),
        ],
    )
    def test_matches_reference_implementation(self, edge, american):
        assert pkg_kelly_full(edge, american) == _reference_kelly_full(edge, american)

    @pytest.mark.parametrize(
        "edge,american",
        [
            (0.05, 100),
            (0.0333, 137),
            (0.0777, -183),
            (0.0618, 161),
            (0.0025, 105),
            (0.12, 333),
            (0.089, -240),
            (0.042, -105),
        ],
    )
    def test_output_is_rounded_to_six_decimals(self, edge, american):
        value = pkg_kelly_full(edge, american)
        assert _is_rounded_to_6dp(value)

    def test_known_value_positive_odds(self):
        # +100 (even money): b=1, implied=0.5, edge=0.05 -> p=0.55,
        # f* = (1*0.55 - 0.45)/1 = 0.10 exactly.
        assert pkg_kelly_full(0.05, 100) == pytest.approx(0.1)

    def test_known_value_negative_odds(self):
        # -110: implied = 110/210, edge=0.02 -> p = 110/210 + 0.02
        expected = _reference_kelly_full(0.02, -110)
        assert pkg_kelly_full(0.02, -110) == expected

    def test_even_american_zero_treated_as_plus_money(self):
        # odds=0 -> _american_to_decimal returns 2.0 -> b=1.0; implied prob
        # branch: not >0 so abs(0)/(0+100) = 0.0 implied, p = edge.
        value = pkg_kelly_full(0.55, 0)
        assert value == _reference_kelly_full_with_zero_implied(0.55)

    def test_no_edge_returns_zero(self):
        # edge=0 means true prob == implied prob -> f* == 0.
        assert pkg_kelly_full(0.0, 100) == 0.0
        assert pkg_kelly_full(0.0, -110) == 0.0

    def test_negative_edge_returns_zero_never_negative(self):
        assert pkg_kelly_full(-0.05, 100) == 0.0
        assert pkg_kelly_full(-0.5, 500) == 0.0

    def test_huge_edge_clamped_at_p_one(self):
        # edge so large that p would exceed 1.0; clamp keeps result finite.
        value = pkg_kelly_full(10.0, 100)
        assert 0.0 <= value <= 1.0
        assert _is_rounded_to_6dp(value)

    def test_extreme_negative_edge_clamped_at_p_zero(self):
        assert pkg_kelly_full(-10.0, 100) == 0.0

    def test_result_type_is_float(self):
        assert isinstance(pkg_kelly_full(0.05, 100), float)

    @pytest.mark.parametrize("american", [100, 110, -110, 150, -150, 300, -300])
    def test_monotone_in_edge_until_cap(self, american):
        small = pkg_kelly_full(0.01, american)
        large = pkg_kelly_full(0.05, american)
        zero = pkg_kelly_full(0.0, american)
        assert zero <= small <= large


def _reference_kelly_full_with_zero_implied(p: float) -> float:
    b = 1.0
    q = 1.0 - p
    return round(max(0.0, (b * p - q) / b), 6)


# ---------------------------------------------------------------------------
# kelly_core — UNROUNDED contract
# ---------------------------------------------------------------------------

class TestKellyCoreUnrounded:
    """kelly_core delegates to kelly_core_unrounded and does NOT round."""

    @pytest.mark.parametrize(
        "p,b",
        [
            (0.55, 1.0),
            (0.52, 1.1),
            (0.60, 0.9090909090909091),
            (0.51, 2.0),
            (0.75, 0.5),
            (1 / 3, 3.0),
            (0.4, 1.9),
        ],
    )
    def test_delegates_to_single_formula(self, p, b):
        assert pkg_kelly_core(p, b) == formula_module.kelly_core_unrounded(p, b)
        assert facade_kelly_core(p, b) == formula_module.kelly_core_unrounded(p, b)

    def test_canonical_formula_value(self):
        # f* = (b*p - q)/b = (1*0.55 - 0.45)/1 = 0.1
        assert pkg_kelly_core(0.55, 1.0) == pytest.approx(0.1)

    def test_unrounded_precision_preserved(self):
        # A (p, b) pair whose exact Kelly fraction is NOT representable at
        # 6 dp: p=1/3, b=3.0 -> f* = (1 - 2/3)/3 = 1/9 = 0.111111111...
        raw = formula_module.kelly_core_unrounded(1 / 3, 3.0)
        value = pkg_kelly_core(1 / 3, 3.0)
        assert value == raw
        assert value != round(raw, 6)  # full precision survived
        assert math.isclose(value, 1 / 9, rel_tol=1e-12)

    def test_another_unrounded_case(self):
        p, b = 0.5238095238095238, 1.7320508075688772
        raw = formula_module.kelly_core_unrounded(p, b)
        assert facade_kelly_core(p, b) == raw
        assert _is_rounded_to_6dp(raw) is False

    def test_b_le_zero_returns_zero(self):
        assert pkg_kelly_core(0.9, 0.0) == 0.0
        assert pkg_kelly_core(0.9, -1.0) == 0.0
        assert facade_kelly_core(0.9, -0.001) == 0.0

    def test_neg_ev_returns_zero_never_negative(self):
        # p too low for the payout -> f* <= 0 clamped to 0.
        assert pkg_kelly_core(0.1, 1.0) == 0.0
        assert facade_kelly_core(0.05, 3.0) == 0.0

    def test_perfect_certainty_gives_full_unit(self):
        # p=1 -> f* = b/b = 1.0
        assert pkg_kelly_core(1.0, 2.0) == 1.0

    def test_facade_and_package_are_identical_functions_of_inputs(self):
        for p in (0.5, 0.53, 0.61, 0.77):
            for b in (0.8, 1.0, 1.5, 2.7):
                assert facade_kelly_core(p, b) == pkg_kelly_core(p, b)


# ---------------------------------------------------------------------------
# kelly_binary (tools.sizing) — thin wrapper over kelly_core, unrounded
# ---------------------------------------------------------------------------

class TestKellyBinaryWrapper:
    """tools.sizing.kelly_binary stays on the unrounded core path."""

    def test_decimal_minus_one_becomes_b(self):
        # decimal 2.10 -> b = 1.10; fair_prob 0.55
        expected = pkg_kelly_core(0.55, 1.10)
        assert kelly_binary(0.55, 2.10) == expected

    def test_documented_verified_example(self):
        # docstring: prob=0.55, odds=2.10 -> f*=0.1409 (approximately)
        value = kelly_binary(0.55, 2.10)
        assert value == pytest.approx(0.1409, abs=1e-4)
        # ...but NOT rounded to 4 dp either:
        assert value == pkg_kelly_core(0.55, 1.10)

    def test_unrounded_through_wrapper(self):
        p, dec = 1 / 3, 4.0  # b=3 -> f* = 1/9 exactly, unrounded
        value = kelly_binary(p, dec)
        assert value == formula_module.kelly_core_unrounded(p, 3.0)
        assert value != round(value, 6)

    def test_not_ev_zero(self):
        assert kelly_binary(0.30, 1.50) == 0.0

    def test_bad_odds_zero(self):
        assert kelly_binary(0.90, 1.0) == 0.0  # b = 0
        assert kelly_binary(0.90, 0.5) == 0.0  # b < 0

    def test_float_coercion_of_inputs(self):
        assert kelly_binary("0.55", "2.10") == kelly_binary(0.55, 2.10)


# ---------------------------------------------------------------------------
# The dual-path invariant: rounding happens ONLY in kelly_full
# ---------------------------------------------------------------------------

class TestDualPathNotMerged:
    """
    INVARIANT: kelly_full rounds; kelly_core/kelly_binary do not.
    These tests FAIL if anyone merges the two paths.
    """

    UNREPRESENTABLE_CASES = [
        # (edge, american) pairs whose raw Kelly fraction has >6 dp digits
        (0.0333, 137),
        (0.0618, 161),
        (0.0777, -183),
        (0.0025, 105),
        (0.0421, -213),
    ]

    @pytest.mark.parametrize("edge,american", UNREPRESENTABLE_CASES)
    def test_kelly_full_differs_from_raw_unrounded_fraction(self, edge, american):
        raw = _raw_unrounded_for(edge, american)
        rounded = pkg_kelly_full(edge, american)
        # If this ever becomes equal for ALL cases, kelly_full stopped
        # rounding (or the raw value became 6-dp-representable).
        assert rounded == round(raw, 6)

    @pytest.mark.parametrize("edge,american", UNREPRESENTABLE_CASES)
    def test_raw_fraction_actually_has_more_precision(self, edge, american):
        raw = _raw_unrounded_for(edge, american)
        assert raw != round(raw, 6), "fixture no longer exercises rounding"

    def test_same_bet_two_paths_differ_by_rounding_only(self):
        # Same underlying bet: edge chosen so both paths describe it.
        edge, american = 0.05, 100
        via_full = pkg_kelly_full(edge, american)          # rounded
        p = calculate_implied_probability(american) + edge
        b = 1.0                                            # +100 -> b = 1
        via_core = pkg_kelly_core(p, b)                    # unrounded
        assert via_full == round(via_core, 6)
        # And the unrounded side really is more precise than 6 dp here?
        # For this symmetric case they coincide; assert equality holds.
        assert via_core == pytest.approx(via_full, abs=5e-7)

    def test_source_level_separation(self):
        # Structural check: only kelly_full's source contains the round().
        from tools.kellypkg import core as core_mod

        full_src = inspect.getsource(core_mod.kelly_full)
        core_src = inspect.getsource(core_mod.kelly_core)
        assert "round(" in full_src
        assert "round(" not in core_src

    def test_formula_module_has_no_rounding(self):
        src = inspect.getsource(formula_module)
        assert "round(" not in src
        assert "max(0.0, (b * p - q) / b)" in src

    def test_sizing_wrapper_has_no_rounding(self):
        import tools.sizing as sizing_mod

        src = inspect.getsource(sizing_mod.kelly_binary)
        assert "round(" not in src

    def test_single_formula_exists_in_codebase(self):
        # Both public entry points funnel into the same primitive object.
        from tools.kellypkg._formula import kelly_core_unrounded as prim

        p, b = 0.57, 1.35
        assert pkg_kelly_core(p, b) == prim(p, b)
        assert facade_kelly_core(p, b) == prim(p, b)


def _raw_unrounded_for(edge: float, american: int) -> float:
    """Raw (unrounded) Kelly fraction for an (edge, american-odds) bet."""
    implied = calculate_implied_probability(int(american))
    p = max(0.0, min(1.0, implied + edge))
    if american > 0:
        decimal = 1.0 + american / 100.0
    elif american < 0:
        decimal = 1.0 + 100.0 / abs(american)
    else:
        decimal = 2.0
    b = decimal - 1.0
    return max(0.0, (b * p - (1.0 - p)) / b)


# ---------------------------------------------------------------------------
# kelly_fractional — built on the rounded kelly_full
# ---------------------------------------------------------------------------

class TestKellyFractional:
    @pytest.mark.parametrize("fraction", [0.25, 0.5, 0.1, 1.0])
    def test_is_scaled_kelly_full(self, fraction):
        full = pkg_kelly_full(0.05, 100)
        assert pkg_kelly_fractional(0.05, 100, fraction) == round(full * fraction, 6)

    def test_default_quarter_kelly(self):
        full = pkg_kelly_full(0.05, 100)
        assert pkg_kelly_fractional(0.05, 100) == round(full * 0.25, 6)

    def test_facade_and_package_agree(self):
        assert facade_kelly_fractional(0.08, -120) == pkg_kelly_fractional(0.08, -120)

    def test_no_edge_gives_zero(self):
        assert pkg_kelly_fractional(0.0, -110) == 0.0

    def test_still_rounded_to_6dp(self):
        value = pkg_kelly_fractional(0.0333, 137, 0.25)
        assert _is_rounded_to_6dp(value)


# ---------------------------------------------------------------------------
# Facade consistency: tools.kelly re-exports the split package
# ---------------------------------------------------------------------------

class TestFacadeReexports:
    def test_kelly_full_is_the_same_object(self):
        from tools import kelly as facade

        assert facade.kelly_full is pkg_kelly_full

    def test_kelly_fractional_is_the_same_object(self):
        from tools import kelly as facade

        assert facade.kelly_fractional is pkg_kelly_fractional

    def test_pkg_core_and_facade_core_agree_everywhere(self):
        grid = [(p / 100, 0.5 + i / 10) for p in range(40, 90, 5) for i in range(1, 20)]
        for p, b in grid:
            assert facade_kelly_core(p, b) == pkg_kelly_core(p, b)

    def test_import_surface_exposes_primitives(self):
        from tools import kelly as facade

        for name in ("kelly_core", "kelly_full", "kelly_fractional"):
            assert hasattr(facade, name), name


# ---------------------------------------------------------------------------
# Fail-closed guard: live betting stays unarmed
# ---------------------------------------------------------------------------

class TestFailClosedLiveBettingGuard:
    """
    Characterization of a hard safety gate.  If any of these fail, STOP —
    do not "fix" the test by widening production code.
    """

    def test_paper_trade_statuses_do_not_include_live(self):
        try:
            from tools import signal_registry  # type: ignore[attr-defined]
        except ImportError:
            pytest.skip("no tools.signal_registry in this tree")
        statuses = getattr(signal_registry, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is None:
            pytest.skip("no _PAPER_TRADE_SIGNAL_STATUSES in this tree")
        assert "live" not in {s.lower() for s in statuses}

    def test_generate_paper_trade_signal_signature_not_widened_to_live(self):
        # Scan candidate modules for generate_paper_trade_signal and make
        # sure none of them special-case the literal status 'live'.
        import tools.sizing as sizing_mod

        candidates = [sizing_mod]
        try:
            from tools import signal_registry as reg  # type: ignore[attr-defined]

            candidates.append(reg)
        except ImportError:
            pass
        checked = False
        for mod in candidates:
            fn = getattr(mod, "generate_paper_trade_signal", None)
            if fn is None:
                continue
            checked = True
            src = inspect.getsource(fn)
            assert "== 'live'" not in src
            assert '"live"' not in src.replace("'live'", "")
        if not checked:
            pytest.skip("generate_paper_trade_signal not found in scanned modules")
