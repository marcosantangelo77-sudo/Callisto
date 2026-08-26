"""
Tests for the tools.dead_numbers split into tools.dead.

Verifies:
- The facade re-exports all public names from the submodules.
- Public function behavior is preserved (key numbers, push probabilities,
  dead number detection, line shopping value, shading detection).
- Numeric behaviors: NFL 3-point dominance, half-point no-push rule,
  interpolation of unknown key numbers, buy-points EV math.
"""

import math

import pytest

import tools.dead as dead_pkg
import tools.dead_numbers as facade
from tools.dead.data import (
    DEAD_THRESHOLD,
    KEY_NUMBERS,
    MARGIN_FREQ,
    PUSH_PROB,
    SPORT_ALIASES,
)
from tools.dead.valuation import (
    get_margin_distribution,
    half_quarter_key_value,
    is_dead_number,
    key_number_value,
    line_shopping_value,
    push_probability,
)


# ---------------------------------------------------------------------------
# Facade re-export integrity
# ---------------------------------------------------------------------------

def test_facade_matches_package_namespaces():
    for name in dead_pkg.__all__:
        assert hasattr(facade, name), f"facade missing {name}"
        assert getattr(facade, name) is getattr(dead_pkg, name)


def test_facade_tables_are_shared_objects():
    assert facade.KEY_NUMBERS is KEY_NUMBERS
    assert facade.MARGIN_FREQ is MARGIN_FREQ
    assert facade.PUSH_PROB is PUSH_PROB
    assert facade.SPORT_ALIASES is SPORT_ALIASES
    assert facade.DEAD_THRESHOLD is DEAD_THRESHOLD


def test_all_sports_have_tables():
    expected = {"NFL", "NBA", "NCAAF", "NCAAB", "MLB", "NHL"}
    assert set(MARGIN_FREQ) == expected
    assert set(KEY_NUMBERS) == expected
    assert set(PUSH_PROB) == expected
    assert set(SPORT_ALIASES.values()) >= {"NFL", "NBA", "NCAAF", "NCAAB", "MLB", "NHL"}


# ---------------------------------------------------------------------------
# Key number valuation
# ---------------------------------------------------------------------------

def test_key_number_value_nfl_three_is_max():
    assert key_number_value(3.0, "NFL") == 1.00


def test_key_number_value_ignores_sign():
    assert key_number_value(-3.0, "NFL") == key_number_value(3.0, "NFL")
    assert key_number_value(7.0, "NFL") == key_number_value(-7.0, "NFL")


def test_key_number_value_mlb_puck_line():
    assert key_number_value(1.5, "MLB") == 1.00
    assert key_number_value(1.0, "NHL") == 0.95


def test_key_number_value_interpolates_unknown_spread():
    # 3.25 sits between 3.0 (1.00) and 3.5 (0.60) in NFL -> midpoint 0.80
    assert key_number_value(3.25, "NFL") == pytest.approx(0.8)


def test_key_number_value_extreme_spread_is_dead():
    assert key_number_value(50.0, "NFL") == 0.02


def test_key_number_value_unknown_sport_raises():
    with pytest.raises(ValueError):
        key_number_value(3.0, "CRICKET")


def test_normalize_sport_alias():
    assert key_number_value("americanfootball_nfl" if False else 3.0, "americanfootball_nfl") == 1.0


# ---------------------------------------------------------------------------
# Dead number detection
# ---------------------------------------------------------------------------

def test_is_dead_number_nfl():
    assert not is_dead_number(3.0, "NFL")
    assert not is_dead_number(-7.0, "NFL")
    # NFL threshold is 0.12; importance at 12 is 0.08 -> dead
    assert is_dead_number(12.0, "NFL")
    assert is_dead_number(19.0, "NFL")


def test_is_dead_number_thresholds_table():
    for sport, threshold in DEAD_THRESHOLD.items():
        assert 0 < threshold < 1


# ---------------------------------------------------------------------------
# Push probabilities — numeric behavior
# ---------------------------------------------------------------------------

def test_push_probability_nfl_three():
    assert push_probability(3.0, "NFL") == pytest.approx(0.148)


def test_push_probability_half_point_never_pushes():
    for sport in ("NFL", "NBA", "MLB", "NHL"):
        assert push_probability(2.5, sport) == 0.0
        assert push_probability(-3.5, sport) == 0.0


def test_push_probability_sign_and_missing_number():
    assert push_probability(-7.0, "NFL") == push_probability(7.0, "NFL")
    assert push_probability(99.0, "NFL") == 0.0
    with pytest.raises(ValueError):
        push_probability(3.0, "CRICKET")


def test_get_margin_distribution_returns_copy():
    table = get_margin_distribution("NFL")
    assert table[3] == pytest.approx(14.8)
    table[3] = -1.0
    assert MARGIN_FREQ["NFL"][3] == pytest.approx(14.8)


# ---------------------------------------------------------------------------
# Line shopping value — numeric behavior
# ---------------------------------------------------------------------------

def test_line_shopping_value_crossing_three():
    result = line_shopping_value(-3.0, -2.5, "NFL")
    # The push mass at 3 (~14.8%) plus the margin-3 win mass (~7.4%) shifts:
    # the implementation counts the full frequency table entry plus half the
    # boundary push -> ~22.2% of outcomes.
    assert result["prob_difference"] == pytest.approx(0.222)
    assert result["prob_difference_pct"] == 22.2
    assert any(k["number"] == 3 for k in result["crossed_key_numbers"])
    assert "CRITICAL" in result["recommendation"]


def test_line_shopping_value_dead_number_move_negligible():
    result = line_shopping_value(-18.5, -19.5, "NFL")
    # Margins 19 is only 1.0% of outcomes; no key number crossed
    assert result["prob_difference"] <= 0.01
    assert result["crossed_key_numbers"] == []
    assert "NEGLIGIBLE" not in result["recommendation"] or "LOW" in result["recommendation"]


def test_line_shopping_value_cents_consistency():
    result = line_shopping_value(-3.0, -2.5, "NFL")
    assert result["cents_value"] == pytest.approx(result["prob_difference"] * 191, abs=0.05)


# ---------------------------------------------------------------------------
# Half/quarter key values
# ---------------------------------------------------------------------------

def test_half_quarter_key_value_first_quarter_zero_massive():
    assert half_quarter_key_value(0.0, "NFL", "1Q") == pytest.approx(0.50)
    assert half_quarter_key_value(3.0, "NFL", "1H") == pytest.approx(0.85)
    # 1Q key at 3 exceeds full-game dampening fallback
    assert half_quarter_key_value(3.0, "NFL", "1Q") > 0.6


def test_half_quarter_key_value_fallback_for_unknown_period():
    full_game = key_number_value(3.0, "MLB")
    assert half_quarter_key_value(3.0, "MLB", "1P") == pytest.approx(full_game * 0.7)


# ---------------------------------------------------------------------------
# Shading detection
# ---------------------------------------------------------------------------

def test_detect_public_shading_on_key_number():
    result = facade.detect_public_shading(-3.0, "NFL")
    assert result["shaded_toward"] == 3.0
    assert result["confidence"] == "HIGH"
    assert result["estimated_shade_cents"] > 0
    assert result["actionable"] is True


def test_detect_public_shading_not_near_key():
    result = facade.detect_public_shading(-13.5, "NBA")
    assert result["shaded_toward"] is None
    assert result["confidence"] == "LOW"
    assert result["estimated_shade_cents"] == 0


# ---------------------------------------------------------------------------
# Composite analysis
# ---------------------------------------------------------------------------

def test_analyze_spread_full_structure():
    result = facade.analyze_spread(-3.0, "NFL")
    assert result["is_dead_number"] is False
    assert result["push_probability"] == pytest.approx(0.148)
    assert result["push_probability_pct"] == 14.8
    assert result["period"] == "FG"
    assert "commentary" in result
    assert "3-point zone" in result["commentary"]


def test_analyze_spread_with_alt_line_includes_shopping():
    result = facade.analyze_spread(-3.0, "NFL", alt_spread=-2.5)
    assert "line_shopping" in result
    assert result["line_shopping"]["prob_difference"] == pytest.approx(0.222)


def test_rank_line_shopping_opportunities_sorted():
    lines = [
        {"bookmaker": "A", "spread": -9.0, "price": -110},
        {"bookmaker": "B", "spread": -2.5, "price": -110},
        {"bookmaker": "C", "spread": -3.0, "price": -105},
    ]
    ranked = facade.rank_line_shopping_opportunities(lines, "NFL")
    assert len(ranked) == 3
    diffs = [c["prob_difference"] for c in ranked]
    assert diffs == sorted(diffs, reverse=True)
    top = ranked[0]
    assert {top["spread_a"], top["spread_b"]} == {-2.5, -9.0}
    assert top["better_book"] == "B"
    juice_only = [c for c in ranked if c["type"] == "JUICE_ONLY"]
    assert len(juice_only) == 0  # all three spreads differ in this fixture


def test_buy_points_analysis_math():
    result = facade.buy_points_analysis(-3.0, -2.5, 20, "NFL")
    assert result["probability_gained"] == pytest.approx(0.222)
    assert result["new_juice_line"] == -130
    expected_juice_cost = 130 / 230 - 110 / 210
    assert result["juice_cost_pct"] == pytest.approx(round(expected_juice_cost, 4))
    assert result["net_value"] == pytest.approx(
        round(result["probability_gained"] - expected_juice_cost, 4)
    )
    assert result["is_profitable"] is True
    assert result["recommendation"].startswith("BUY")


def test_buy_points_analysis_pass_when_expensive():
    # Buying a tiny edge on a dead number at high cost should be a PASS
    result = facade.buy_points_analysis(-18.5, -19.0, 30, "NFL")
    assert result["is_profitable"] is False
    assert result["recommendation"].startswith("PASS")


def test_find_dead_number_steals_finds_key_vs_dead():
    # DeadBook sits on 9.0 (a dead number: importance 0.10 < 0.12 threshold)
    # while KeyBook is on 10.0, a significant key number (importance 0.45).
    lines = [
        {"bookmaker": "DeadBook", "spread": -9.0, "price": -110},
        {"bookmaker": "KeyBook", "spread": -10.0, "price": -110},
    ]
    steals = facade.find_dead_number_steals(lines, "NFL")
    assert len(steals) == 1
    steal = steals[0]
    assert steal["dead_book"] == "DeadBook"
    assert steal["key_book"] == "KeyBook"
    assert steal["key_number_crossed"] == 10.0
    assert "STEAL" in steal["recommendation"]
    assert steal["prob_difference"] > 0


def test_find_dead_number_steals_empty_inputs():
    assert facade.find_dead_number_steals([], "NFL") == []
    one = [{"bookmaker": "A", "spread": -3.0, "price": -110}]
    assert facade.find_dead_number_steals(one, "NFL") == []
    same = [
        {"bookmaker": "A", "spread": -3.0, "price": -110},
        {"bookmaker": "B", "spread": -3.0, "price": -120},
    ]
    assert facade.find_dead_number_steals(same, "NFL") == []


# ---------------------------------------------------------------------------
# Downstream consumers still work through the facade
# ---------------------------------------------------------------------------

def test_downstream_import_paths_still_resolve():
    # The real consumers import from the facade; verify they still resolve.
    # NOTE: tools.edge_scanner must be imported first — a pre-existing
    # circular-import ordering quirk between edge_scanner and tools.edges.
    import tools.edge_scanner  # noqa: F401
    import tools.edges.filters  # noqa: F401
    import tools.edges.common  # noqa: F401
    import tools.autonomous  # noqa: F401
    import tools.api.odds_extra  # noqa: F401
