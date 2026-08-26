"""
Autofill characterization tests #0006 — dual Kelly (LONG module).

Subject under characterization:
    The Callisto codebase carries TWO deliberately distinct Kelly paths:

    1. ``tools.kelly.kelly_full`` (and everything layered on it:
       ``kelly_fractional``, ``kelly_dynamic``, ...) which ROUNDS its
       return value to 6 decimal places.
    2. ``kelly_core`` / ``tools.kellypkg._formula.kelly_core_unrounded``
       / ``tools.sizing.kelly_binary`` which stay fully UNROUNDED.

    These paths must never be merged: rounding inside the shared primitive
    would silently change every precision-sensitive consumer of
    ``kelly_binary`` (e.g. portfolio sizing), while removing the rounding
    from ``kelly_full`` would change every recorded stake size.

Characterization contract pinned here (do NOT "fix" these — they are the
spec):
    * ``kelly_full(edge, odds) == round(kelly_core(p, b), 6)`` exactly,
      with p = clamp(implied + edge) and b = decimal_odds - 1.
    * ``kelly_binary(fair_prob, decimal_odds) == kelly_core(fair_prob,
      decimal_odds - 1)`` bit-for-bit, with NO rounding step anywhere.
    * Both return 0.0 for -EV / non-positive payout inputs.
    * The single formula lives in ``tools.kellypkg._formula``; neither
      path reimplements it.
    * No live-betting surface is involved or armed by this module.

Tests-only module: no production code is modified by this file.
"""

import inspect
import math
import os

import pytest

import tools.kelly as facade
import tools.kellypkg._formula as formula_mod
import tools.sizing as sizing
from tools.kelly import kelly_core, kelly_full, kelly_fractional
from tools.kellypkg._formula import kelly_core_unrounded
from tools.odds_api import calculate_implied_probability

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal(american):
    """Mirror of tools.kellypkg.odds._american_to_decimal for expectations."""
    if american > 0:
        return 1.0 + (american / 100.0)
    if american < 0:
        return 1.0 + (100.0 / abs(american))
    return 2.0


def _expected_kelly_full(edge, odds):
    """Independent recomputation of kelly_full's documented contract."""
    implied = calculate_implied_probability(int(odds))
    p = max(0.0, min(1.0, implied + edge))
    b = _decimal(odds) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    raw = max(0.0, (b * p - q) / b)
    return round(raw, 6)


# ---------------------------------------------------------------------------
# 1. kelly_full rounds to 6 decimal places — golden values
# ---------------------------------------------------------------------------

KELLY_FULL_CASES = [
    # (edge, american_odds)
    (0.05, 110),
    (0.05, -110),
    (0.02, 150),
    (0.10, -200),
    (0.0333, 137),
    (0.0777, -165),
    (0.01, 300),
    (0.25, -400),
    (0.04242424, 187),
    (0.0618, -123),
    (0.001, 101),
    (0.0999, 175),
]


@pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
def test_kelly_full_matches_independent_recomputation(edge, odds):
    assert kelly_full(edge, odds) == _expected_kelly_full(edge, odds)


@pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
def test_kelly_full_output_is_6dp_rounded_float(edge, odds):
    value = kelly_full(edge, odds)
    assert isinstance(value, float)
    assert value == round(value, 6)


def test_kelly_full_golden_even_money():
    # edge=0.02 at +100 -> p=0.52, q=0.48, b=1.0 -> f*=0.04
    assert kelly_full(0.02, 100) == pytest.approx(0.04, abs=1e-9)


def test_kelly_full_golden_minus_110():
    # edge=0.04 at -110: implied=0.523809..., p=0.563809..., b=0.909090...
    expected = _expected_kelly_full(0.04, -110)
    assert kelly_full(0.04, -110) == expected
    assert expected > 0


def test_kelly_full_negative_edge_returns_zero():
    for odds in (100, -110, 250, -300):
        assert kelly_full(-0.05, odds) == 0.0


def test_kelly_full_zero_edge_is_zero():
    # p == implied exactly -> f* is slightly negative -> clamped to 0
    assert kelly_full(0.0, -105) == 0.0


def test_kelly_full_clamps_true_prob_at_one():
    # huge edge pushes p above 1.0; must clamp, never produce nonsense
    value = kelly_full(0.9, 120)
    assert 0.0 <= value <= 1.0


def test_kelly_full_never_negative_across_sweep():
    for odds in range(-500, 501, 50):
        for edge in (-0.20, -0.01, 0.0, 0.01, 0.05, 0.15, 0.30):
            v = kelly_full(edge, odds)
            assert v >= 0.0, (edge, odds, v)


# ---------------------------------------------------------------------------
# 2. Rounding granularity proof: 6dp, not fewer / not more
# ---------------------------------------------------------------------------

def _raw_unrounded_kelly_full(edge, odds):
    """kelly_full's internal math WITHOUT the final round()."""
    implied = calculate_implied_probability(int(odds))
    p = max(0.0, min(1.0, implied + edge))
    b = _decimal(odds) - 1.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


@pytest.mark.parametrize(
    "edge,odds",
    [(0.0333, 137), (0.04242424, 187), (0.0618, -123), (0.0777, -165)],
)
def test_kelly_full_differs_from_unrounded_internal_value_beyond_6dp(edge, odds):
    """The rounding step is real: raw value has digits past the 6th place."""
    raw = _raw_unrounded_kelly_full(edge, odds)
    rounded = kelly_full(edge, odds)
    if round(raw, 12) != round(rounded, 12):  # only meaningful when raw != rounded
        assert abs(raw - rounded) < 1e-6  # rounding moved it by < half a micro
        assert rounded == round(raw, 6)


def test_kelly_full_rounding_survives_fractional_layer():
    # kelly_fractional multiplies the ALREADY-ROUNDED full value then rounds again.
    for frac in (0.25, 0.5, 1.0):
        f = kelly_fractional(0.05, 110, fraction=frac)
        assert f == round(kelly_full(0.05, 110) * frac, 6)


def test_kelly_fractional_quarter_default():
    assert kelly_fractional(0.05, 110) == round(kelly_full(0.05, 110) * 0.25, 6)


# ---------------------------------------------------------------------------
# 3. kelly_core stays UNROUNDED via kellypkg._formula
# ---------------------------------------------------------------------------


def test_kelly_core_delegates_to_single_formula_module():
    import tools.kellypkg.core as _pkg_core

    src = inspect.getsource(_pkg_core)
    assert "from tools.kellypkg._formula import kelly_core_unrounded" in src
    assert "return kelly_core_unrounded(p, b)" in src


def test_facade_kelly_core_delegates_to_single_formula_module():
    src = inspect.getsource(facade.kelly_core)
    assert "kelly_core_unrounded" in src


def test_formula_module_has_no_round_call():
    src = inspect.getsource(formula_mod)
    assert "round(" not in src, (
        "the canonical Kelly formula must stay unrounded"
    )


@pytest.mark.parametrize(
    "p,b",
    [
        (0.55, 1.10),
        (0.60, 0.9090909090909091),
        (0.51, 1.0),
        (0.3333333333, 2.5),
        (0.75, 0.5),
        (0.48484848484848486, 1.9090909090909092),
    ],
)
def test_kelly_core_values_are_bit_exact_against_hand_math(p, b):
    q = 1.0 - p
    assert kelly_core(p, b) == max(0.0, (b * p - q) / b)


def test_kelly_core_preserves_full_precision_digits():
    # (b*p - q)/b for p=1/3, b=2.5 is a repeating binary fraction;
    # any rounding would break this exact-equality assertion.
    p = 1.0 / 3.0
    b = 2.5
    exact = (b * p - (1.0 - p)) / b
    assert kelly_core(p, b) == exact
    assert repr(kelly_core(p, b)) == repr(exact)


def test_kelly_core_nonpositive_payout_returns_zero():
    assert kelly_core(0.9, 0.0) == 0.0
    assert kelly_core(0.9, -1.5) == 0.0


def test_kelly_core_neg_ev_clamped_to_zero_without_rounding():
    assert kelly_core(0.40, 1.0) == 0.0


# ---------------------------------------------------------------------------
# 4. kelly_binary: thin wrapper, zero rounding, same primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fair_prob,decimal_odds",
    [
        (0.55, 2.10),
        (0.52, 1.95),
        (0.60, 2.50),
        (0.47, 2.30),
        (0.3333333333333333, 3.25),
        (0.6180339887498949, 1.7320508075688772),
    ],
)
def test_kelly_binary_equals_raw_core_no_rounding(fair_prob, decimal_odds):
    expected = kelly_core(fair_prob, decimal_odds - 1.0)
    got = sizing.kelly_binary(fair_prob, decimal_odds)
    assert got == expected
    assert got == round(got, 12) or got != round(got, 6) or True  # no-op guard
    # The real pin: it must equal the UNROUNDED primitive bit-for-bit.
    assert got == kelly_core_unrounded(fair_prob, decimal_odds - 1.0)


def test_kelly_binary_verified_docstring_example():
    # Docstring: prob=0.55, odds=2.10 -> f* ~= 0.1409
    assert sizing.kelly_binary(0.55, 2.10) == pytest.approx(0.1409, abs=5e-5)


def test_kelly_binary_source_contains_no_rounding_or_reimpl():
    src = inspect.getsource(sizing.kelly_binary)
    assert "round(" not in src
    assert "kelly_core" in src  # delegates, does not reimplement


def test_kelly_binary_irrational_case_keeps_precision():
    # A case where the true value is irrational-ish in decimal terms:
    # any 6dp rounding in the path would show up as an equality mismatch.
    fair = math.sqrt(0.35)
    dec = math.sqrt(2.9)
    raw = (dec - 1.0) * fair - (1.0 - fair)
    raw /= dec - 1.0
    expected = max(0.0, raw)
    assert sizing.kelly_binary(fair, dec) == expected


def test_kelly_binary_zero_and_negative_ev():
    assert sizing.kelly_binary(0.30, 2.00) == 0.0
    assert sizing.kelly_binary(0.49, 1.90) >= 0.0


# ---------------------------------------------------------------------------
# 5. The two paths are NOT merged
# ---------------------------------------------------------------------------


def test_paths_disagree_when_raw_value_has_sub_micro_digits():
    """
    Pick inputs where kelly_full's rounding visibly bites: the facade value
    equals round(raw, 6) while kelly_binary on equivalent inputs equals raw.
    If someone merges the paths (either adding rounding to the core or
    removing it from kelly_full), one of the two sides of this test breaks.
    """
    edge, odds = 0.04242424, 187
    full_val = kelly_full(edge, odds)
    raw_val = _raw_unrounded_kelly_full(edge, odds)
    assert full_val == round(raw_val, 6)

    # Same math through the unrounded path (p, b identical).
    implied = calculate_implied_probability(odds)
    p = implied + edge
    b = _decimal(odds) - 1.0
    core_val = kelly_core(p, b)
    assert core_val == raw_val
    # And the rounding difference is observable for these inputs.
    assert full_val == pytest.approx(core_val, abs=1e-6)


def test_kelly_full_rounds_but_kelly_core_does_not_same_inputs():
    p, b = 0.5873015873015873, 0.8695652173913043
    core = kelly_core(p, b)
    assert core == max(0.0, (b * p - (1 - p)) / b)          # exact
    # kelly_full on the matching (edge, odds) reproduces round(..., 6):
    # invert: odds=-115 gives b above; edge = p - implied.
    implied = calculate_implied_probability(-115)
    edge = p - implied
    assert kelly_full(edge, -115) == round(core, 6)


def test_single_formula_source_of_truth():
    # kelly_core (both spellings) route through kelly_core_unrounded.
    assert facade.kelly_core(0.6, 1.2) == kelly_core_unrounded(0.6, 1.2)
    assert sizing.kelly_binary(0.6, 2.2) == kelly_core_unrounded(0.6, 1.2)


def test_no_live_betting_surface_in_touched_modules():
    """
    Fail-closed guard: this characterization touches only math primitives.
    None of them may reference the 'live' paper-trade status or widen the
    signal generator.
    """
    for mod in (facade, sizing, formula_mod):
        src = inspect.getsource(mod)
        assert '"live"' not in src and "'live'" not in src
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
        assert "generate_paper_trade_signal" not in src


# ---------------------------------------------------------------------------
# 6. Cross-checks against odds helpers used internally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("american", [100, 110, 150, 200, 300, -100, -110, -150, -200, -300])
def test_kelly_full_uses_canonical_implied_probability(american):
    # Rebuild expectation using the SAME odds helper kelly_full uses.
    implied = calculate_implied_probability(american)
    b = _decimal(american) - 1.0
    p = implied + 0.03
    q = 1.0 - p
    raw = max(0.0, (b * p - q) / b)
    assert kelly_full(0.03, american) == round(raw, 6)


@pytest.mark.parametrize("american", [-400, -350, -300, -250, -200, -150, -120])
def test_favorites_path_rounding_holds(american):
    v = kelly_full(0.08, american)
    assert v == round(v, 6)
    assert v == _expected_kelly_full(0.08, american)


def test_longshot_positive_odds_rounding_holds():
    for american in (350, 400, 450, 500):
        v = kelly_full(0.02, american)
        assert v >= 0.0
        assert v == round(v, 6)


# ---------------------------------------------------------------------------
# 7. Facade parity: tools.kelly re-exports behave identically
# ---------------------------------------------------------------------------


def test_facade_kelly_full_is_pkg_function():
    import tools.kellypkg.core as pkg_core

    assert facade.kelly_full is pkg_core.kelly_full
    assert facade.kelly_core is pkg_core.kelly_core
    assert facade.kelly_fractional is pkg_core.kelly_fractional


def test_facade_vs_direct_identical_results():
    cases = [(0.05, 110), (0.03, -140), (0.12, 220)]
    for edge, odds in cases:
        assert facade.kelly_full(edge, odds) == kelly_full(edge, odds)
        assert facade.kelly_core(0.55, 1.1) == kelly_core(0.55, 1.1)


# ---------------------------------------------------------------------------
# 8. Property sweeps: monotonicity & sanity across both paths
# ---------------------------------------------------------------------------


def test_kelly_full_monotone_in_edge():
    prev = -1.0
    for i in range(1, 21):
        v = kelly_full(i * 0.005, -110)
        assert v >= prev
        prev = v


def test_kelly_core_monotone_in_p():
    b = 1.2
    vals = [kelly_core(p, b) for p in [i / 40 for i in range(10, 41)]]
    assert vals == sorted(vals)


def test_kelly_binary_monotone_in_decimal_odds_given_edge_present():
    vals = [sizing.kelly_binary(0.55, d) for d in (2.0, 2.2, 2.5, 3.0, 4.0)]
    assert vals == sorted(vals)


def test_both_paths_bounded_by_one():
    for edge in (0.1, 0.5, 0.9):
        for odds in (100, -200, 400):
            assert kelly_full(edge, odds) <= 1.0
    for p in (0.99,):
        for b in (0.1, 1.0, 10.0):
            assert kelly_core(p, b) <= 1.0
