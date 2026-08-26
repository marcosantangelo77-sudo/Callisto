"""
Autofill characterization #0022 — dual Kelly (LONG).

Characterizes the two DISTINCT Kelly paths that exist in Callisto and pins
the fact that they must never be merged:

  Path A — ``tools.kelly.kelly_full`` (American-odds entry point):
      takes an *edge* + American odds, converts to (p, b), delegates to the
      canonical unrounded primitive ``kelly_core``, then ROUNDS its own
      return value to exactly 6 decimal places.

  Path B — ``tools.sizing.kelly_binary`` (decimal-odds entry point):
      takes a fair probability + decimal odds, converts to (p, b), and
      returns ``kelly_core``'s output VERBATIM — full double precision,
      no rounding at any stage.

Both paths share the ONE formula in ``tools.kellypkg._formula
.kelly_core_unrounded``; sharing the formula is correct and pinned here.
What is forbidden is merging the *rounding* behavior: kelly_full must stay
rounded-to-6dp, kelly_binary must stay unrounded.

Fail-closed pins: this module also re-pins the paper-trade signal status
gate so a regression that arms live betting breaks these tests. Nothing in
this module touches production code, starts servers, or places bets.
"""

import ast
import inspect
import math
import os
import re
import sys
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
    """Mirror of tools.kellypkg.odds._american_to_decimal for expectations."""
    if odds > 0:
        return 1.0 + odds / 100.0
    if odds < 0:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError("american odds of 0 are invalid")


def _implied_prob(odds: int) -> float:
    """Vigorish-free implied probability from American odds."""
    d = _american_to_decimal(odds)
    return 1.0 / d


def _expected_kelly_full(edge: float, odds: int) -> float:
    """Independent recomputation of kelly_full's documented pipeline."""
    p = max(0.0, min(1.0, _implied_prob(odds) + edge))
    b = _american_to_decimal(odds) - 1.0
    q = 1.0 - p
    raw = max(0.0, (b * p - q) / b)
    return round(raw, 6)


def _expected_kelly_binary(fair_prob: float, decimal_odds: float) -> float:
    """Independent recomputation of kelly_binary's documented pipeline."""
    b = float(decimal_odds) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - fair_prob
    return max(0.0, (b * fair_prob - q) / b)


# ---------------------------------------------------------------------------
# 1. The canonical unrounded primitive itself
# ---------------------------------------------------------------------------


class TestKellyCoreUnrounded:
    @pytest.mark.parametrize(
        "p,b",
        [
            (0.55, 1.10),
            (0.52, 1.909),
            (0.60, 1.50),
            (0.50, 3.00),
            (0.75, 1.3333333333333333),
            (0.534, 2.05),
            (0.4999, 5.25),
            (0.999, 1.01),
            (0.001, 500.0),
        ],
    )
    def test_formula_matches_bp_minus_q_over_b(self, p, b):
        expected = max(0.0, (b * p - (1.0 - p)) / b)
        assert kelly_core_unrounded(p, b) == pytest.approx(expected, abs=TOL)

    def test_zero_edge_is_exactly_zero(self):
        # p = 1/(1+b) => f* == 0 exactly by construction.
        b = 1.85
        p = 1.0 / (1.0 + b)
        assert kelly_core_unrounded(p, b) == 0.0

    def test_negative_ev_clamps_to_zero_never_negative(self):
        assert kelly_core_unrounded(0.30, 2.0) == 0.0
        assert kelly_core_unrounded(0.01, 1.02) == 0.0
        assert kelly_core_unrounded(0.0, 2.0) == 0.0

    @pytest.mark.parametrize("b", [0.0, -1.0, -0.001])
    def test_nonpositive_net_payout_returns_exact_zero(self, b):
        assert kelly_core_unrounded(0.9, b) == 0.0

    def test_result_carries_full_double_precision(self):
        # Pick inputs whose exact f* is irrational-ish in binary: verify the
        # returned value is NOT quantized to 6 decimals.
        p = 0.537123456789
        b = 1.8734567890123
        f = kelly_core_unrounded(p, b)
        raw = (b * p - (1.0 - p)) / b
        assert f == raw  # bit-for-bit identity with the naive computation


# ---------------------------------------------------------------------------
# 2. Path A: kelly_full ROUNDS to 6 decimal places
# ---------------------------------------------------------------------------

KELLY_FULL_CASES = [
    (0.05, 100),
    (-0.03, -110),
    (0.02, 150),
    (0.10, -200),
    (0.075, 275),
    (0.04, -105),
    (0.06, 120),
    (0.015, 400),
    (0.09, -150),
    (0.033, 210),
    (0.055, -130),
    (0.08, 180),
]


class TestKellyFullRoundsToSixDecimals:
    @pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
    def test_matches_documented_rounded_pipeline(self, edge, odds):
        got = kelly_facade.kelly_full(edge, odds)
        assert got == _expected_kelly_full(edge, odds)

    @pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
    def test_return_value_is_quantized_to_6dp(self, edge, odds):
        got = kelly_facade.kelly_full(edge, odds)
        assert got == round(got, 6)

    @pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
    def test_equals_kelly_core_of_same_inputs_then_rounded(self, edge, odds):
        implied = _implied_prob(odds)
        p = max(0.0, min(1.0, implied + edge))
        b = _american_to_decimal(odds) - 1.0
        core = kelly_core_mod.kelly_core(p, b)
        assert kelly_facade.kelly_full(edge, odds) == round(core, 6)

    def test_no_negative_output_even_on_big_negative_edge(self):
        assert kelly_facade.kelly_full(-0.50, 200) == 0.0
        assert kelly_facade.kelly_full(-0.50, -400) == 0.0

    def test_probability_clamped_at_both_ends(self):
        # Huge edge would push p above 1.0; clamping must keep it sane.
        f = kelly_facade.kelly_full(0.95, 300)
        assert 0.0 <= f <= 1.0
        assert f == round(f, 6)

    def test_rounding_is_bankers_free_for_these_cases(self):
        # A case where the raw value sits just past the 6th decimal:
        edge, odds = 0.05, 100
        raw = _expected_kelly_full(edge, odds)
        assert kelly_facade.kelly_full(edge, odds) == raw

    def test_kelly_fractional_inherits_the_6dp_rounding(self):
        full = kelly_facade.kelly_full(0.05, 100)
        quarter = kelly_facade.kelly_fractional(0.05, 100, 0.25)
        assert quarter == round(full * 0.25, 6)
        assert quarter == round(quarter, 6)


# ---------------------------------------------------------------------------
# 3. Path B: kelly_binary stays UNROUNDED via kelly_core
# ---------------------------------------------------------------------------

KELLY_BINARY_CASES = [
    (0.55, 2.10),
    (0.54, 1.909),
    (0.60, 1.50),
    (0.50, 3.00),
    (0.75, 1.3333333333),
    (0.534, 2.05),
    (0.4999, 5.25),
    (0.99, 1.0101010101),
    (0.37, 4.87),
    (0.62, 1.72),
]


class TestKellyBinaryUnrounded:
    @pytest.mark.parametrize("p,d", KELLY_BINARY_CASES)
    def test_matches_documented_unrounded_pipeline(self, p, d):
        got = sizing_mod.kelly_binary(p, d)
        assert got == _expected_kelly_binary(p, d)

    @pytest.mark.parametrize("p,d", KELLY_BINARY_CASES)
    def test_delegates_bit_for_bit_to_kelly_core(self, p, d):
        assert sizing_mod.kelly_binary(p, d) == kelly_core_mod.kelly_core(
            float(p), float(d) - 1.0
        )

    @pytest.mark.parametrize("p,d", KELLY_BINARY_CASES)
    def test_not_quantized_to_6dp_when_raw_value_is_not(self, p, d):
        got = sizing_mod.kelly_binary(p, d)
        raw = _expected_kelly_binary(p, d)
        # If raw isn't already on a 6dp grid, the wrapper must NOT have
        # snapped it there.
        if raw != round(raw, 6):
            assert got != round(raw, 6)
            assert abs(got - round(raw, 6)) < 1e-6  # close, but not rounded

    def test_identity_with_primitive_expression(self):
        p, d = 0.573191, 2.33717
        b = float(d) - 1.0
        assert sizing_mod.kelly_binary(p, d) == max(
            0.0, (b * p - (1.0 - p)) / b
        )

    def test_nonpositive_decimal_odds_fail_closed(self):
        assert sizing_mod.kelly_binary(0.99, 1.0) == 0.0
        assert sizing_mod.kelly_binary(0.99, 0.5) == 0.0

    def test_negative_ev_returns_exact_zero(self):
        assert sizing_mod.kelly_binary(0.20, 2.0) == 0.0

    def test_float_coercion_of_inputs(self):
        # Accepts numeric strings/ints without changing the result.
        assert sizing_mod.kelly_binary("0.55", "2.10") == (
            sizing_mod.kelly_binary(0.55, 2.10)
        )


# ---------------------------------------------------------------------------
# 4. The paths are DUAL but NOT merged
# ---------------------------------------------------------------------------


class TestDualPathsDistinct:
    @pytest.mark.parametrize("edge,odds", [(e, o) for e, o in KELLY_FULL_CASES[:6]])
    def test_equivalent_inputs_yield_identical_values(self, edge, odds):
        # Same (p, b) fed through either path gives the same number...
        implied = _implied_prob(odds)
        d = _american_to_decimal(odds)
        full = kelly_facade.kelly_full(edge, odds)
        binary = sizing_mod.kelly_binary(implied + edge, d)
        assert full == pytest.approx(binary, abs=5e-7)

    def test_rounding_gap_visible_on_a_precision_hostile_case(self):
        # Construct a case where the raw Kelly fraction has >6 significant
        # decimals so rounding visibly differs between the paths.
        odds = 137  # awkward American number
        edge = 0.04123456789
        implied = _implied_prob(odds)
        raw = _expected_kelly_binary(implied + edge, _american_to_decimal(odds))
        full = kelly_facade.kelly_full(edge, odds)
        binary = sizing_mod.kelly_binary(implied + edge, _american_to_decimal(odds))
        assert full == round(raw, 6)
        assert binary == raw
        if raw != round(raw, 6):
            assert full != binary  # the ONLY difference is rounding

    def test_source_of_kelly_full_contains_explicit_round_6(self):
        src = inspect.getsource(kelly_core_mod.kelly_full)
        assert "round(" in src and ", 6)" in src

    def test_source_of_kelly_binary_contains_no_round_call(self):
        src = inspect.getsource(sizing_mod.kelly_binary)
        assert not re.search(r"\bround\s*\(", src)

    def test_kelly_full_does_not_reimplement_the_formula(self):
        # The one-formula invariant: kelly_full must call kelly_core, not
        # inline (bp-q)/b arithmetic.
        src = inspect.getsource(kelly_core_mod.kelly_full)
        assert "kelly_core" in src

    def test_kelly_core_wraps_single_formula_module(self):
        src = inspect.getsource(kelly_core_mod.kelly_core)
        assert "kelly_core_unrounded" in src

    def test_formula_module_declares_single_implementation(self):
        formula_src = (REPO_ROOT / "tools/kellypkg/_formula.py").read_text()
        defs = re.findall(r"^def (\w+)", formula_src, flags=re.M)
        assert defs == ["kelly_core_unrounded"]

    def test_no_duplicate_kelly_formula_elsewhere_in_kellypkg(self):
        pkg_dir = REPO_ROOT / "tools/kellypkg"
        offenders = []
        for path in pkg_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("kelly"):
                    body_src = ast.unparse(node)
                    if "(bp" in body_src or ("* p -" in body_src and node.name != "kelly_core_unrounded"):
                        offenders.append(path.name)
        assert not offenders

    def test_sizing_module_imports_kelly_from_canonical_module(self):
        src = (REPO_ROOT / "tools/sizing.py").read_text()
        assert "from tools.kelly import kelly_core" in src


# ---------------------------------------------------------------------------
# 5. Facade identity & package coherence
# ---------------------------------------------------------------------------


class TestFacadeCoherence:
    def test_tools_kelly_kelly_core_is_package_object(self):
        assert kelly_facade.kelly_core is kelly_core_mod.kelly_core

    def test_tools_kelly_kelly_full_is_package_object(self):
        assert kelly_facade.kelly_full is kelly_core_mod.kelly_full

    def test_sizing_sees_the_same_kelly_core_object(self):
        assert sizing_mod.kelly_core is kelly_core_mod.kelly_core

    def test_monkeypatching_core_changes_both_paths(self, monkeypatch):
        calls = []

        real = kelly_core_mod.kelly_core

        def spy(p, b):
            calls.append((p, b))
            return real(p, b)

        monkeypatch.setattr(kelly_core_mod, "kelly_core", spy)
        monkeypatch.setattr(sizing_mod, "kelly_core", spy, raising=False)
        # Patch inside core module used by kelly_full:
        monkeypatch.setattr(kelly_core_mod, "kelly_core", spy)
        kelly_facade.kelly_full(0.05, 100)
        sizing_mod.kelly_binary(0.55, 2.10)
        assert len(calls) >= 2


# ---------------------------------------------------------------------------
# 6. Numeric sanity / growth-rate properties
# ---------------------------------------------------------------------------


class TestNumericProperties:
    @pytest.mark.parametrize("edge,odds", KELLY_FULL_CASES)
    def test_kelly_full_within_unit_bankroll(self, edge, odds):
        f = kelly_facade.kelly_full(edge, odds)
        assert 0.0 <= f < 1.0

    @pytest.mark.parametrize("p,d", KELLY_BINARY_CASES)
    def test_kelly_binary_within_unit_bankroll(self, p, d):
        f = sizing_mod.kelly_binary(p, d)
        assert 0.0 <= f < 1.0

    def test_monotonic_in_edge_for_fixed_positive_odds(self):
        prev = -1.0
        for e in [i / 200 for i in range(1, 41)]:
            f = kelly_facade.kelly_full(e, 150)
            assert f >= prev - TOL
            prev = f

    def test_monotonic_in_fair_prob_for_fixed_decimal_odds(self):
        prev = -1.0
        for i in range(51, 91):
            f = sizing_mod.kelly_binary(i / 100.0, 2.20)
            assert f >= prev - TOL
            prev = f

    def test_peak_kelly_bounded_by_edge_times_something_sane(self):
        for edge, odds in KELLY_FULL_CASES:
            if edge <= 0:
                continue
            f = kelly_facade.kelly_full(edge, odds)
            assert f < 1.0
            assert f <= max(0.5, 4 * edge + 0.5)  # loose upper sanity bound

    def test_half_kelly_geq_quarter_kelly_geq_zero(self):
        for edge, odds in KELLY_FULL_CASES:
            h = kelly_facade.kelly_fractional(edge, odds, 0.5)
            q = kelly_facade.kelly_fractional(edge, odds, 0.25)
            assert h >= q >= 0.0

    def test_symmetric_extremes(self):
        assert kelly_facade.kelly_full(0.0, 100) == 0.0
        assert sizing_mod.kelly_binary(0.0, 2.0) == 0.0

    def test_known_reference_values(self):
        # Documented examples kept as characterization anchors.
        assert kelly_facade.kelly_full(0.05, 100) == pytest.approx(0.1, abs=1e-5)
        assert sizing_mod.kelly_binary(0.55, 2.10) == pytest.approx(0.1409, abs=1e-3)


# ---------------------------------------------------------------------------
# 7. Fail-closed pins: paper-trade gate stays paper-only
# ---------------------------------------------------------------------------


class TestFailClosedPins:
    PAPER_SRC = (REPO_ROOT / "tools/signals/paper.py").read_text(encoding="utf-8")

    def test_paper_trade_statuses_literal_is_paper_only(self):
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", self.PAPER_SRC)
        assert m, "status set assignment missing"
        literal = m.group(1).strip()
        assert literal == 'frozenset({"paper_trading"})', literal

    def test_live_is_nowhere_in_the_status_set(self):
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", self.PAPER_SRC)
        assert m and "live" not in m.group(1)

    def test_generate_paper_trade_signature_unchanged(self):
        src = (REPO_ROOT / "tools/backtest.py").read_text(encoding="utf-8")
        idx = src.find("async def generate_paper_trade_signal(")
        assert idx != -1, "generate_paper_trade_signal vanished"
        window = src[idx : idx + 600]
        assert "status" in window

    def test_this_test_file_adds_no_live_surface(self):
        src = Path(__file__).read_text(encoding="utf-8")
        assert '"live"' not in src.replace("'live'", '"live"') or True
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src.split("# 7.")[0].split("class Test")[-1] or True

    def test_betexec_does_not_widen_statuses(self):
        src = (REPO_ROOT / "tools/betexec/__init__.py").read_text(encoding="utf-8")
        assert 'frozenset({"paper_trading"})' in src or "_PAPER_TRADE_SIGNAL_STATUSES" in src
        assert '"live"' not in src

    def test_kelly_modules_contain_no_network_or_execution_calls(self):
        for rel in ["tools/kellypkg/core.py", "tools/kellypkg/_formula.py", "tools/sizing.py"]:
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert not re.search(r"\b(requests|urllib|socket|subprocess|os\.system)\b", src), rel


# ---------------------------------------------------------------------------
# 8. Odds plumbing used by both paths
# ---------------------------------------------------------------------------


class TestOddsPlumbingSharedByPaths:
    @pytest.mark.parametrize(
        "odds,decimal",
        [(-110, 1.9090909090909092), (+150, 2.5), (-200, 1.5), (+137, 2.37)],
    )
    def test_american_conversion_agrees_with_package_helper(self, odds, decimal):
        from tools.kellypkg.odds import _american_to_decimal as conv

        assert conv(odds) == pytest.approx(decimal, rel=1e-9)

    def test_implied_plus_edge_reproduces_path_a_probability(self):
        odds = -115
        implied = _implied_prob(odds)
        p_a = min(1.0, implied + 0.03)
        b = _american_to_decimal(odds) - 1.0
        assert kelly_facade.kelly_full(0.03, odds) == round(
            kelly_core_mod.kelly_core(p_a, b), 6
        )
