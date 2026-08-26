"""
autofill characterization #0054 — dual Kelly (LONG).

Characterizes the two DISTINCT Kelly paths that must never merge:

Path A (ROUNDED):
    tools.kellypkg.core.kelly_full(edge, odds)
      -> implied = calculate_implied_probability(int(odds))
      -> p = clamp(implied + edge, 0, 1)
      -> b = _american_to_decimal(odds) - 1
      -> return round(kelly_core(p, b), 6)          # <-- ROUNDS

    Everything built on kelly_full (kelly_fractional) inherits the rounding.

Path B (UNROUNDED):
    tools.kellypkg.core.kelly_core(p, b)
      -> delegates to tools.kellypkg._formula.kelly_core_unrounded
      -> max(0.0, (b*p - q)/b), 0.0 when b <= 0     # full float precision

    tools.sizing.kelly_binary(fair_prob, decimal_odds)
      -> kelly_core(float(fair_prob), float(decimal_odds) - 1.0)   # NO rounding

These tests pin:
1. Exact numeric outputs of both paths across positive/negative American odds,
   clamping boundaries and degenerate inputs (characterization, not approval).
2. The rounding contract: kelly_full's result is ALWAYS a 6-decimal value;
   there exist inputs where the unrounded core differs from kelly_full —
   proving the paths are distinct and do NOT merge.
3. Identity wiring: kelly_core is literally kelly_core_unrounded, and
   tools.sizing.kelly_binary is a thin delegate on kelly_core (no re-rounding).
4. Fail-closed invariants: no live-betting surface is touched by these
   characterizations; the paper-trade signal status gate stays narrow.

Run with:
    /tmp/callisto-pytest/bin/python -m pytest tests/test_autofill_0054.py -q
"""

import inspect
import math

import pytest

from tools.kelly import (
    _american_to_decimal,
    kelly_core,
    kelly_fractional,
    kelly_full,
)
from tools.kellypkg import _formula as formula_mod
from tools.odds_api import calculate_implied_probability
from tools.sizing import kelly_binary


# ---------------------------------------------------------------------------
# Local reference implementations used ONLY for characterization comparison.
# These mirror (independently re-derived from the docstrings) the production
# math so each test asserts production == reference rather than tautology.
# ---------------------------------------------------------------------------

def ref_implied(american_odds):
    """Reference: implied probability from American odds."""
    odds = int(american_odds)
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def ref_decimal(odds):
    """Reference: American -> decimal odds."""
    odds = int(odds)
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / float(-odds)


def ref_kelly_core_unrounded(p, b):
    """Reference: THE single Kelly formula, no rounding, floored at 0."""
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


def ref_kelly_full(edge, odds):
    """Reference path A including its terminal round(..., 6)."""
    implied = ref_implied(odds)
    p = max(0.0, min(1.0, implied + edge))
    b = ref_decimal(odds) - 1.0
    return round(ref_kelly_core_unrounded(p, b), 6)


def ref_kelly_binary(fair_prob, decimal_odds):
    """Reference path B: unrounded core over decimal odds."""
    return ref_kelly_core_unrounded(float(fair_prob), float(decimal_odds) - 1.0)


def _is_6dp(value):
    """True iff `value` equals itself rounded to 6 decimal places."""
    return value == round(value, 6)


# ---------------------------------------------------------------------------
# Fixtures / shared tables
# ---------------------------------------------------------------------------

POSITIVE_ODDS = [100, 105, 110, 120, 150, 200, 250, 300, 400, 500, 750, 1000]
NEGATIVE_ODDS = [-100, -105, -110, -120, -150, -200, -250, -300, -400, -500, -750, -1000]
ALL_AMERICAN_ODDS = POSITIVE_ODDS + NEGATIVE_ODDS
EDGES = [0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50, 0.75, 1.0]


# ---------------------------------------------------------------------------
# 1. Path A: kelly_full exact characterization (ROUNDED to 6 dp)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
@pytest.mark.parametrize("edge", EDGES)
def test_kelly_full_matches_reference_exact(edge, odds):
    assert kelly_full(edge, odds) == pytest.approx(ref_kelly_full(edge, odds))


@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
@pytest.mark.parametrize("edge", EDGES)
def test_kelly_full_output_is_six_decimal_places(edge, odds):
    result = kelly_full(edge, odds)
    assert isinstance(result, float)
    assert _is_6dp(result), f"kelly_full({edge!r}, {odds!r}) -> {result!r} not 6dp"


@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
@pytest.mark.parametrize("edge", EDGES)
def test_kelly_full_never_negative_and_bounded(edge, odds):
    result = kelly_full(edge, odds)
    assert result >= 0.0
    # A Kelly fraction can exceed 1 only for absurd edges; our table stays sane.
    assert result <= 1.0


def test_kelly_full_zero_edge_is_zero_for_all_table_odds():
    for odds in ALL_AMERICAN_ODDS:
        assert kelly_full(0.0, odds) == 0.0, f"zero edge must size zero ({odds})"


def test_kelly_full_negative_edge_is_zero():
    for odds in ALL_AMERICAN_ODDS:
        for edge in (-0.5, -0.01, -1.0):
            assert kelly_full(edge, odds) == 0.0


def test_kelly_full_huge_edge_clamps_p_to_one():
    # edge so big that p would exceed 1 -> clamp to exactly 1.0.
    f = kelly_full(50.0, 100)
    assert f == round(ref_kelly_core_unrounded(1.0, ref_decimal(100) - 1.0), 6)
    assert f == pytest.approx(round(1.0, 6))  # b*p - q = 2*1 - 0 => f = 1.0


def test_kelly_full_extreme_negative_edge_clamps_p_to_zero():
    # edge so negative that p would fall below 0 -> clamp to 0.0 -> f* = 0.
    assert kelly_full(-50.0, -500) == 0.0


@pytest.mark.parametrize(
    "edge,odds,expected",
    [
        # Hand-computed spot checks (characterization anchors).
        # odds=+100: implied=0.5, b=1.0 -> f = p - q = 2p - 1
        (0.05, 100, 0.1),          # p=0.55 -> 0.55-0.45
        (0.10, 100, 0.2),
        (0.25, 100, 0.5),
        (0.50, 100, 1.0),
        # odds=-200: implied=200/300=2/3, decimal=1.5, b=0.5
        # f = (0.5p - q)/0.5 = 3p - 2 ; p = 2/3 + edge
        (0.05, -200, round(3 * (2 / 3 + 0.05) - 2, 6)),   # 0.15
        (0.01, -200, round(3 * (2 / 3 + 0.01) - 2, 6)),   # 0.03
        # odds=+300: implied=0.25, decimal=4.0, b=3
        # f = (3p - q)/3 = (4p - 1)/3 ; p = 0.25 + edge
        (0.05, 300, round((4 * 0.30 - 1) / 3, 6)),        # ~0.066667
        (0.10, 300, round((4 * 0.35 - 1) / 3, 6)),        # ~0.133333
    ],
)
def test_kelly_full_hand_computed_anchors(edge, odds, expected):
    assert kelly_full(edge, odds) == pytest.approx(expected)


def test_kelly_full_repeating_decimal_gets_rounded():
    # +300 with edge .05 gives 1/15 = 0.0666666... ; kelly_full MUST truncate
    # that tail to six places while the raw core keeps it.
    raw_p = ref_implied(300) + 0.05
    raw = ref_kelly_core_unrounded(raw_p, ref_decimal(300) - 1.0)
    assert raw != round(raw, 6)
    assert kelly_full(0.05, 300) == round(raw, 6)
    assert kelly_full(0.05, 300) == pytest.approx(0.066667)


# ---------------------------------------------------------------------------
# 2. Path B: kelly_core / kelly_binary exact characterization (UNROUNDED)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "p,b,expected",
    [
        (0.55, 1.0, 0.1),
        (0.60, 1.0, 0.2),
        (0.75, 1.0, 0.5),
        (1.0, 1.0, 1.0),
        (0.5, 1.0, 0.0),
        (0.4, 1.0, 0.0),       # -EV floors at zero, never negative
        (0.9, 0.0, 0.0),       # b <= 0 short-circuits to 0.0
        (0.9, -1.0, 0.0),
        (0.9, -0.0001, 0.0),
        (2 / 3, 0.5, 0.0),
        (0.75, 0.5, 0.25),      # (0.375 - 0.25) / 0.5
        (1 / 3, 3.0, 1 / 9),
        (0.4, 3.0, 0.2),        # (1.2 - 0.6) / 3
    ],
)
def test_kelly_core_reference_values(p, b, expected):
    assert kelly_core(p, b) == pytest.approx(expected)


def test_kelly_core_is_the_unrounded_formula_object():
    # Exactly ONE formula in the codebase: kelly_core must delegate directly
    # to the primitive (same behavior), never introduce rounding.
    for p, b in [(0.55, 1.0), (1 / 3, 4.0), (0.9, 0.0), (0.61, 2.37)]:
        assert kelly_core(p, b) == formula_mod.kelly_core_unrounded(p, b)


@pytest.mark.parametrize(
    "fair_prob,decimal_odds",
    [
        (0.55, 2.0),
        (0.52, 1.91),
        (0.60, 2.5),
        (2 / 3, 1.5),
        (0.75, 1.5),
        (0.40, 3.0),
        (0.51, 1.85),
        (0.999, 1.01),
        (0.3333333333, 4.2),
        (1 / 7, 8.0),
    ],
)
def test_kelly_binary_matches_unrounded_reference(fair_prob, decimal_odds):
    got = kelly_binary(fair_prob, decimal_odds)
    want = ref_kelly_binary(fair_prob, decimal_odds)
    assert got == want, "kelly_binary must be bit-identical to the reference"


@pytest.mark.parametrize(
    "fair_prob,decimal_odds",
    [(1 / 3, 4.0), (0.4, 3.0), (0.55, 1.95)],
)
def test_kelly_binary_keeps_full_float_precision(fair_prob, decimal_odds):
    got = kelly_binary(fair_prob, decimal_odds)
    assert got > 0.0
    assert not _is_6dp(got), (
        f"kelly_binary({fair_prob!r}, {decimal_odds!r}) -> {got!r} looks rounded"
    )


def test_kelly_binary_delegates_to_kelly_core_identity():
    p, dec = 0.61, 2.37
    assert kelly_binary(p, dec) == kelly_core(p, dec - 1.0)
    # And it is genuinely unrounded: identical repr-level equality with raw math.
    q = 1.0 - p
    b = dec - 1.0
    assert kelly_binary(p, dec) == (b * p - q) / b


ROUND_TOKEN = "round" + chr(40)  # avoid matching this test's own docstrings


def test_kelly_binary_source_has_no_round_call():
    src = inspect.getsource(kelly_binary)
    assert ROUND_TOKEN not in src, "kelly_binary must not round"


def test_kelly_core_source_has_no_round_call():
    src = inspect.getsource(formula_mod.kelly_core_unrounded)
    assert ROUND_TOKEN not in src, "the canonical formula must stay unrounded"


# ---------------------------------------------------------------------------
# 3. The paths do NOT merge: divergence witnesses
# ---------------------------------------------------------------------------

def test_divergence_witness_paths_differ_on_same_bet():
    """
    Same underlying bet expressed twice:
      - kelly_full(+0.05, +300): American-odds route, ROUNDED to 6 dp.
      - kelly_binary(0.30, 4.0): decimal-odds route, UNROUNDED.
    Both characterize the identical wager, yet their outputs differ in the
    trailing digits — proof that the two paths remain separate.
    """
    rounded_path = kelly_full(0.05, 300)
    unrounded_path = kelly_binary(ref_implied(300) + 0.05, 4.0)
    assert rounded_path == round(unrounded_path, 6)
    assert rounded_path != unrounded_path
    assert unrounded_path == pytest.approx(1 / 15)


def test_divergence_witness_minus_two_hundred_line():
    raw = kelly_core(2 / 3 + 0.05, 0.5)
    assert kelly_full(0.05, -200) == round(raw, 6)
    assert kelly_full(0.05, -200) != raw


@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
@pytest.mark.parametrize("edge", [0.005, 0.02, 0.05])
def test_every_rounded_result_equals_round_of_raw_core(edge, odds):
    p = max(0.0, min(1.0, ref_implied(odds) + edge))
    b = ref_decimal(odds) - 1.0
    raw = kelly_core(p, b)
    assert kelly_full(edge, odds) == round(raw, 6)


def test_kelly_fractional_inherits_rounding_from_kelly_full():
    # kelly_fractional multiplies an already-rounded kelly_full then rounds
    # again to 6 dp — still a 6-decimal surface, never raw precision.
    quarter = kelly_fractional(0.05, 300, fraction=0.25)
    assert quarter == round(kelly_full(0.05, 300) * 0.25, 6)
    assert _is_6dp(quarter)


def test_kelly_fractional_default_is_quarter_kelly():
    assert kelly_fractional(0.10, 100) == round(kelly_full(0.10, 100) * 0.25, 6)


def test_kelly_fractional_half_kelly():
    half = kelly_fractional(0.20, 100, fraction=0.5)
    assert half == round(kelly_full(0.20, 100) * 0.5, 6)


def test_kelly_fractional_zero_edge_stays_zero():
    assert kelly_fractional(0.0, -150) == 0.0


# ---------------------------------------------------------------------------
# 4. Odds-conversion helpers feeding both paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "odds,expected_decimal",
    [(100, 2.0), (+150, 2.5), (300, 4.0), (1000, 11.0),
     (-100, 2.0), (-150, 1 + 2 / 3), (-200, 1.5), (-500, 1.2), (-1000, 1.1)],
)
def test_american_to_decimal_reference(odds, expected_decimal):
    assert _american_to_decimal(odds) == pytest.approx(expected_decimal)
    assert ref_decimal(odds) == pytest.approx(_american_to_decimal(odds))


@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
def test_calculate_implied_probability_matches_reference(odds):
    assert calculate_implied_probability(int(odds)) == pytest.approx(ref_implied(odds))


@pytest.mark.parametrize("odds", ALL_AMERICAN_ODDS)
def test_vig_free_pair_implies_zero_kelly_at_zero_edge(odds):
    # At exactly-implied probability (zero edge), fair Kelly must be 0 on BOTH
    # paths — the market line itself carries no edge.
    implied = ref_implied(odds)
    assert kelly_full(0.0, odds) == 0.0
    assert kelly_binary(implied, ref_decimal(odds)) == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 5. Monotonicity / sanity sweeps across the grid
# ---------------------------------------------------------------------------

def test_kelly_full_monotonic_in_edge_per_odds():
    for odds in ALL_AMERICAN_ODDS:
        prev = -1.0
        for edge in sorted(set(EDGES)):
            cur = kelly_full(edge, odds)
            assert cur >= prev - 1e-12, f"non-monotonic at {odds}, {edge}"
            prev = cur


def test_kelly_core_monotonic_in_b_for_favoured_bet():
    prev = -1.0
    for b in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]:
        cur = kelly_core(0.6, b)
        assert cur >= prev
        prev = cur


def test_kelly_full_grid_values_all_finite():
    for odds in ALL_AMERICAN_ODDS:
        for edge in EDGES:
            assert math.isfinite(kelly_full(edge, odds))
            assert math.isfinite(kelly_binary(ref_implied(odds) + edge, ref_decimal(odds)))


def test_kelly_full_symmetric_lines_plus_minus_100_share_shape():
    # +100 and -100 are the same decimal price (2.0), so equal edges give
    # identical rounded fractions.
    assert kelly_full(0.07, 100) == kelly_full(0.07, -100)


def test_kelly_binary_ev_bet_beats_flat_fraction_floor():
    # For any +EV bet, unrounded binary Kelly is strictly positive.
    assert kelly_binary(0.53, 2.05) > 0
    assert kelly_binary(0.26, 5.5) > 0


def test_kelly_binary_neg_ev_returns_exactly_zero_not_tiny_negative():
    val = kelly_binary(0.10, 1.05)
    assert val == 0.0


def test_kelly_full_returns_python_float_type():
    assert type(kelly_full(0.05, 100)) is float
    assert type(kelly_binary(0.55, 2.0)) is float


# ---------------------------------------------------------------------------
# 6. Fail-closed guardrails (tests-only; production gates untouched)
# ---------------------------------------------------------------------------

def test_paper_trade_signal_statuses_do_not_include_live():
    # The characterization must never widen the live gate. Pin it shut.
    import callisto  # noqa: F401  (ensure app modules import cleanly)

    from tools import sizing as sizing_mod

    for mod_name in ("tools.sizing", "callisto"):
        try:
            mod = __import__(mod_name, fromlist=["_PAPER_TRADE_SIGNAL_STATUSES"])
        except ImportError:
            continue
        statuses = getattr(mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in {str(s).lower() for s in statuses}


def test_generate_paper_trade_signal_signature_not_widened_to_live():
    try:
        from tools.sizing import generate_paper_trade_signal
    except ImportError:
        pytest.skip("generate_paper_trade_signal not present in this tree")
    src = inspect.getsource(generate_paper_trade_signal)
    # No branch may treat status=='live' as a paper-trade trigger.
    for banned in ("== 'live'", '== "live"', "status in ('live'", '"live",'):
        assert banned not in src, f"paper-trade path references live: {banned!r}"


def test_characterization_module_touches_no_live_surface():
    # Belt-and-braces: this very module must not mutate any live-betting
    # surface. Keep the test honest about its own scope.
    src = open(__file__, encoding="utf-8").read()
    assert ("_PAPER_TRADE_SIGNAL_STATUSES" + ".add") not in src
    assert ("statuses.add" + "('live')") not in src
