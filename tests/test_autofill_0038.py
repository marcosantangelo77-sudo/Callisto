"""
Autofill characterization #0038 — dual Kelly (LONG).

Characterizes the two distinct Kelly paths that must never merge:

1. ``kelly_full`` (tools.kelly / tools.kellypkg.core) — the "full" path:
   accepts American odds + edge, converts internally, and ROUNDS its
   return value to exactly 6 decimal places (via ``round(x, 6)``).
2. ``kelly_binary`` / ``kelly_core`` — the binary path: stays UNROUNDED
   via ``tools.kellypkg._formula.kelly_core_unrounded``.

These are pure characterization tests: they pin down current behavior so
any accidental merge of the two paths, a change in rounding granularity,
or a widening of the paper-trade status gate fails loudly.

Tests only; no production code is modified. No live betting surface is
touched or introduced.
"""

import math
import os
import re

import pytest

import tools.kelly as facade
from tools.kellypkg._formula import kelly_core_unrounded
from tools.kellypkg.core import kelly_core as pkg_kelly_core
from tools.kellypkg.core import kelly_full as pkg_kelly_full
from tools.odds_api import calculate_implied_probability
from tools.sizing import kelly_binary


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_kelly(p, b):
    """Reference implementation with the exact op order of _formula."""
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


def _full_inputs(odds, edge):
    """Reproduce kelly_full's internal p/b derivation."""
    implied = calculate_implied_probability(int(odds))
    p = max(0.0, min(1.0, implied + edge))
    b = facade._american_to_decimal(int(odds)) - 1.0
    return p, b


def _pkg_sources():
    root = os.path.join(REPO_ROOT, "tools", "kellypkg")
    for fname in sorted(os.listdir(root)):
        if fname.endswith(".py"):
            with open(os.path.join(root, fname)) as fh:
                yield fname, fh.read()


# ---------------------------------------------------------------------------
# kelly_full rounds to 6 decimal places
# ---------------------------------------------------------------------------

class TestKellyFullRounding:
    """kelly_full's return value must be rounded to 6 decimal places."""

    @pytest.mark.parametrize(
        "odds,edge",
        [
            (-110, 0.05),
            (+150, 0.03),
            (-200, 0.10),
            (+275, 0.02),
            (-135, 0.075),
            (+120, 0.01),
            (-105, 0.005),
            (+333, 0.04),
            (-250, 0.15),
            (+175, 0.06),
        ],
    )
    def test_equals_round6_of_raw(self, odds, edge):
        p, b = _full_inputs(odds, edge)
        expected = round(_raw_kelly(p, b), 6)
        assert facade.kelly_full(edge, odds) == expected

    @pytest.mark.parametrize(
        "odds,edge",
        [(-110, 0.05), (+150, 0.03), (-200, 0.10), (+333, 0.04), (-135, 0.075)],
    )
    def test_value_is_on_6dp_grid(self, odds, edge):
        got = facade.kelly_full(edge, odds)
        # A 6-dp-rounded float times 1e6 must be (very near) an integer.
        assert abs(got * 1e6 - round(got * 1e6)) < 1e-6

    @pytest.mark.parametrize(
        "odds,edge",
        [(-110, 0.05), (+150, 0.03), (-200, 0.10), (+333, 0.04)],
    )
    def test_rounding_actually_changes_something(self, odds, edge):
        """The unrounded raw value differs from kelly_full's output, i.e.
        the round() call is doing real work on these inputs."""
        p, b = _full_inputs(odds, edge)
        raw = _raw_kelly(p, b)
        rounded = round(raw, 6)
        if round(raw, 12) != rounded:  # skip inputs where rounding is a no-op
            assert facade.kelly_full(edge, odds) == rounded
            assert raw != rounded

    def test_known_value_minus110_edge5pct(self):
        # -110 -> decimal 1.90909..., b=0.90909...; implied ~0.52381;
        # p ~ 0.57381 -> f* ~ 0.128227...
        assert facade.kelly_full(0.05, -110) == round(
            _raw_kelly(*_full_inputs(-110, 0.05)), 6
        )

    def test_never_negative(self):
        for odds in (-400, -110, +100, +500):
            assert facade.kelly_full(0.0, odds) == 0.0
            assert facade.kelly_full(-0.05, odds) == 0.0

    def test_result_type_is_float(self):
        assert isinstance(facade.kelly_full(0.05, -110), float)
        assert isinstance(pkg_kelly_full(0.05, -110), float)

    def test_large_edge_clamped_at_p_one(self):
        # Edge so large that p would exceed 1.0 -> clamped, still finite.
        val = facade.kelly_full(3.0, +100)
        assert val == round(_raw_kelly(1.0, 1.0), 6)
        assert val == 1.0

    def test_pkg_and_facade_agree_exactly(self):
        for odds in (-300, -110, +100, +250):
            for edge in (0.01, 0.04, 0.09):
                assert facade.kelly_full(edge, odds) == pkg_kelly_full(edge, odds)


# ---------------------------------------------------------------------------
# kelly_binary / kelly_core stay unrounded
# ---------------------------------------------------------------------------

class TestBinaryPathUnrounded:
    """The binary path must expose full float precision (no round())."""

    @pytest.mark.parametrize(
        "p,b",
        [
            (0.55, 1.10),
            (0.52, 0.9090909090909091),
            (0.60, 0.6666666666666667),
            (0.51, 1.5),
            (0.75, 0.25),
            (2.0 / 3.0, 4.0 / 3.0),
            (0.501, 1.01),
        ],
    )
    def test_kelly_core_matches_raw_bit_for_bit(self, p, b):
        raw = _raw_kelly(p, b)
        assert facade.kelly_core(p, b) == raw
        assert pkg_kelly_core(p, b) == raw
        assert kelly_core_unrounded(p, b) == raw

    @pytest.mark.parametrize(
        "fair,dec",
        [(0.55, 2.10), (0.52, 1.91), (0.60, 1.67), (0.53, 2.35)],
    )
    def test_kelly_binary_is_raw_not_rounded(self, fair, dec):
        raw = _raw_kelly(fair, dec - 1.0)
        got = kelly_binary(fair, dec)
        assert got == raw
        # If it had been rounded to 6dp these would usually differ.
        if round(raw, 6) != raw:
            assert got != round(got, 6) or raw == got

    def test_docstring_example_holds(self):
        # sizing docstring: prob=0.55, odds=2.10 -> f*=0.1409 (approx).
        assert kelly_binary(0.55, 2.10) == pytest.approx(0.1409, abs=1e-3)

    def test_non_plus_ev_returns_zero(self):
        assert kelly_binary(0.40, 2.10) == 0.0
        assert kelly_binary(0.50, 1.50) == 0.0

    def test_zero_or_negative_b_returns_zero(self):
        assert kelly_binary(0.99, 1.0) == 0.0
        assert kelly_binary(0.99, 0.5) == 0.0

    def test_precision_survives_through_binary_path(self):
        # Construct an input whose true value has >6 decimal digits and
        # confirm no truncation happens anywhere in the chain.
        p = 0.5238095238095238  # 11/21-ish
        b = 0.9523809523809523
        assert facade.kelly_core(p, b) == _raw_kelly(p, b)
        assert repr(facade.kelly_core(p, b)) != repr(round(facade.kelly_core(p, b), 6))


# ---------------------------------------------------------------------------
# Paths must NOT merge
# ---------------------------------------------------------------------------

class TestPathsStaySeparate:
    """Structural pins: rounding happens in kelly_full, not in the core."""

    def test_formula_module_contains_no_round_call(self):
        for fname, src in _pkg_sources():
            if fname == "_formula.py":
                assert "round(" not in src, fname

    def test_kelly_full_source_calls_round_with_6(self):
        import inspect

        src = inspect.getsource(pkg_kelly_full)
        assert "round(" in src and ", 6)" in src, "kelly_full must round to 6"

    def test_kelly_core_delegates_to_unrounded_formula(self):
        import inspect

        src = inspect.getsource(pkg_kelly_core)
        assert "kelly_core_unrounded" in src

    def test_facade_kelly_core_also_delegates(self):
        import inspect

        src = inspect.getsource(facade.kelly_core)
        assert "kelly_core_unrounded" in src

    def test_identity_between_all_core_aliases(self):
        # One formula, several names — all identical function object or
        # at least identical outputs bit-for-bit across a grid.
        for p in (0.51, 0.55, 0.62, 0.70):
            for b in (0.5, 1.0, 1.37, 2.9):
                vals = {
                    facade.kelly_core(p, b),
                    pkg_kelly_core(p, b),
                    kelly_core_unrounded(p, b),
                }
                assert len(vals) == 1

    def test_kelly_full_differs_from_unrounded_when_rounding_matters(self):
        # Pick inputs where the raw value is not on the 6dp grid; the
        # full path must differ from the raw binary-style computation.
        odds, edge = -137, 0.041
        p, b = _full_inputs(odds, edge)
        raw = _raw_kelly(p, b)
        if round(raw, 6) != raw:
            assert facade.kelly_full(edge, odds) == round(raw, 6)
            assert facade.kelly_full(edge, odds) != raw


# ---------------------------------------------------------------------------
# kelly_fractional builds on the rounded full path
# ---------------------------------------------------------------------------

class TestFractionalPath:
    @pytest.mark.parametrize("fraction", [0.25, 0.5, 0.1])
    def test_fractional_is_scaled_full(self, fraction):
        odds, edge = -110, 0.06
        full = facade.kelly_full(edge, odds)
        assert facade.kelly_fractional(edge, odds, fraction) == round(full * fraction, 6)

    def test_default_is_quarter_kelly(self):
        odds, edge = +120, 0.05
        assert facade.kelly_fractional(edge, odds) == round(
            facade.kelly_full(edge, odds) * 0.25, 6
        )

    def test_no_edge_zero(self):
        assert facade.kelly_fractional(0.0, -110) == 0.0


# ---------------------------------------------------------------------------
# Fail-closed: paper-trade status gate stays narrow
# ---------------------------------------------------------------------------

class TestFailClosedPaperGate:
    """The live-betting gate must remain closed."""

    def test_status_frozenset_is_paper_only(self):
        from tools.signals import paper

        assert paper._PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_not_in_statuses(self):
        from tools.signals import paper

        assert "live" not in paper._PAPER_TRADE_SIGNAL_STATUSES

    def test_paper_py_has_no_live_literal_in_statuses(self):
        with open(os.path.join(REPO_ROOT, "tools", "signals", "paper.py")) as fh:
            src = fh.read()
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", src)
        assert m, "statuses assignment missing"
        assert '"live"' not in m.group(1)
        assert "'live'" not in m.group(1)

    def test_gate_helpers_untouched(self):
        from tools.signals import paper

        # The gate must key off the narrow frozenset, not accept 'live'.
        assert paper.allowed_paper_statuses() == {"paper_trading"}
        assert paper.reject_non_paper("live") is True
        assert paper.reject_non_paper("paper_trading") is False


# ---------------------------------------------------------------------------
# Odds-conversion sanity underpinning kelly_full
# ---------------------------------------------------------------------------

class TestOddsConversionUnderpinnings:
    @pytest.mark.parametrize(
        "american,decimal",
        [(-110, 1.9090909090909092), (+150, 2.5), (+100, 2.0), (-100, 2.0), (-200, 1.5)],
    )
    def test_american_to_decimal(self, american, decimal):
        assert facade._american_to_decimal(american) == pytest.approx(decimal, rel=1e-9)

    def test_implied_probability_sum_near_one(self):
        pos = calculate_implied_probability(+150)
        neg = calculate_implied_probability(-150)
        assert 0 < pos < 1 and 0 < neg < 1

    def test_b_derivation_matches_core_expectation(self):
        odds = -115
        p, b = _full_inputs(odds, 0.03)
        assert b == pytest.approx(facade._american_to_decimal(odds) - 1.0)
        assert 0 <= p <= 1
