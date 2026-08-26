"""
Autofill characterization #0030 — dual Kelly (LONG).

Characterizes the two distinct Kelly paths that must never merge:

1. ``kelly_full`` (tools.kelly / tools.kellypkg.core) — the "full" path:
   accepts American odds + edge, converts internally, and ROUNDS its
   return value to 6 decimal places.
2. ``kelly_core`` (and ``tools.sizing.kelly_binary`` built on it) —
   the binary path: stays UNROUNDED via tools.kellypkg._formula.

These tests are pure characterization: they pin down current behavior
(rounding granularity, clamping, delegation identity, zero/negative-edge
folding, parity between facade and package) so any accidental merge of
the paths or a change in rounding behavior fails loudly.

No production code is modified. No live betting surface is touched or
introduced; a final section fail-closed pins the paper-trade status gate.
"""

import ast
import math
import os

import pytest

import tools.kelly as facade
from tools.odds_api import calculate_implied_probability


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
# 1. kelly_full rounds to 6 decimal places — many parameterized cases
# ---------------------------------------------------------------------------

ODDS_EDGE_GRID = [
    (0.05, -110),
    (0.03, -110),
    (0.01, -105),
    (0.02, 150),
    (0.10, 200),
    (0.04, -120),
    (0.075, 175),
    (0.005, -102),
    (0.06, -140),
    (0.08, 300),
    (0.12, 400),
    (0.001, -101),
    (0.25, 1000),
    (0.15, -200),
    (0.09, 120),
    (0.033, 105),
]


@pytest.mark.parametrize("edge,odds", ODDS_EDGE_GRID)
def test_full_rounds_to_six_decimals_grid(edge, odds):
    val = facade.kelly_full(edge, odds)
    assert val == round(val, 6)


@pytest.mark.parametrize("edge,odds", ODDS_EDGE_GRID)
def test_full_is_exact_multiple_of_1e_6_grid(edge, odds):
    scaled = facade.kelly_full(edge, odds) * 1_000_000
    assert abs(scaled - round(scaled)) < 1e-6


@pytest.mark.parametrize("edge,odds", ODDS_EDGE_GRID)
def test_full_equals_round_of_core_formula_grid(edge, odds):
    p, b = _full_inputs(odds, edge)
    expected = round(facade.kelly_core(p, b), 6)
    assert facade.kelly_full(edge, odds) == expected


@pytest.mark.parametrize("edge,odds", ODDS_EDGE_GRID)
def test_full_matches_reference_impl_grid(edge, odds):
    p, b = _full_inputs(odds, edge)
    assert facade.kelly_full(edge, odds) == round(_raw_kelly(p, b), 6)


@pytest.mark.parametrize("edge,odds", ODDS_EDGE_GRID)
def test_full_never_negative_and_bounded_grid(edge, odds):
    val = facade.kelly_full(edge, odds)
    assert 0.0 <= val <= 1.0


def test_full_rounding_changes_value_for_high_precision_edge():
    """A deliberately awkward edge whose raw Kelly has >6 significant digits,
    proving the rounding pass actually bites (not vacuously true)."""
    edge = 0.0412371
    odds = 137
    p, b = _full_inputs(odds, edge)
    raw = _raw_kelly(p, b)
    assert raw != round(raw, 6)
    assert facade.kelly_full(edge, odds) == round(raw, 6)


def test_full_output_has_at_most_six_decimal_digits():
    for edge, odds in ODDS_EDGE_GRID[:8]:
        s = repr(facade.kelly_full(edge, odds))
        if "." in s:
            assert len(s.split(".")[1].rstrip("0")) <= 6


# ---------------------------------------------------------------------------
# 2. kelly_core stays unrounded via kelly_core_unrounded
# ---------------------------------------------------------------------------

CORE_CASES = [
    (0.55, 0.909090909090909),
    (0.60, 1.5),
    (0.52, 0.9523809523809523),
    (0.75, 3.0),
    (0.51, 0.5),
    (0.6667, 2.0),
    (0.40, 1.2),
    (0.999, 0.01),
]


@pytest.mark.parametrize("p,b", CORE_CASES)
def test_core_is_bit_exact_with_raw_formula(p, b):
    assert facade.kelly_core(p, b) == _raw_kelly(p, b)


@pytest.mark.parametrize("p,b", CORE_CASES)
def test_core_is_not_equal_to_six_dp_rounding_when_it_differs(p, b):
    """If raw != round(raw, 6), core must equal the RAW value — i.e. no
    rounding pass may have been applied to kelly_core."""
    raw = _raw_kelly(p, b)
    got = facade.kelly_core(p, b)
    if raw != round(raw, 6):
        assert got == raw
        assert got != round(raw, 6)


@pytest.mark.parametrize("p,b", CORE_CASES)
def test_pkg_core_matches_facade_core(p, b):
    from tools.kellypkg.core import kelly_core as pkg_core

    assert pkg_core(p, b) == facade.kelly_core(p, b)


@pytest.mark.parametrize("p,b", CORE_CASES)
def test_formula_module_direct_call_matches_core(p, b):
    from tools.kellypkg._formula import kelly_core_unrounded

    assert kelly_core_unrounded(p, b) == facade.kelly_core(p, b)


def test_core_handles_non_positive_b_by_zeroing():
    assert facade.kelly_core(0.9, 0.0) == 0.0
    assert facade.kelly_core(0.9, -1.0) == 0.0


def test_core_folds_neg_ev_bets_to_zero():
    # p=0.4, b=1.0 -> (0.4 - 0.6)/1 < 0 -> 0.0
    assert facade.kelly_core(0.4, 1.0) == 0.0
    assert facade.kelly_core(0.0, 5.0) == 0.0


# ---------------------------------------------------------------------------
# 3. The two paths do NOT merge — structural identity pins
# ---------------------------------------------------------------------------

def test_paths_are_distinct_code_objects():
    import tools.sizing as sizing
    from tools.kellypkg.core import kelly_core as pkg_core

    assert facade.kelly_full.__code__ is not pkg_core.__code__
    assert facade.kelly_full.__code__ is not facade.kelly_core.__code__
    assert sizing.kelly_binary.__code__ is not facade.kelly_full.__code__
    assert sizing.kelly_binary.__code__ is not pkg_core.__code__


def test_facade_and_package_share_each_path_once():
    from tools.kellypkg.core import kelly_core as pkg_core
    from tools.kellypkg.core import kelly_full as pkg_full

    assert facade.kelly_core is pkg_core
    assert facade.kelly_full is pkg_full


def test_sizing_kelly_binary_delegates_to_shared_core(monkeypatch):
    import tools.sizing as sizing
    from tools.kellypkg.core import kelly_core as pkg_core

    calls = []
    real = sizing.kelly_core

    def spy(p, b):
        calls.append((p, b))
        return real(p, b)

    monkeypatch.setattr(sizing, "kelly_core", spy)
    decimal_odds = 1.909090909090909
    out = sizing.kelly_binary(0.55, decimal_odds)
    assert calls == [(0.55, decimal_odds - 1.0)]
    assert out == pkg_core(0.55, decimal_odds - 1.0)


def test_sizing_kelly_binary_output_is_unrounded():
    import tools.sizing as sizing

    p, decimal_odds = 0.55, 1.909090909090909
    out = sizing.kelly_binary(p, decimal_odds)
    assert out == _raw_kelly(p, decimal_odds - 1.0)
    # And specifically NOT the 6dp-rounded variant whenever they differ.
    rounded = round(out, 6)
    if rounded != out:
        assert out != rounded


def test_only_one_round_site_in_kelly_full_source():
    """kellypkg/core.py must contain exactly one `round(` call, inside
    kelly_full, applied to kelly_core's result."""
    src_path = os.path.join(REPO_ROOT, "tools", "kellypkg", "core.py")
    with open(src_path) as fh:
        src = fh.read()
    tree = ast.parse(src)
    round_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "round"
    ]
    # Exactly ONE rounding site may consume kelly_core's result: the 6dp
    # rounding inside kelly_full. (kelly_fractional re-rounds its own scaled
    # value, never kelly_core's output directly.)
    core_roundings = [
        n for n in round_calls
        if n.args
        and isinstance(n.args[0], ast.Call)
        and getattr(n.args[0].func, "id", None) == "kelly_core"
    ]
    assert len(core_roundings) == 1
    node = core_roundings[0]
    # ...and the second positional of that round() is literally 6.
    ndigits = node.args[1]
    assert isinstance(ndigits, ast.Constant) and ndigits.value == 6


def test_formula_module_contains_no_rounding_at_all():
    src_path = os.path.join(REPO_ROOT, "tools", "kellypkg", "_formula.py")
    with open(src_path) as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        assert not (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) in ("round", "format")
        ), "_formula.py must stay free of rounding/formatting"


def test_no_call_site_rounds_kelly_core_anywhere_in_package():
    for fname, src in _pkg_sources():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "round"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id == "kelly_core":
                        pytest.fail(f"{fname}: kelly_core must stay unrounded")


def test_sizing_does_not_import_kelly_full():
    """kelly_binary's path must be fed by kelly_core only — sizing.py must
    not import the rounded full path at all."""
    import ast as _ast

    src_path = os.path.join(REPO_ROOT, "tools", "sizing.py")
    with open(src_path) as fh:
        src = fh.read()
    tree = _ast.parse(src)
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module:
            for a in node.names:
                imported.add(a.asname or a.name)
        elif isinstance(node, _ast.Import):
            for a in node.names:
                imported.add((a.asname or a.name).split(".")[0])
    assert "kelly_full" not in imported


# ---------------------------------------------------------------------------
# 4. Behavioral parity / characterization of kelly_full edges
# ---------------------------------------------------------------------------

def test_full_zero_or_negative_edge_returns_zero():
    assert facade.kelly_full(0.0, -110) == 0.0
    assert facade.kelly_full(-0.05, -110) == 0.0
    assert facade.kelly_full(-1.0, 500) == 0.0


def test_full_clamps_true_probability_into_unit_interval():
    # Massive positive edge on short odds would push p above 1 without clamp;
    # with clamp, f* stays sane and non-negative.
    huge = facade.kelly_full(0.99, -400)
    assert 0.0 <= huge <= 1.0
    p, b = _full_inputs(-400, 0.99)
    assert p == 1.0  # clamped
    assert huge == round(_raw_kelly(p, b), 6)


@pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 1.0])
def test_fractional_is_scaled_rounded_full(fraction):
    full = facade.kelly_full(0.05, -110)
    frac = facade.kelly_fractional(0.05, -110, fraction=fraction)
    assert frac == round(full * fraction, 6)
    assert frac == round(frac, 6)  # fractional path also 6dp-rounded


def test_fractional_default_is_quarter_kelly():
    full = facade.kelly_full(0.05, -110)
    assert facade.kelly_fractional(0.05, -110) == round(full * 0.25, 6)


def test_full_monotone_in_edge_for_fixed_odds():
    prev = -1.0
    for e in [i / 200 for i in range(1, 21)]:
        v = facade.kelly_full(e, -110)
        assert v >= prev
        prev = v


def test_full_agrees_with_core_up_to_rounding_on_a_dense_grid():
    diffs = []
    for i in range(50):
        edge = 0.001 * (i + 1)
        odds = -110
        p, b = _full_inputs(odds, edge)
        core_val = facade.kelly_core(p, b)
        full_val = facade.kelly_full(edge, odds)
        assert abs(full_val - core_val) <= 5e-7 + 1e-12
        diffs.append(full_val == core_val)
    # At least some grid points must actually differ pre-rounding, else the
    # dual-path distinction would be vacuous on this grid.
    assert not all(diffs)


def test_full_accepts_numeric_string_odds_like_int_cast():
    # kelly_full int()-casts odds; verify float-typed odds behave identically.
    assert facade.kelly_full(0.05, -110.0) == facade.kelly_full(0.05, -110)
    assert facade.kelly_full(0.03, 150.0) == facade.kelly_full(0.03, 150)


# ---------------------------------------------------------------------------
# 5. Facade surface preserved (characterization)
# ---------------------------------------------------------------------------

def test_facade_docstring_pins_the_dual_contract():
    doc = facade.__doc__ or ""
    assert "6 decimal" in doc
    assert "unrounded" in doc.lower()


def test_formula_module_is_private_and_single():
    formula_files = []
    for fname, src in _pkg_sources():
        if "def kelly_core_unrounded(" in src:
            formula_files.append(fname)
    assert formula_files == ["_formula.py"]


def test_constants_tiers_still_exposed_via_facade():
    assert facade.AGP_TIER_MULTIPLIERS["UNVERIFIED"] == 0.00
    assert facade._american_to_decimal(-110) == pytest.approx(1.9090909090)


# ---------------------------------------------------------------------------
# 6. Fail-closed: no live betting surface introduced
# ---------------------------------------------------------------------------

def test_paper_trade_statuses_literal_excludes_live():
    """Fail-closed pin via source scan of the gate definition."""
    import re

    path = os.path.join(REPO_ROOT, "tools", "signals", "paper.py")
    with open(path) as fh:
        src = fh.read()
    m = re.search(
        r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\{([^}]*)\}\)", src
    )
    assert m, "_PAPER_TRADE_SIGNAL_STATUSES definition must remain discoverable"
    statuses = {
        s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()
    }
    assert statuses == {"paper_trading"}


def test_paper_trade_gate_statuses_exclude_live():
    """Fail-closed pin: only paper_trading may pass the paper-signal gate."""
    from tools.signals.paper import (
        _PAPER_TRADE_SIGNAL_STATUSES,
        allowed_paper_statuses,
        reject_non_paper,
    )

    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
    assert "live" not in allowed_paper_statuses()
    # Gate rejects everything else, fail-closed.
    assert reject_non_paper("live") is True
    assert reject_non_paper("paper_trading") is False
    assert reject_non_paper(None) is True


def test_generate_paper_trade_signature_untouched():
    """BacktestEngine.generate_paper_trade_signal must keep its narrow shape:
    (hypothesis_id, live_odds) — no widened status parameter."""
    import inspect

    from tools.backtest import BacktestEngine

    sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
    params = list(sig.parameters)
    assert params[:3] == ["self", "hypothesis_id", "live_odds"]
    assert "status" not in params


def test_kellypkg_sources_contain_no_live_literal():
    for fname, src in _pkg_sources():
        assert '"live"' not in src and "'live'" not in src, fname
