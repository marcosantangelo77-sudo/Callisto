"""Tier 4 data-plane characterization tests — odds units and conversions.

Pins CURRENT behaviour of pure conversion/normalization functions across the
scraper/API stack so unit drift (the confirmed defect class guarding real
money) becomes visible. No network, no database.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tools.odds_api_io import (
    _decimal_to_american,
    _pick_primary_spread,
    _pick_primary_total,
    _safe_float,
)
from tools.dk_scraper import (
    _parse_nash_american_odds,
    _dk_american_odds,
    _expand_dk_short_name,
)
from tools.odds_api import (
    calculate_implied_probability,
    calculate_ev,
    find_best_line,
    detect_line_movement,
)


class TestDecimalToAmerican:
    """Pin odds_api_io._decimal_to_american — every WS/REST normalize passes
    through this. A sign or rounding flip here poisons every snapshot."""

    def test_plus_side(self):
        assert _decimal_to_american(2.50) == 150
        assert _decimal_to_american(3.00) == 200
        assert _decimal_to_american(2.00) == 100

    def test_minus_side(self):
        assert _decimal_to_american(1.50) == -200
        assert _decimal_to_american(1.43) == -233  # round(-100/0.43)


    def test_boundary_even(self):
        # 1.91 -> -100/(0.91) = -109.89 -> round = -110
        assert _decimal_to_american(1.91) == -110

    def test_degenerate(self):
        assert _decimal_to_american(1.0) == -10000  # sentinel, not crash


class TestDkNashOddsParsing:
    def test_ascii(self):
        assert _parse_nash_american_odds("-112") == -112
        assert _parse_nash_american_odds("+150") == 150

    def test_unicode_minus(self):
        # U+2212 MINUS SIGN — the actual defect this function was written for
        assert _parse_nash_american_odds("\u2212112") == -112

    def test_en_dash(self):
        assert _parse_nash_american_odds("\u2013110") == -110

    def test_garbage_is_zero_not_crash(self):
        assert _parse_nash_american_odds("") == 0
        assert _parse_nash_american_odds("N/A") == 0

    def test_dk_decimal_conversion(self):
        assert _dk_american_odds(2.50) == 150
        assert _dk_american_odds(1.91) == -110

    def test_expand_short_name(self):
        assert _expand_dk_short_name("CHA Hornets") == "Charlotte Hornets"
        assert _expand_dk_short_name("GS Warriors") == "Golden State Warriors"
        # Unknown prefix passes through unchanged (silent mismatch risk downstream)
        assert _expand_dk_short_name("XX Unknowns") == "XX Unknowns"


class TestPrimaryLinePickers:
    """odds-api.io returns alt-line ladders; _pick_primary_* selects the main
    line as the entry closest to -110/-110. Pin the selection."""

    def test_spread_picks_main(self):
        ladder = [
            {"hdp": -10.5, "home": "2.20", "away": "1.70"},
            {"hdp": -6.5, "home": "1.91", "away": "1.91"},
            {"hdp": -2.5, "home": "1.55", "away": "2.45"},
        ]
        out = _pick_primary_spread(ladder, "Home Team", "Away Team")
        assert out[0]["point"] == -6.5
        assert out[1]["point"] == 6.5  # mirrored
        # Prices converted to American
        assert out[0]["price"] == -110
        assert out[1]["price"] == -110

    def test_total_picks_main_and_same_point(self):
        ladder = [
            {"hdp": 220.5, "over": "1.75", "under": "2.10"},
            {"hdp": 226.5, "over": "1.87", "under": "1.95"},
        ]
        out = _pick_primary_total(ladder)
        assert out[0]["point"] == 226.5
        assert out[1]["point"] == 226.5  # SAME point for Over and Under
        assert out[0]["name"] == "Over"

    def test_empty_ladder_returns_none(self):
        assert _pick_primary_spread([], "A", "B") is None
        assert _pick_primary_total([{"hdp": None}]) is None


class TestImpliedProbAndEV:
    def test_implied_prob_standard(self):
        assert abs(calculate_implied_probability(-110) - 0.5238) < 0.001
        assert abs(calculate_implied_probability(+150) - 0.40) < 0.0001

    def test_ev_positive_edge(self):
        r = calculate_ev(probability=0.55, american_odds=-110, stake=100)
        assert r["is_positive_ev"] is True
        assert r["edge"] > 0
        assert 0 < r["kelly_fraction"] < 1

    def test_ev_negative_no_bet(self):
        r = calculate_ev(probability=0.45, american_odds=-110)
        assert r["is_positive_ev"] is False
        assert r["kelly_fraction"] == 0


def _mk_game(prices_by_book, name="Team A"):
    bms = []
    for bk, prices in prices_by_book.items():
        bms.append({
            "key": bk, "title": bk,
            "markets": [{
                "key": "spreads",
                "outcomes": [{"name": name, "price": p, "point": -3.5} for p in prices],
            }],
        })

    return {"bookmakers": bms}


class TestFindBestLine:
    def test_best_is_max_price(self):
        g = _mk_game({"dk": [-110], "fd": [-105], "mgm": [-115]})
        r = find_best_line(g, market="spreads", team="Team")
        assert r["best"]["bookmaker"] == "fd"
        assert r["spread_across_books"] == 10

    def test_h2h_contamination_filter(self):
        # Both sides of the market leaked into one team's line set
        g = _mk_game({"a": [-700], "b": [+500], "c": [+520], "d": [-680]})
        g["bookmakers"][0]["markets"][0]["key"] = "h2h"
        g["bookmakers"][1]["markets"][0]["key"] = "h2h"
        g["bookmakers"][2]["markets"][0]["key"] = "h2h"
        g["bookmakers"][3]["markets"][0]["key"] = "h2h"
        r = find_best_line(g, market="h2h", team="Team")
        # Majority positive -> negatives purged
        assert all(l["price"] > 0 for l in r["all_lines"])


class TestDetectLineMovement:
    def test_threshold_crossing(self):
        old = {"games": [{"id": "1", "bookmakers": [{
            "key": "dk", "title": "DK",
            "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home Team", "price": -110, "point": -3.5}]}]}]}]}
        new = {"games": [{"id": "1", "bookmakers": [{
            "key": "dk", "title": "DK",
            "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home Team", "price": -120, "point": -4.0}]}]}]}]}
        movs = detect_line_movement(old, new)
        assert len(movs) == 1
        m = movs[0]
        assert m["price_movement"] == -10
        assert m["point_movement"] == pytest.approx(-0.5)

    def test_subthreshold_ignored(self):
        old = {"games": [{"id": "1", "bookmakers": [{
            "key": "dk", "title": "DK",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "price": -110, "point": 220.5}]}]}]}]}
        new = {"games": [{"id": "1", "bookmakers": [{
            "key": "dk", "title": "DK",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "price": -112, "point": 220.5}]}]}]}]}
        assert detect_line_movement(old, new) == []


class TestSafeFloat:
    def test_strings_and_none(self):
        assert _safe_float("2.95") == 2.95
        assert _safe_float(None) is None
        assert _safe_float("abc") is None
