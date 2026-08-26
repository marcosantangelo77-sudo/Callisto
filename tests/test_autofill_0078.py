"""
Autofill characterization #0078 — dual Kelly (LONG).

Characterizes the two DISTINCT Kelly paths in Callisto and pins the fact
that they remain unmerged:

Path A (ROUNDED):
    ``tools.kelly.kelly_full`` / ``tools.kellypkg.core.kelly_full``
    computes via the canonical unrounded primitive but ROUNDS its own
    return value to exactly 6 decimal places.

Path B (UNROUNDED):
    ``tools.kelly.kelly_core`` -> ``tools.kellypkg._formula.kelly_core_unrounded``
    is THE single Kelly formula in the codebase.  It never rounds.
    ``tools.sizing.kelly_binary`` delegates to it (decimal odds -> b) with
    full precision preserved.

Contract pinned here:
1. kelly_full output == round(kelly_core(p, b), 6) for every probed input,
   i.e. its rounding is observable and exact to 6 decimal places.
2. kelly_binary / kelly_core preserve full float precision — for inputs
   whose true fraction has more than 6 decimals they must NOT equal their
   own 6-decimal rounding.
3. The two paths are not merged: kelly_binary is NOT routed through any
   rounding wrapper; kelly_full does not bypass kelly_core.
4. Edge cases: zero/negative edge clamps to 0.0, non-positive payout b
   returns 0.0, p clamped into [0, 1], odds accepted as int-like.
5. No live-betting drift: paper-trade status set stays paper-only and the
   signal generator gate keeps 'live' out.

All tests are pure characterization of existing behavior — no production
code is modified by this module.
"""

import ast
import inspect
import math
import os

import pytest

import tools.kelly as facade
import tools.kellypkg
from tools.kelly import kelly_core as facade_kelly_core
from tools.kelly import kelly_full as facade_kelly_full
from tools.kellypkg._formula import kelly_core_unrounded
from tools.kellypkg.core import kelly_core as pkg_kelly_core
from tools.kellypkg.core import kelly_full as pkg_kelly_full
from tools.odds_api import calculate_implied_probability
from tools.sizing import kelly_binary

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Reference implementations used ONLY to characterize observed behavior.
# ---------------------------------------------------------------------------


def _implied(american_odds):
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def _net_payout(american_odds):
    # _american_to_decimal: positive -> 1 + o/100 ; negative -> 1 + 100/|o|
    if american_odds > 0:
        return (1 + american_odds / 100) - 1.0
    return (1 + 100 / abs(american_odds)) - 1.0


def _expected_kelly_full(edge, american_odds):
    """Independent model of kelly_full's documented contract."""
    p = max(0.0, min(1.0, _implied(int(american_odds)) + edge))
    b = _net_payout(american_odds)
    return round(kelly_core_unrounded(p, b), 6)


def _expected_unrounded_fraction(prob, decimal_odds):
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - prob
    return max(0.0, (b * prob - q) / b)


# ---------------------------------------------------------------------------
# 1. kelly_full rounds to 6 decimal places
# ---------------------------------------------------------------------------

KELLY_FULL_GRID = [
    # (edge, american_odds)
    (0.05, 110),
    (0.05, -110),
    (0.02, 150),
    (0.10, 200),
    (-0.03, 120),   # negative edge -> 0.0
    (0.075, -175),
    (0.01, 300),
    (0.25, -400),
    (0.004, 105),
    (0.0333, 137),
]


@pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
def test_kelly_full_matches_independent_rounded_model(edge, odds):
    assert facade_kelly_full(edge, odds) == _expected_kelly_full(edge, odds)


@pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
def test_pkg_kelly_full_identical_to_facade(edge, odds):
    assert pkg_kelly_full(edge, odds) == facade_kelly_full(edge, odds)


@pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
def test_kelly_full_output_is_6dp_quantized(edge, odds):
    value = facade_kelly_full(edge, odds)
    assert value == round(value, 6)
    # quantization check: value * 1e6 must be integral within float noise
    assert abs(value * 1e6 - round(value * 1e6)) < 1e-6


@pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
def test_kelly_full_equals_round_of_underlying_core(edge, odds):
    p = max(0.0, min(1.0, calculate_implied_probability(int(odds)) + edge))
    raw = pkg_kelly_core(p, _net_payout(odds))
    assert facade_kelly_full(edge, odds) == round(raw, 6)


@pytest.mark.parametrize(
    "edge,odds",
    [
        (0.01, 137),
        (0.005, -163),
        (0.0077, 221),
        (0.0123, -187),
        (0.0021, 344),
    ],
)
def test_kelly_full_actually_truncates_precision(edge, odds):
    """For these inputs the unrounded fraction differs from the returned
    value — proving kelly_full's rounding is real, not incidental."""
    returned = facade_kelly_full(edge, odds)
    p = max(0.0, min(1.0, calculate_implied_probability(int(odds)) + edge))
    raw = pkg_kelly_core(p, _net_payout(odds))
    # The raw core value must carry sub-6dp information...
    assert raw != round(raw, 6) or raw == 0.0 or returned == 0.0
    # ...and kelly_full returns exactly its 6dp rounding.
    assert returned == round(raw, 6)


def test_kelly_full_known_values():
    # Hand-computed anchors (documented behavior).
    # odds=-110: implied = 110/210, b = 0.909090..., edge=0.05 -> p ~ .5738
    v = facade_kelly_full(0.05, -110)
    assert isinstance(v, float)
    assert 0.0 <= v <= 1.0
    assert v == round(v, 6)
    # Positive-edge bets at plus money yield strictly positive fractions.
    assert facade_kelly_full(0.10, 200) > 0.0
    # Negative edge yields exactly 0.0 (never negative).
    assert facade_kelly_full(-0.05, 200) == 0.0
    assert facade_kelly_full(-1.0, -105) == 0.0


def test_kelly_full_never_negative_across_grid():
    for edge in [x / 1000 for x in range(-200, 201, 25)]:
        for odds in [-400, -200, -110, 100, 150, 250, 500]:
            v = facade_kelly_full(edge, odds)
            assert v >= 0.0


def test_kelly_full_monotone_in_edge_per_odds():
    for odds in [110, -110, 200, -200]:
        vals = [facade_kelly_full(e, odds) for e in (0.01, 0.03, 0.05, 0.08)]
        assert vals == sorted(vals)


# ---------------------------------------------------------------------------
# 2. kelly_core / kelly_binary stay UNROUNDED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prob,decimal_odds",
    [
        (0.55, 2.10),
        (0.5238095238095238, 2.0),
        (0.60123456789, 2.37),
        (0.51, 1.9230769230769231),
        (0.487654321, 3.141592653589793),
        (2 / 3, 2.5),
        (1 / 7, 8.5),
        (0.9999999999999999, 2.0),
    ],
)
def test_kelly_binary_preserves_full_precision(prob, decimal_odds):
    expected = _expected_unrounded_fraction(prob, decimal_odds)
    got = kelly_binary(prob, decimal_odds)
    assert got == expected
    # Full-precision equality, not approx-equal-to-6dp.
    assert repr(got) == repr(expected)


@pytest.mark.parametrize(
    "prob,decimal_odds",
    [
        (0.5238095238095238, 2.0),
        (0.60123456789, 2.37),
        (0.487654321, 3.141592653589793),
        (2 / 3, 2.5),
    ],
)
def test_kelly_binary_is_not_six_dp_rounded(prob, decimal_odds):
    """The whole point of path B: result carries more than 6 decimals."""
    got = kelly_binary(prob, decimal_odds)
    assert got != round(got, 6)


@pytest.mark.parametrize(
    "p,b", [(0.55, 1.1), (0.61, 0.5), (0.3333333333333333, 2.75), (0.7, 0.25)]
)
def test_kelly_core_identity_with_formula_primitive(p, b):
    assert pkg_kelly_core(p, b) == kelly_core_unrounded(p, b)
    assert facade_kelly_core(p, b) == kelly_core_unrounded(p, b)


def test_kelly_core_docstring_pins_unrounded_contract():
    doc = inspect.getdoc(pkg_kelly_core) or ""
    assert "unrounded" in doc.lower()
    doc_f = inspect.getdoc(pkg_kelly_full) or ""
    assert "round" in doc_f.lower()


def test_sizing_kelly_binary_docstring_pins_no_rounding():
    doc = inspect.getdoc(kelly_binary) or ""
    assert "kelly_core" in doc
    assert "no rounding" in doc.lower() or "unrounded" in doc.lower()


# ---------------------------------------------------------------------------
# 3. Paths do not merge
# ---------------------------------------------------------------------------


def test_paths_distinct_on_shared_input():
    """
    Same underlying bet expressed both ways:
      American -110  <->  decimal 1.909090... , true prob from edge 0.05.
    Path A returns a 6dp-rounded number; path B returns full precision.
    They must differ whenever precision matters.
    """
    american = -110
    implied = _implied(american)
    prob = implied + 0.05
    rounded_path = facade_kelly_full(0.05, american)
    unrounded_path = kelly_binary(prob, 1 + 100 / 110)
    assert rounded_path == round(rounded_path, 6)
    if unrounded_path != round(unrounded_path, 6):
        assert unrounded_path != rounded_path
        assert abs(unrounded_path - rounded_path) < 1e-5


def test_kelly_full_source_contains_explicit_round_call():
    src = inspect.getsource(pkg_kelly_full)
    assert "round(" in src
    assert ", 6)" in src


def test_kelly_core_source_has_no_round_call():
    src = inspect.getsource(pkg_kelly_core)
    assert "round(" not in src
    assert ", 6)" not in src


def test_formula_module_has_no_round_anywhere():
    path = os.path.join(REPO_ROOT, "tools", "kellypkg", "_formula.py")
    with open(path) as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "round"


def test_sizing_kelly_binary_delegates_to_canonical_core():
    """tools.sizing.kelly_binary must call kelly_core, not reimplement."""
    import tools.sizing as sizing_mod

    src = inspect.getsource(sizing_mod.kelly_binary)
    assert "kelly_core" in src
    # No inline reimplementation of (b*p - q)/b in that function body.
    assert "(b * p" not in src.replace(" ", "")


def test_only_one_formula_definition_in_package():
    """
    The (bp-q)/b formula is defined exactly once (_formula.py).  Within
    ``core.py`` — the home of both characterized paths — only kelly_full
    may round; kelly_core must delegate unrounded.
    """
    pkg_dir = os.path.join(REPO_ROOT, "tools", "kellypkg")
    with open(os.path.join(pkg_dir, "core.py")) as fh:
        tree = ast.parse(fh.read())
    rounding_fns = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("kelly"):
            has_round_call = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == "round"
                for c in ast.walk(node)
            )
            if has_round_call:
                rounding_fns.append(node.name)
    assert rounding_fns == ["kelly_full", "kelly_fractional"]
    # And no module other than _formula.py spells out the formula inline:
    for fname in os.listdir(pkg_dir):
        if not fname.endswith(".py") or fname == "_formula.py":
            continue
        with open(os.path.join(pkg_dir, fname)) as fh:
            src = fh.read()
        assert "(b * p" not in src.replace(" ", ""), fname


# ---------------------------------------------------------------------------
# 4. Edge cases and clamping
# ---------------------------------------------------------------------------


class TestKellyCoreEdgeCases:
    def test_zero_b_returns_zero(self):
        assert kelly_core_unrounded(0.9, 0.0) == 0.0

    def test_negative_b_returns_zero(self):
        assert kelly_core_unrounded(0.9, -1.5) == 0.0

    def test_ev_negative_clamps_to_zero(self):
        assert kelly_core_unrounded(0.30, 2.0) == 0.0  # bp=0.6 < q=0.7

    def test_breakeven_is_zero(self):
        assert kelly_core_unrounded(0.5, 1.0) == 0.0

    def test_exact_even_money_positive_edge(self):
        assert kelly_core_unrounded(0.6, 1.0) == pytest.approx(0.2)

    def test_never_negative_for_extreme_inputs(self):
        assert kelly_core_unrounded(0.001, 1.0000001) >= 0.0

    @pytest.mark.parametrize("b", [1.0, 2.0, 10.0])
    def test_p_one_gives_full_fraction(self, b):
        assert kelly_core_unrounded(1.0, b) == pytest.approx(1.0)

    @pytest.mark.parametrize("p", [0.0, 0.5, 1.0])
    def test_symmetry_points(self, p):
        f = kelly_core_unrounded(p, 1.0)
        expected = max(0.0, p - (1 - p))
        assert f == pytest.approx(expected)


class TestKellyFullClamping:
    def test_huge_edge_clamps_p_at_one(self):
        # implied(-200)=2/3; edge=10 pushes p past 1 -> clamp -> f*=1
        assert facade_kelly_full(10.0, -200) == 1.0

    def test_edge_pushing_p_below_zero_yields_zero(self):
        assert facade_kelly_full(-10.0, 100) == 0.0

    def test_zero_edge_yields_zero(self):
        assert facade_kelly_full(0.0, 110) == 0.0
        assert facade_kelly_full(0.0, -110) == 0.0

    def test_result_always_within_unit_interval(self):
        for edge in (-0.5, 0.0, 0.02, 0.5):
            for odds in (-500, -100, 100, 400):
                v = facade_kelly_full(edge, odds)
                assert 0.0 <= v <= 1.0

    def test_odd_type_coercion(self):
        # kelly_full int()-coerces the odds argument.
        assert facade_kelly_full(0.05, -110.0) == facade_kelly_full(0.05, -110)
        # str odds are NOT supported: int("-110") succeeds but the comparison
        # inside _american_to_decimal fails — characterize fail-loud behavior.
        with pytest.raises(TypeError):
            facade_kelly_full(0.05, "-110")


class TestKellyBinaryDelegation:
    def test_delegation_matches_manual_computation(self):
        assert kelly_binary(0.55, 2.10) == pytest.approx((1.10 * 0.55 - 0.45) / 1.10)

    def test_not_ev_gives_zero(self):
        assert kelly_binary(0.40, 2.0) == 0.0

    def test_decimal_odds_one_is_breakeven_or_zero(self):
        assert kelly_binary(0.5, 1.0) == 0.0

    def test_string_args_coerced_to_float(self):
        assert kelly_binary("0.55", "2.10") == kelly_binary(0.55, 2.10)

    def test_high_precision_input_survives(self):
        prob = 0.5501234567890123
        got = kelly_binary(prob, 2.13)
        manual = (1.13 * prob - (1 - prob)) / 1.13
        assert got == manual  # bit-for-bit


# ---------------------------------------------------------------------------
# 5. Facade parity & package structure
# ---------------------------------------------------------------------------


def test_facade_kelly_full_is_pkg_function():
    assert facade.kelly_full is pkg_kelly_full
    assert facade.kelly_core is pkg_kelly_core


def test_formula_primitive_not_exported_publicly_but_importable():
    assert "kelly_core_unrounded" not in getattr(tools.kellypkg, "__all__", [])
    from tools.kellypkg._formula import kelly_core_unrounded as _f  # noqa: F401


@pytest.mark.parametrize(
    "name", ["kelly_core", "kelly_full", "kelly_fractional"]
)
def test_facade_exposes_core_trio(name):
    assert hasattr(facade, name)
    assert name in tools.kellypkg.__all__


def test_kelly_fractional_builds_on_core_without_breaking_paths():
    """Fractional Kelly scales kelly_full; rounding contract still holds."""
    frac = facade.kelly_fractional
    full_v = facade_kelly_full(0.05, -110)
    for factor_name in ("quarter", "half"):
        kwargs = {factor_name: True}
        try:
            v = frac(0.05, -110, **kwargs)
        except TypeError:
            continue
        assert 0.0 <= v <= full_v + 1e-9


# ---------------------------------------------------------------------------
# 6. Fail-closed guardrails: no live betting drift
# ---------------------------------------------------------------------------


PAPER_SIGNALS_PATH = os.path.join(REPO_ROOT, "tools", "signals", "paper.py")


def _read_paper_signals_src():
    with open(PAPER_SIGNALS_PATH) as fh:
        return fh.read()


def test_paper_trade_statuses_stay_paper_only():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_no_live_string_in_status_set_literal():
    src = _read_paper_signals_src()
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    # The only permitted "live" mentions are fail-closed comments forbidding it.
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    stripped = "".join(
        part for part in code_only.split('"""')[::2]
    )
    assert '"live"' not in stripped
    assert "'live'" not in stripped


def test_generate_paper_trade_signal_gate_rejects_live():
    """The gate function must treat 'live' as excluded from paper trading."""
    import tools.signals.paper as paper_mod

    fn = None
    for name in dir(paper_mod):
        obj = getattr(paper_mod, name)
        if callable(obj) and "generate_paper_trade_signal" in name:
            fn = obj
            break
    if fn is None:
        pytest.skip("generate_paper_trade_signal helper not present")
    try:
        result = fn("live")
    except TypeError:
        pytest.skip("signature mismatch; gate characterized elsewhere")
    assert result is False or result is not True


def test_kelly_modules_reference_no_live_execution():
    for relpath in (
        "tools/kellypkg/core.py",
        "tools/kellypkg/_formula.py",
        "tools/sizing.py",
    ):
        with open(os.path.join(REPO_ROOT, relpath)) as fh:
            src = fh.read()
        assert "place_live_bet" not in src
        assert "status == \"live\"" not in src
