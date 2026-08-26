"""
Autofill characterization #0062 — dual Kelly (LONG).

This module is a LARGE characterization suite pinning the two DISTINCT Kelly
paths in Callisto and the invariant that they must never be merged:

  Path A — ``tools.kellypkg.core.kelly_full`` (also re-exported via
      ``tools.kelly``): takes an *edge* + American odds, converts to
      (p, b) internally, delegates to the canonical unrounded primitive
      ``kelly_core``, then ROUNDS ITS OWN RETURN VALUE to exactly
      6 decimal places.

  Path B — ``tools.sizing.kelly_binary``: takes a fair probability +
      decimal odds, converts to (p, b), and returns ``kelly_core``'s
      output VERBATIM — full double precision, no rounding at any stage.

Both paths share the ONE formula in ``tools.kellypkg._formula
.kelly_core_unrounded``; sharing the formula is correct and pinned here.
What is forbidden is merging the *rounding behavior*: kelly_full must stay
rounded-to-6dp, kelly_binary must stay unrounded.

Fail-closed pins: this module also re-pins the paper-trade signal status
gate so a regression that arms live betting breaks these tests first.
Nothing here touches production code, starts servers, or places bets.
"""

import ast
import inspect
import math
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import sizing as sizing_mod  # noqa: E402
from tools import kelly as kelly_facade  # noqa: E402
from tools.kellypkg import core as kelly_pkg  # noqa: E402
from tools.kellypkg.core import kelly_core, kelly_full, kelly_fractional  # noqa: E402
from tools.kellypkg._formula import kelly_core_unrounded  # noqa: E402
from tools.odds_api import calculate_implied_probability  # noqa: E402

TOL = 1e-12


# ---------------------------------------------------------------------------
# Helpers — independent recomputation of each documented pipeline
# ---------------------------------------------------------------------------


def _american_to_decimal(odds: int) -> float:
    """Mirror of tools.kellypkg.odds._american_to_decimal."""
    if odds > 0:
        return 1.0 + odds / 100.0
    if odds < 0:
        return 1.0 + 100.0 / abs(odds)
    raise ValueError("american odds of 0 are invalid")


def _expected_kelly_full(edge: float, odds: int) -> float:
    """Independent recomputation of kelly_full's documented pipeline,
    including its final round(..., 6)."""
    implied = calculate_implied_probability(int(odds))
    p = max(0.0, min(1.0, implied + edge))
    b = _american_to_decimal(odds) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return round(max(0.0, (b * p - q) / b), 6)


def _expected_kelly_binary(fair_prob: float, decimal_odds: float) -> float:
    """Independent recomputation of kelly_binary's documented pipeline:
    NO rounding anywhere."""
    b = float(decimal_odds) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - fair_prob
    return max(0.0, (b * fair_prob - q) / b)


def _is_rounded_to_6dp(x: float) -> bool:
    """True iff x equals itself rounded to 6 decimal places exactly."""
    return x == round(x, 6)


# Fixtures shared across sections: (edge, american_odds) pairs where the
# exact unrounded fraction has more than 6 significant decimals, so the
# rounding split is observable.
FULL_CASES = [
    (0.05, -110),
    (0.03, 150),
    (0.01, -105),
    (0.0755, 275),
    (0.02, -150),
    (0.0431, 210),
    (0.0087, -120),
    (0.0623, 175),
]

BINARY_CASES = [
    (0.55, 2.10),
    (0.52, 1.95),
    (0.60, 2.50),
    (0.505, 2.02),
    (0.575, 2.35),
    (0.51, 1.85),
    (0.6667, 3.05),
]


# ---------------------------------------------------------------------------
# Section 1 — Path A: kelly_full rounds to 6 decimal places
# ---------------------------------------------------------------------------


class TestKellyFullRoundsToSixDecimals:
    @pytest.mark.parametrize("edge,odds", FULL_CASES)
    def test_matches_independent_pipeline(self, edge, odds):
        assert kelly_full(edge, odds) == _expected_kelly_full(edge, odds)

    @pytest.mark.parametrize("edge,odds", FULL_CASES)
    def test_result_is_exactly_6dp(self, edge, odds):
        assert _is_rounded_to_6dp(kelly_full(edge, odds))

    def test_known_value(self):
        # edge=0.05 at -110: p≈0.5639, b=1.909..., f* ≈ 0.0844...
        got = kelly_full(0.05, -110)
        assert got == pytest.approx(_expected_kelly_full(0.05, -110), abs=0)
        assert got == round(got, 6)

    def test_docstring_pins_rounding_contract(self):
        doc = inspect.getdoc(kelly_full)
        assert doc is not None
        assert "ROUNDED" in doc.upper() or "rounded" in doc.lower()
        assert "6" in doc

    def test_source_contains_round_call_at_6(self):
        src = inspect.getsource(kelly_full)
        assert "round(kelly_core(p, b), 6)" in src

    def test_no_edge_returns_zero_and_is_6dp(self):
        for odds in (-110, 150, 100):
            assert kelly_full(0.0, odds) == 0.0

    def test_negative_edge_clamps_to_zero(self):
        for e in (-0.01, -0.10, -0.5):
            assert kelly_full(e, -110) == 0.0

    def test_huge_positive_edge_clamped_p_le_one(self):
        got = kelly_full(0.9, -400)
        exp = _expected_kelly_full(0.9, -400)
        assert got == exp
        assert 0.0 <= got <= 1.0

    def test_extreme_odds_still_6dp(self):
        for odds in (5000, -5000):
            got = kelly_full(0.03, odds)
            assert _is_rounded_to_6dp(got)

    def test_return_type_float(self):
        assert isinstance(kelly_full(0.05, -110), float)

    def test_monotone_in_edge(self):
        vals = [kelly_full(e, -110) for e in (0.01, 0.02, 0.03, 0.05)]
        assert vals == sorted(vals)

    def test_never_negative_across_grid(self):
        for odds in range(-500, 501, 25):
            if odds == 0:
                continue
            for e in (-0.2, 0.0, 0.001, 0.2):
                assert kelly_full(e, odds) >= 0.0


# ---------------------------------------------------------------------------
# Section 2 — Path B: sizing.kelly_binary is UNROUNDED
# ---------------------------------------------------------------------------


class TestKellyBinaryUnrounded:
    @pytest.mark.parametrize("prob,dec", BINARY_CASES)
    def test_matches_independent_pipeline(self, prob, dec):
        assert sizing_mod.kelly_binary(prob, dec) == _expected_kelly_binary(
            prob, dec
        )

    @pytest.mark.parametrize("prob,dec", BINARY_CASES)
    def test_not_necessarily_6dp(self, prob, dec):
        got = sizing_mod.kelly_binary(prob, dec)
        # The value must equal kelly_core's raw output bit-for-bit.
        assert got == kelly_core(prob, dec - 1.0)

    def test_full_precision_survives(self):
        # Construct an input whose exact f* is NOT representable at 6dp;
        # kelly_binary must return it with extra precision intact.
        prob, dec = 0.5501234567, 2.1012345
        raw = kelly_core(prob, dec - 1.0)
        got = sizing_mod.kelly_binary(prob, dec)
        assert got == raw
        assert got != round(raw, 6) or raw == round(raw, 6)

    @staticmethod
    def _source_without_docstring(fn):
        src = inspect.getsource(fn)
        return re.sub(r'"""[\s\S]*?"""', "", src)

    def test_delegates_to_kelly_core_verbatim(self):
        body = self._source_without_docstring(sizing_mod.kelly_binary)
        assert "kelly_core(" in body
        assert "round(" not in body

    def test_docstring_pins_unrounded(self):
        doc = inspect.getdoc(sizing_mod.kelly_binary)
        assert "no rounding" in doc.lower()

    def test_negative_ev_returns_exact_zero(self):
        assert sizing_mod.kelly_binary(0.30, 2.00) == 0.0

    def test_zero_or_bad_b_returns_zero(self):
        assert sizing_mod.kelly_binary(0.55, 1.0) == 0.0
        assert sizing_mod.kelly_binary(0.55, 0.5) == 0.0

    def test_even_money_boundary(self):
        # b=1.0: f* = 2p - 1
        assert sizing_mod.kelly_binary(0.55, 2.0) == pytest.approx(0.10, abs=TOL)

    def test_return_type_float(self):
        assert isinstance(sizing_mod.kelly_binary(0.55, 2.1), float)

    def test_monotone_in_prob(self):
        vals = [sizing_mod.kelly_binary(p, 2.0) for p in (0.51, 0.53, 0.57)]
        assert vals == sorted(vals)


# ---------------------------------------------------------------------------
# Section 3 — The ONE formula: delegation graph pinned
# ---------------------------------------------------------------------------


class TestSingleFormulaDelegation:
    def test_kelly_core_is_formula_passthrough(self):
        for p, b in [(0.55, 1.1), (0.5, 2.0), (0.9, 0.5)]:
            assert kelly_core(p, b) == kelly_core_unrounded(p, b)

    def test_kelly_core_has_no_round_call_in_body(self):
        tree = ast.parse(inspect.getsource(kelly_core))
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "round"
        ]
        assert calls == []

    def test_formula_module_has_no_round_call_at_all(self):
        formula_src = (
            REPO_ROOT / "tools/kellypkg/_formula.py"
        ).read_text()
        tree = ast.parse(formula_src)
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "round"
        ]
        assert calls == []

    def test_core_module_rounds_only_inside_kelly_full_family(self):
        core_src = (REPO_ROOT / "tools/kellypkg/core.py").read_text()
        tree = ast.parse(core_src)
        rounding_fns = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if any(
                    isinstance(n, ast.Call)
                    and getattr(n.func, "id", "") == "round"
                    for n in ast.walk(node)
                ):
                    rounding_fns.append(node.name)
        assert set(rounding_fns) == {"kelly_full", "kelly_fractional"}

    def test_sizing_imports_canonical_core(self):
        ssrc = (REPO_ROOT / "tools/sizing.py").read_text()
        assert "from tools.kelly import kelly_core" in ssrc

    def test_facade_reexports_same_objects(self):
        assert kelly_facade.kelly_full is kelly_pkg.kelly_full
        assert kelly_facade.kelly_core is kelly_pkg.kelly_core

    def test_only_one_formula_definition_in_repo(self):
        pkg_dir = REPO_ROOT / "tools/kellypkg"
        hits = []
        for f in pkg_dir.glob("*.py"):
            text = f.read_text()
            if re.search(r"\(b \* p - q\) / b|\(b\*p-q\)/b", text):
                hits.append(f.name)
        assert hits == ["_formula.py"]

    def test_dynamic_also_routes_through_kelly_full(self):
        dsrc = (REPO_ROOT / "tools/kellypkg/dynamic.py").read_text()
        assert "from tools.kellypkg.core import kelly_full" in dsrc
        assert '"kelly_full": round(kelly_full(edge, odds), 6)' in dsrc


# ---------------------------------------------------------------------------
# Section 4 — Paths must never be MERGED (differential characterization)
# ---------------------------------------------------------------------------


class TestPathsNeverMerge:
    def test_rounding_split_observable(self):
        # Find inputs where full-precision and 6dp differ, then confirm the
        # two entry points actually disagree on those inputs' precision.
        diffs = 0
        for prob, dec in BINARY_CASES:
            raw = kelly_core(prob, dec - 1.0)
            if raw != round(raw, 6):
                diffs += 1
                assert sizing_mod.kelly_binary(prob, dec) == raw
        assert diffs > 0, "fixture set no longer exercises the split"

    def test_kelly_full_output_always_equals_its_own_6dp(self):
        for edge, odds in FULL_CASES:
            v = kelly_full(edge, odds)
            assert v == round(v, 6)

    def test_binary_output_may_violate_6dp(self):
        violators = [
            (p, d)
            for p, d in BINARY_CASES
            if sizing_mod.kelly_binary(p, d)
            != round(sizing_mod.kelly_binary(p, d), 6)
        ]
        assert violators, "no binary fixture violates 6dp; split untestable"

    def test_equivalent_input_agrees_to_within_half_ulp_of_6dp(self):
        # Same bet expressed through both paths must agree up to kelly_full's
        # documented rounding tolerance.
        edge, odds = 0.05, -110
        implied = calculate_implied_probability(odds)
        via_full = kelly_full(edge, odds)
        via_binary = sizing_mod.kelly_binary(implied + edge, _american_to_decimal(odds))
        assert via_full == pytest.approx(via_binary, abs=5e-7)

    def test_fractional_kelly_derives_from_full_and_stays_6dp(self):
        for frac in (0.25, 0.5):
            got = kelly_fractional(0.05, -110, frac)
            exp = round(kelly_full(0.05, -110) * frac, 6)
            assert got == exp
            assert got == round(got, 6)

    def test_no_cross_contamination_of_rounding(self):
        # Mutating nothing: just prove the two paths are distinct callables
        # and that kelly_binary's executable body never rounds.
        assert sizing_mod.kelly_binary.__code__ is not kelly_full.__code__
        body = re.sub(
            r'"""[\s\S]*?"""', "", inspect.getsource(sizing_mod.kelly_binary)
        )
        assert "round(" not in body


# ---------------------------------------------------------------------------
# Section 5 — Formula sanity (shared math both paths rely on)
# ---------------------------------------------------------------------------


class TestFormulaSanity:
    @pytest.mark.parametrize(
        "p,b,expected",
        [
            (0.55, 1.0, 0.10),
            (0.5, 2.0, 0.25),
            (0.6, 1.5, 0.33333333333333326),
            (0.4, 3.0, 0.20),
        ],
    )
    def test_closed_form_values(self, p, b, expected):
        assert kelly_core_unrounded(p, b) == pytest.approx(expected, abs=1e-9)

    def test_b_zero_guard(self):
        assert kelly_core_unrounded(0.9, 0.0) == 0.0

    def test_b_negative_guard(self):
        assert kelly_core_unrounded(0.9, -1.0) == 0.0

    def test_minus_ev_clamped(self):
        assert kelly_core_unrounded(0.2, 1.0) == 0.0

    def test_breakeven_p_gives_zero(self):
        assert kelly_core_unrounded(0.5, 1.0) == pytest.approx(0.0, abs=TOL)

    def test_symmetry_property(self):
        # f* = (b*p - q)/b with b=1 is 2p - 1, linear in p.
        f_lo = kelly_core_unrounded(0.52, 1.0)
        f_hi = kelly_core_unrounded(0.62, 1.0)
        assert f_lo == pytest.approx(0.04, abs=1e-9)
        assert f_hi == pytest.approx(0.24, abs=1e-9)
        assert f_hi - f_lo == pytest.approx(0.20, abs=1e-9)


# ---------------------------------------------------------------------------
# Section 6 — Fail-closed pins: paper-only gate stays shut
# ---------------------------------------------------------------------------

PAPER_SRC = (REPO_ROOT / "tools/signals/paper.py").read_text(encoding="utf-8")
BACKTEST_SRC = (REPO_ROOT / "tools/backtest.py").read_text(encoding="utf-8")

class TestPaperOnlyGatePinned:
    def test_statuses_literal_unchanged(self):
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", PAPER_SRC)
        assert m
        assert m.group(1).strip() == 'frozenset({"paper_trading"})'

    def test_live_absent_from_statuses(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        assert "live" not in {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}

    def test_generate_paper_trade_signal_not_widened(self):
        m = re.search(
            r"async def generate_paper_trade_signal\(.*?(?=\n    (?:async )?def |\nclass |\Z)",
            BACKTEST_SRC,
            re.S,
        )
        assert m, "generate_paper_trade_signal missing"
        body = m.group(0)
        assert "== 'live'" not in body and '== "live"' not in body
        assert "reject_non_paper(" in body
        # The gate helper itself must reject everything outside the set.
        from tools.signals.paper import allowed_paper_statuses, reject_non_paper

        assert reject_non_paper("live") is True
        assert reject_non_paper("paper_trading") is False
        assert reject_non_paper("") is True
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_backtest_reuses_paper_module_set(self):
        assert (
            "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES"
            in BACKTEST_SRC
        )
        assert re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset", BACKTEST_SRC) is None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
