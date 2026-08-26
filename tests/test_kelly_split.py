"""
Tests for the tools.kelly -> tools.kellypkg split.

Contract under test:
1. ``tools.kelly`` is a facade re-exporting EVERY public name from
   ``tools.kellypkg`` (and the private helpers other modules rely on).
2. The two Kelly paths remain distinct and unmerged:
   - ``kelly_full`` (and everything built on it) ROUNDS to 6 decimal places.
   - ``kelly_core`` stays UNROUNDED — ``tools.sizing.kelly_binary`` delegates
     to it and must keep full precision.
3. Behavior parity: every public function still produces identical results.
4. No live betting surface is introduced by the split.
"""

import ast
import math
import os

import pytest

import tools.kelly as facade
import tools.kellypkg


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 1. Facade completeness
# ---------------------------------------------------------------------------

def _public_names(module):
    public = set(getattr(module, "__all__", []))
    if not public:
        public = {n for n in dir(module) if not n.startswith("_")}
    return public


def test_facade_reexports_all_public_package_names():
    pkg_public = _public_names(tools.kellypkg)
    assert pkg_public, "kellypkg must expose public names"
    missing = pkg_public - set(dir(facade))
    assert not missing, f"facade missing public names: {sorted(missing)}"


@pytest.mark.parametrize(
    "name",
    [
        "AGP_TIER_MULTIPLIERS",
        "LINE_MOVEMENT_PROFILES",
        "MARKET_CLV_DECAY",
        "kelly_core",
        "kelly_full",
        "kelly_fractional",
        "kelly_dynamic",
        "kelly_portfolio",
        "ruin_probability",
        "timing_value",
        "calculate_units",
        # private helpers consumed elsewhere in the codebase / tests
        "_american_to_decimal",
        "_confidence_tier_from_score",
        "_simulate_ruin",
        "_expected_bets_to_ruin_neg_ev",
        "_DEFAULT_MOVEMENT_PROFILE",
    ],
)
def test_facade_exposes_name(name):
    assert hasattr(facade, name), f"tools.kelly lost {name!r} in the split"


def test_facade_names_are_the_package_objects_not_copies():
    # Re-export must be identity, so monkeypatching/state stays coherent.
    import tools.kellypkg as kp

    assert facade.kelly_full is kp.kelly_full
    assert facade.AGP_TIER_MULTIPLIERS is kp.AGP_TIER_MULTIPLIERS
    assert facade.LINE_MOVEMENT_PROFILES is kp.LINE_MOVEMENT_PROFILES


def test_facade_is_a_module_not_a_shadowing_directory():
    # Guard against recreating the split as tools/kelly/ which would shadow
    # this facade module on the import path.
    assert not os.path.isdir(os.path.join(REPO_ROOT, "tools", "kelly"))
    assert os.path.isfile(os.path.join(REPO_ROOT, "tools", "kelly.py"))
    assert os.path.isdir(os.path.join(REPO_ROOT, "tools", "kellypkg"))


# ---------------------------------------------------------------------------
# 2. Dual Kelly invariant (AST-pinned)
# ---------------------------------------------------------------------------

def _pkg_sources():
    root = os.path.join(REPO_ROOT, "tools", "kellypkg")
    for fname in sorted(os.listdir(root)):
        if fname.endswith(".py"):
            with open(os.path.join(root, fname)) as fh:
                yield fname, fh.read()


def test_no_call_site_rounds_kelly_core_itself():
    """The unrounded primitive must never be rounded inside kellypkg."""
    for fname, src in _pkg_sources():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "round":
                    for arg in node.args:
                        if isinstance(arg, ast.Name) and arg.id == "kelly_core":
                            pytest.fail(
                                f"{fname}: kelly_core result must stay unrounded"
                            )


def test_kelly_core_is_unrounded():
    from tools.kelly import kelly_core

    val = kelly_core(0.55, 0.909090909090909)
    # Exact float equality against the same expression evaluated with the
    # identical operation order proves no rounding pass touched the result.
    p, b = 0.55, 0.909090909090909
    q = 1.0 - p
    assert val == max(0.0, (b * p - q) / b)


def test_kelly_full_rounds_to_six_decimals():
    from tools.kelly import kelly_full

    for edge, odds in [(0.05, -110), (0.03, 150), (0.01, -105), (0.10, 200)]:
        val = kelly_full(edge, odds)
        assert val == round(val, 6)


def test_kelly_full_is_exact_multiple_of_1e_6():
    from tools.kelly import kelly_full

    val = kelly_full(0.041237, 137)
    scaled = val * 1_000_000
    assert abs(scaled - round(scaled)) < 1e-6


def test_kelly_binary_via_sizing_stays_unrounded_and_delegates_to_kelly_core(monkeypatch):
    """tools.sizing.kelly_binary must delegate to the shared unrounded core."""
    import tools.sizing as sizing
    from tools.kellypkg.core import kelly_core as pkg_core

    calls = []
    real = sizing.kelly_core

    def spy(p, b):
        calls.append((p, b))
        return real(p, b)

    monkeypatch.setattr(sizing, "kelly_core", spy)
    out = sizing.kelly_binary(0.55, 1.909090909090909)
    assert calls == [(0.55, 0.909090909090909)]
    assert out == pkg_core(0.55, 0.909090909090909)
    # unrounded: exact equality with the raw formula, same op order
    p, b = 0.55, 0.909090909090909
    q = 1.0 - p
    assert out == max(0.0, (b * p - q) / b)


def test_two_paths_are_distinct_functions():
    """The split must NOT merge kelly_full and kelly_binary/kelly_core."""
    import tools.sizing as sizing

    assert facade.kelly_full.__code__ is not sizing.kelly_binary.__code__
    assert facade.kelly_full.__code__ is not facade.kelly_core.__code__


def test_kelly_full_delegates_to_kelly_core_formula(monkeypatch):
    from tools.kelly import _american_to_decimal, kelly_core, kelly_full

    edge, odds = 0.05, -110
    from tools.odds_api import calculate_implied_probability

    p = min(1.0, max(0.0, calculate_implied_probability(int(odds)) + edge))
    b = _american_to_decimal(odds) - 1.0
    assert kelly_full(edge, odds) == round(kelly_core(p, b), 6)


def test_facade_and_package_kelly_core_share_one_implementation():
    """tools.kelly.kelly_core and tools.kellypkg.core.kelly_core must be the
    same function object (single formula, not two merged/duplicated paths)."""
    from tools.kellypkg.core import kelly_core as pkg_core

    assert facade.kelly_core is pkg_core


# ---------------------------------------------------------------------------
# 3. Behavior parity spot-checks
# ---------------------------------------------------------------------------

def test_kelly_full_zero_or_negative_edge_returns_zero():
    assert facade.kelly_full(0.0, -110) == 0.0
    assert facade.kelly_full(-0.05, -110) == 0.0
    assert facade.kelly_full(-0.05, -110) >= 0.0


def test_kelly_fractional_scales_full():
    full = facade.kelly_full(0.05, -110)
    quarter = facade.kelly_fractional(0.05, -110, fraction=0.25)
    assert quarter == round(full * 0.25, 6)


def test_kelly_dynamic_structure_and_caps():
    res = facade.kelly_dynamic(0.03, -110, 0.80, 0.02, 10_000)
    assert {"stake", "fraction", "tier", "reasoning"} <= set(res)
    assert res["fraction"] <= 0.05
    assert res["stake"] == round(10_000 * res["fraction"], 2)

    huge = facade.kelly_dynamic(0.30, 500, 0.99, 0.0, 10_000)
    assert huge["hard_cap_applied"] is True
    assert huge["fraction"] == 0.05


def test_unverified_confidence_means_no_stake():
    res = facade.kelly_dynamic(0.05, -110, 0.10, 0.01, 10_000)
    assert res["tier"] == "UNVERIFIED"
    assert res["stake"] == 0.0


def test_kelly_portfolio_empty():
    assert facade.kelly_portfolio([]) == []


def test_kelly_portfolio_summary_present():
    bets = [
        {"edge": 0.03, "odds": -110, "correlation_with_others": 0.3},
        {"edge": 0.04, "odds": 120, "correlation_with_others": 0.3},
    ]
    results = facade.kelly_portfolio(bets)
    assert len(results) == 2
    summary = results[0]["portfolio_summary"]
    assert summary["bet_count"] == 2
    assert summary["final_total_allocation"] <= 0.20 + 1e-9


def test_ruin_probability_neg_ev_is_certain():
    res = facade.ruin_probability(10_000, 100, 0.40, -110)
    assert res["ruin_probability"] == 1.0
    assert res["recommended_max_stake"] == 0.0
    assert "NEGATIVE EV" in res["analysis"]


def test_ruin_probability_analytical_positive_ev_low_risk():
    res = facade.ruin_probability(10_000, 50, 0.60, -110)
    assert res["ruin_probability"] < 0.001
    assert res["risk_level"] in ("NEGLIGIBLE", "LOW")


def test_timing_value_recommendations():
    no_bet = facade.timing_value(-0.02, 10)
    assert no_bet["recommendation"] == "NO_BET"
    res = facade.timing_value(0.03, 10)
    assert res["recommendation"] in ("WAIT", "BET_NOW", "SLIGHT_LEAN_NOW")


def test_calculate_units_labels():
    assert facade.calculate_units(10_000, 0.03, 0.95)["unit_label"] in (
        "LEAN", "HALF", "STANDARD", "STRONG", "MAX"
    )
    zero = facade.calculate_units(10_000, 0.03, 0.10)
    assert zero["unit_label"] == "NO_BET"


def test_constants_preserved_exactly():
    from tools.kellypkg.constants import (
        AGP_TIER_MULTIPLIERS,
        LINE_MOVEMENT_PROFILES,
        MARKET_CLV_DECAY,
    )

    assert AGP_TIER_MULTIPLIERS["VERIFIED"] == 1.00
    assert AGP_TIER_MULTIPLIERS["UNVERIFIED"] == 0.00
    assert len(LINE_MOVEMENT_PROFILES) == 6
    assert MARKET_CLV_DECAY["h2h"] == 1.2


def test_american_to_decimal_roundtrip_values():
    assert facade._american_to_decimal(-110) == pytest.approx(1.9090909090)
    assert facade._american_to_decimal(150) == 2.5
    assert facade._american_to_decimal(0) == 2.0


def test_confidence_tier_boundaries():
    f = facade._confidence_tier_from_score
    assert f(0.90) == "VERIFIED"
    assert f(0.899999) == "CORROBORATED"
    assert f(0.75) == "CORROBORATED"
    assert f(0.55) == "PROBABLE"
    assert f(0.30) == "SPECULATIVE"
    assert f(0.29) == "UNVERIFIED"


# ---------------------------------------------------------------------------
# 4. No live-betting regression introduced by the split
# ---------------------------------------------------------------------------

def test_split_does_not_touch_paper_trade_statuses():
    """The refactor must not add 'live' anywhere in kellypkg sources."""
    for fname, src in _pkg_sources():
        assert '"live"' not in src and "'live'" not in src, fname


def test_kellypkg_never_imports_live_betting_executor():
    for fname, src in _pkg_sources():
        tree = ast.parse(src)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "bet_executor" not in n and "betexec" not in n, fname
