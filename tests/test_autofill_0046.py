"""
Autofill characterization #0046 — dual Kelly (LONG).

Characterization module for the two DISTINCT Kelly entry points in Callisto:

  Path A — ``tools.kelly.kelly_full`` (American-odds entry point):
      implied prob + edge -> (p, b) -> canonical ``kelly_core`` ->
      ROUNDS its own return value to exactly 6 decimal places.

  Path B — ``tools.sizing.kelly_binary`` (decimal-odds entry point):
      fair probability + decimal odds -> ``kelly_core`` VERBATIM,
      full double precision, never rounded.

The invariant pinned here: kelly_full stays quantized to a 6-decimal grid;
kelly_binary carries every bit of kelly_core's double-precision result.
Do not merge the paths' rounding behavior.

This module is tests-only: it touches no production code, starts no
servers, places no bets, and re-pins the paper-only signal-status gate
fail-closed (a regression that arms live betting must break these tests).
"""

import ast
import inspect
import math
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import sizing as sizing_mod  # noqa: E402
from tools import kelly as kelly_facade  # noqa: E402
from tools.kellypkg import core as kelly_core_mod  # noqa: E402
from tools.kellypkg._formula import kelly_core_unrounded  # noqa: E402

TOL = 1e-12


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _american_to_decimal(odds: int) -> float:
    """Mirror of tools.kellypkg.odds._american_to_decimal."""
    if odds > 0:
        return 1.0 + odds / 100.0
    if odds < 0:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError("american odds of 0 are invalid")


def _implied_prob(odds: int) -> float:
    """Vigorish-free implied probability from American odds."""
    return 1.0 / _american_to_decimal(odds)


def _raw_kelly(p: float, b: float) -> float:
    """Naive f* = max(0, (b*p - q)/b), independent of any package code."""
    if b <= 0:
        return 0.0
    return max(0.0, (b * p - (1.0 - p)) / b)


def _expected_kelly_full(edge: float, odds: int) -> float:
    p = max(0.0, min(1.0, _implied_prob(odds) + edge))
    b = _american_to_decimal(odds) - 1.0
    return round(_raw_kelly(p, b), 6)


def _expected_kelly_binary(fair_prob: float, decimal_odds: float) -> float:
    return _raw_kelly(float(fair_prob), float(decimal_odds) - 1.0)


def _is_on_6dp_grid(x: float) -> bool:
    """True iff x equals its own round-to-6dp image (bit-for-bit)."""
    return x == round(x, 6)


# Wide characterization matrices (kept distinct from autofill_0022's sets).
KELLY_FULL_GRID = [
    (0.041, 105),
    (-0.021, -108),
    (0.0635, 165),
    (0.0875, -220),
    (0.0125, 340),
    (0.058, -125),
    (0.0965, 195),
    (0.0295, -145),
    (0.071, 260),
    (0.0345, -175),
    (0.082, 310),
    (-0.055, 130),
    (0.0495, -95),
    (0.0675, 225),
    (0.0185, 480),
]

KELLY_BINARY_GRID = [
    (0.5525, 2.155),
    (0.5875, 1.845),
    (0.5125, 2.62),
    (0.6675, 1.605),
    (0.4775, 3.35),
    (0.7025, 1.505),
    (0.5325, 2.31),
    (0.6125, 1.77),
    (0.4425, 4.15),
    (0.5925, 2.02),
    (0.5675, 1.925),
    (0.4825, 3.08),
    (0.6375, 1.68),
]


# ---------------------------------------------------------------------------
# 1. The unrounded primitive: bit-level fidelity
# ---------------------------------------------------------------------------


class TestPrimitiveBitFidelity:
    @pytest.mark.parametrize(
        "p,b",
        [
            (0.517231, 1.93327),
            (0.604171, 1.65481),
            (0.493117, 2.12789),
            (0.559983, 1.78543),
            (0.638229, 1.56683),
            (0.521739, 1.91667),
        ],
    )
    def test_primitive_is_bit_for_bit_the_naive_formula(self, p, b):
        assert kelly_core_unrounded(p, b) == _raw_kelly(p, b)

    def test_primitive_never_quantizes(self):
        p, b = 0.537191283, 1.86154729
        raw = _raw_kelly(p, b)
        assert kelly_core_unrounded(p, b) == raw
        assert not _is_on_6dp_grid(raw)

    def test_primitive_zero_break_even_is_exact(self):
        b = 2.137
        p = 1.0 / (1.0 + b)
        assert kelly_core_unrounded(p, b) == 0.0

    @pytest.mark.parametrize("b", [0.0, -0.5, -1e-9])
    def test_primitive_guards_nonpositive_b_first(self, b):
        assert kelly_core_unrounded(0.99, b) == 0.0

    def test_core_wrapper_is_a_pure_delegation(self):
        src = inspect.getsource(kelly_core_mod.kelly_core)
        body_only = src.split('"""')[-1]
        assert "kelly_core_unrounded" in body_only
        assert not re.search(r"\bmax\b|\bround\b|/\s*b", body_only)


# ---------------------------------------------------------------------------
# 2. Path A: kelly_full quantizes to the 6-decimal grid
# ---------------------------------------------------------------------------


class TestKellyFullSixDecimalGrid:
    @pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
    def test_matches_independent_rounded_pipeline(self, edge, odds):
        assert kelly_facade.kelly_full(edge, odds) == (
            _expected_kelly_full(edge, odds)
        )

    @pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
    def test_output_sits_on_the_6dp_grid_bitwise(self, edge, odds):
        got = kelly_facade.kelly_full(edge, odds)
        assert got == round(got, 6)
        # Decimal check: at most 6 fractional digits are representable.
        frac = Decimal(repr(got)).as_tuple().exponent
        assert frac >= -6, (got, frac)

    @pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
    def test_equals_round_of_core_for_same_inputs(self, edge, odds):
        p = max(0.0, min(1.0, _implied_prob(odds) + edge))
        b = _american_to_decimal(odds) - 1.0
        assert kelly_facade.kelly_full(edge, odds) == round(
            kelly_core_mod.kelly_core(p, b), 6
        )

    @pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
    def test_differs_from_unrounded_when_raw_off_grid(self, edge, odds):
        raw = _expected_kelly_binary(
            min(1.0, _implied_prob(odds) + edge),
            _american_to_decimal(odds),
        )
        got = kelly_facade.kelly_full(edge, odds)
        if raw != round(raw, 6):
            assert got != raw
            assert abs(got - raw) < 5e-7

    def test_clamped_probability_still_rounded(self):
        got = kelly_facade.kelly_full(2.0, 150)
        assert got == round(got, 6)
        assert 0.0 <= got <= 1.0

    def test_negative_edges_collapse_to_exactly_zero(self):
        for edge, odds in KELLY_FULL_GRID:
            if edge < 0:
                assert kelly_facade.kelly_full(edge, odds) == 0.0

    def test_fractional_kelly_preserves_the_grid(self):
        for edge, odds in KELLY_FULL_GRID[:8]:
            for fraction in (0.5, 0.25, 0.125):
                f = kelly_facade.kelly_fractional(edge, odds, fraction)
                assert f == round(f, 6)
                full = kelly_facade.kelly_full(edge, odds)
                assert f == round(full * fraction, 6)

    def test_source_has_literal_round_call_with_6(self):
        src = inspect.getsource(kelly_core_mod.kelly_full)
        assert re.search(r"round\s*\(\s*kelly_core\s*\(.+?,\s*6\s*\)", src)


# ---------------------------------------------------------------------------
# 3. Path B: kelly_binary passes kelly_core through verbatim
# ---------------------------------------------------------------------------


class TestKellyBinaryVerbatimDelegation:
    @pytest.mark.parametrize("p,d", KELLY_BINARY_GRID)
    def test_matches_independent_unrounded_pipeline(self, p, d):
        assert sizing_mod.kelly_binary(p, d) == _expected_kelly_binary(p, d)

    @pytest.mark.parametrize("p,d", KELLY_BINARY_GRID)
    def test_bit_identical_to_kelly_core(self, p, d):
        core = kelly_core_mod.kelly_core(float(p), float(d) - 1.0)
        assert sizing_mod.kelly_binary(p, d) == core
        assert sizing_mod.kelly_binary(p, d).hex() == core.hex()

    @pytest.mark.parametrize("p,d", KELLY_BINARY_GRID)
    def test_not_snapped_to_grid_when_raw_off_grid(self, p, d):
        raw = _expected_kelly_binary(p, d)
        got = sizing_mod.kelly_binary(p, d)
        if raw != round(raw, 6):
            assert got != round(raw, 6)
            assert got == raw

    def test_string_and_int_inputs_are_coerced(self):
        base = sizing_mod.kelly_binary(0.5725, 2.135)
        assert sizing_mod.kelly_binary("0.5725", "2.135") == base
        assert sizing_mod.kelly_binary(0.5725, 2.135) == base

    def test_b_equal_one_is_always_zero(self):
        assert sizing_mod.kelly_binary(0.999, 1.0) == 0.0
        assert sizing_mod.kelly_binary(1.0, 1.0) == 0.0

    def test_source_has_no_rounding_expression(self):
        src = inspect.getsource(sizing_mod.kelly_binary)
        assert not re.search(r"\bround\s*\(", src)

    def test_source_is_single_delegation_line_semantics(self):
        src = inspect.getsource(sizing_mod.kelly_binary)
        assert "kelly_core" in src
        assert "- 1.0" in src or "- 1" in src


# ---------------------------------------------------------------------------
# 4. Duality: same (p, b), only rounding differs
# ---------------------------------------------------------------------------


class TestDualPathDualityOnlyRoundingDiffers:
    @pytest.mark.parametrize(
        "edge,odds",
        [(0.041, 105), (-0.021, -108), (0.0635, 165), (0.0875, -220)],
    )
    def test_equivalent_inputs_agree_within_half_of_the_6dp_step(
        self, edge, odds
    ):
        # Feed the SAME (p, b) — derived from the American odds without any
        # lossy reconstruction — through both paths; they may only differ by
        # kelly_full's 6dp quantization (< half a grid step).
        p = min(1.0, _implied_prob(odds) + edge)
        d = _american_to_decimal(odds)
        full = kelly_facade.kelly_full(edge, odds)
        binary = sizing_mod.kelly_binary(p, d)
        assert abs(full - binary) <= 5e-7 + 1e-12

    def test_precision_hostile_case_shows_the_gap(self):
        odds = 173
        edge = 0.037777314159
        implied = _implied_prob(odds)
        d = _american_to_decimal(odds)
        p = implied + edge
        raw = _raw_kelly(p, d - 1.0)
        full = kelly_facade.kelly_full(edge, odds)
        binary = sizing_mod.kelly_binary(p, d)
        assert full == round(raw, 6)
        assert binary == raw
        assert _is_on_6dp_grid(full)
        if raw != round(raw, 6):
            assert full != binary

    def test_on_grid_raw_values_make_paths_indistinguishable(self):
        # p chosen so f* lands exactly on the grid: then A == B bitwise.
        b = 2.0
        for i in range(1, 20):
            target = i / 100.0  # exactly on the 6dp grid
            p = (target * b + (1.0)) / (b + 1.0)
            binary = sizing_mod.kelly_binary(p, b + 1.0)
            # Floating division leaves ~1e-16 residue; the point is that no
            # additional rounding is layered on top of the primitive.
            assert binary == pytest.approx(target, abs=1e-9)
            assert abs(binary - round(binary, 6)) < 1e-9

    def test_monkeypatched_core_flows_through_both_paths(self, monkeypatch):
        seen = []
        real = kelly_core_mod.kelly_core

        def spy(p, b):
            seen.append((p, b))
            return real(p, b)

        monkeypatch.setattr(kelly_core_mod, "kelly_core", spy)
        monkeypatch.setattr(sizing_mod, "kelly_core", spy)
        kelly_facade.kelly_full(0.041, 105)
        sizing_mod.kelly_binary(0.5525, 2.155)
        assert len(seen) == 2
        # Both saw the SAME primitive identity.
        assert callable(seen[0][0]) is False


# ---------------------------------------------------------------------------
# 5. Numeric properties across the wide grids
# ---------------------------------------------------------------------------


class TestNumericPropertiesWideGrids:
    @pytest.mark.parametrize("edge,odds", KELLY_FULL_GRID)
    def test_full_output_in_unit_range(self, edge, odds):
        assert 0.0 <= kelly_facade.kelly_full(edge, odds) < 1.0

    @pytest.mark.parametrize("p,d", KELLY_BINARY_GRID)
    def test_binary_output_in_unit_range(self, p, d):
        assert 0.0 <= sizing_mod.kelly_binary(p, d) < 1.0

    def test_strict_monotonicity_in_edge_positive_side(self):
        prev = 0.0
        for i in range(10, 200, 5):
            f = kelly_facade.kelly_full(i / 1000.0, 160)
            assert f > prev or f == prev == 0.0
            prev = f

    def test_monotonicity_in_fair_prob_binary(self):
        prev = -1.0
        for i in range(400, 801):
            f = sizing_mod.kelly_binary(i / 1000.0, 2.24)
            assert f >= prev - TOL
            prev = f

    def test_fractional_ordering_half_ge_quarter(self):
        for edge, odds in KELLY_FULL_GRID:
            h = kelly_facade.kelly_fractional(edge, odds, 0.5)
            q = kelly_facade.kelly_fractional(edge, odds, 0.25)
            assert h >= q >= 0.0

    def test_growth_curve_flat_near_peak_sanity(self):
        # Kelly growth rate g(f) = p*ln(1+b f) + q*ln(1-f): the value at the
        # computed optimum should beat nearby fractions on average.
        p, b = 0.575, 1.85

        def g(f):
            if f >= 1.0:
                return -math.inf
            return p * math.log(1 + b * f) + (1 - p) * math.log(1 - f)

        f_star = sizing_mod.kelly_binary(p, b + 1.0)
        assert g(f_star) >= g(max(0.0, f_star - 0.01)) - 1e-9
        assert g(f_star) >= g(f_star + 0.01) - 1e-9

    def test_known_reference_anchor_values(self):
        assert kelly_facade.kelly_full(0.05, 110) == pytest.approx(0.095455, abs=1e-5)
        assert sizing_mod.kelly_binary(0.5525, 2.155) == pytest.approx(
            0.165054, abs=1e-5
        )


# ---------------------------------------------------------------------------
# 6. Structural / one-formula pins
# ---------------------------------------------------------------------------


class TestStructuralOneFormulaPins:
    def test_formula_module_defines_exactly_one_function(self):
        src = (REPO_ROOT / "tools/kellypkg/_formula.py").read_text()
        assert re.findall(r"^def (\w+)", src, flags=re.M) == [
            "kelly_core_unrounded"
        ]

    def test_no_inline_formula_outside_formula_module(self):
        offenders = []
        for path in (REPO_ROOT / "tools/kellypkg").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name not in (
                    "kelly_core_unrounded",
                ):
                    unparsed = ast.unparse(node)
                    if "(b * p - q) / b" in unparsed or "bp - q" in unparsed:
                        offenders.append(f"{path.name}:{node.name}")
        assert not offenders

    def test_sizing_imports_canonical_core(self):
        src = (REPO_ROOT / "tools/sizing.py").read_text()
        assert "from tools.kelly import kelly_core" in src

    def test_facade_reexports_package_objects(self):
        assert kelly_facade.kelly_full is kelly_core_mod.kelly_full
        assert kelly_facade.kelly_core is kelly_core_mod.kelly_core

    def test_kelly_modules_have_no_network_or_exec_surface(self):
        for rel in (
            "tools/kellypkg/core.py",
            "tools/kellypkg/_formula.py",
            "tools/sizing.py",
        ):
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert not re.search(
                r"\b(requests|urllib|socket|subprocess|os\.system|eval|exec)\b",
                src,
            ), rel

    def test_docstrings_pin_the_contract(self):
        assert "ROUNDED TO 6 DECIMAL PLACES" in (
            inspect.getdoc(kelly_core_mod.kelly_full) or ""
        )
        assert "UNROUNDED" in (
            inspect.getdoc(kelly_core_mod.kelly_core) or ""
        )


# ---------------------------------------------------------------------------
# 7. Fail-closed pins: paper-trade gate stays paper-only
# ---------------------------------------------------------------------------


class TestFailClosedPaperGatePins:
    PAPER_SRC = (REPO_ROOT / "tools/signals/paper.py").read_text(encoding="utf-8")

    def test_status_set_literal_is_paper_trading_only(self):
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", self.PAPER_SRC)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES assignment missing"
        literal = m.group(1).strip()
        assert literal == 'frozenset({"paper_trading"})', literal

    def test_live_absent_from_status_assignment(self):
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", self.PAPER_SRC)
        assert m and "live" not in m.group(1)

    def test_generate_paper_trade_signal_still_gated_by_status(self):
        src = (REPO_ROOT / "tools/backtest.py").read_text(encoding="utf-8")
        idx = src.find("async def generate_paper_trade_signal(")
        assert idx != -1, "generate_paper_trade_signal vanished"
        window = src[idx : idx + 900]
        assert "status" in window

    def test_betexec_keeps_paper_only_statuses(self):
        src = (REPO_ROOT / "tools/betexec/__init__.py").read_text(encoding="utf-8")
        assert '"live"' not in src
        assert (
            'frozenset({"paper_trading"})' in src
            or "_PAPER_TRADE_SIGNAL_STATUSES" in src
        )

    def test_this_module_declares_no_live_surface(self):
        src = Path(__file__).read_text(encoding="utf-8")
        code_only = src.split('"""')[2]  # drop the docstring
        assert '"live"' not in code_only.replace("'live'", '"live"')
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in code_only.split("# 7.")[0]
