"""Tests for the tools/prop_scraper_free -> tools/propscrape split.

Pure parser/format tests — no network, no betting execution.
"""

import asyncio

import pytest

import tools.prop_scraper_free as facade
from tools.propscrape import (
    classify_dk_nash_prop,
    classify_fd_prop,
    classify_mgm_prop,
    mgm_decimal_to_american,
    mgm_parse_odds,
    scrape_dk_props,
    scrape_fd_props,
    scrape_mgm_props,
)
from tools.propscrape.common import parse_nash_american_odds


# ─────────────────────────────────────────────────────────────────────
# Module structure / facade re-exports
# ─────────────────────────────────────────────────────────────────────

def test_facade_reexports_scrapers():
    for name in (
        "scrape_dk_props",
        "scrape_fd_props",
        "scrape_mgm_props",
        "scrape_all_props",
        "props_to_scanner_format",
        "store_prop_snapshot",
        "ensure_prop_schema",
        "close_clients",
    ):
        assert hasattr(facade, name), f"facade missing {name}"


def test_facade_private_aliases_preserved():
    assert facade._classify_dk_nash_prop is classify_dk_nash_prop
    assert facade._parse_nash_american_odds is parse_nash_american_odds
    assert facade._classify_fd_prop is classify_fd_prop
    assert facade._classify_mgm_prop is classify_mgm_prop
    assert facade._mgm_decimal_to_american is mgm_decimal_to_american
    assert facade._mgm_parse_odds is mgm_parse_odds


def test_facade_prop_markets_set():
    assert "player_points" in facade.PROP_MARKETS
    assert "pitcher_strikeouts" in facade.PROP_MARKETS


def test_no_live_status_widening():
    """Guard: the module must never gain a 'live' paper-trade status."""
    src = open(facade.__file__, encoding="utf-8").read()
    assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
    assert "'live'" not in src and '"live"' not in src


# ─────────────────────────────────────────────────────────────────────
# DK Nash parser
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Points O/U", "player_points"),
    ("rebounds o/u", "player_rebounds"),
    ("Pts + Reb + Ast O/U", "player_points_rebounds_assists"),
    ("Strikeouts o/u", "pitcher_strikeouts"),
    ("Passing Yards o/u", "player_pass_yds"),
    # regex fallback
    ("Player Points 25+ O/U", "player_points"),
    ("Shots on Goal 3+ o/u", "player_shots_on_goal"),
])
def test_classify_dk_nash_prop(name, expected):
    assert classify_dk_nash_prop(name) == expected


def test_classify_dk_nash_prop_unknown():
    assert classify_dk_nash_prop("totally unknown market") is None


@pytest.mark.parametrize("raw,expected", [
    ("-110", -110),
    ("+150", 150),
    ("\u2212110", -110),   # Unicode minus
    ("\u2013205", -205),   # en dash
    ("", 0),
    ("abc", 0),
])
def test_parse_nash_american_odds(raw, expected):
    assert parse_nash_american_odds(raw) == expected


# ─────────────────────────────────────────────────────────────────────
# FanDuel parser
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mtype,expected", [
    ("PLAYER_POINTS", "player_points"),
    ("PLAYER_THREES_MADE", "player_threes"),
    ("PLAYER_ANYTIME_TOUCHDOWN", "player_touchdowns"),
    # regex fallbacks
    ("PLAYER_TOTAL_POINTS_ODD_EVEN", "player_points"),
    ("BATTER_TOTAL_BASES_SPECIAL", "batter_total_bases"),
])
def test_classify_fd_prop(mtype, expected):
    assert classify_fd_prop(mtype) == expected


def test_classify_fd_prop_unknown():
    assert classify_fd_prop("SOMETHING_ELSE") is None


# ─────────────────────────────────────────────────────────────────────
# BetMGM parser
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Player Points", "player_points"),
    ("player rebounds", "player_rebounds"),
    ("Strikeouts", "pitcher_strikeouts"),
    ("Shots on Goal", "player_shots_on_goal"),
    # pattern fallback
    ("Player Points Combo Special", "player_points"),
    ("Total Bases Market", "batter_total_bases"),
])
def test_classify_mgm_prop(name, expected):
    assert classify_mgm_prop(name) == expected


def test_classify_mgm_prop_unknown():
    assert classify_mgm_prop("corner kicks") is None


@pytest.mark.parametrize("dec,expected", [
    (3.0, 200),
    (1.5, -200),
])
def test_mgm_decimal_to_american(dec, expected):
    assert mgm_decimal_to_american(dec) == expected


def test_mgm_parse_odds_american():
    assert mgm_parse_odds({"americanOdds": "-110"}) == -110
    assert mgm_parse_odds({"americanOdds": -105}) == -105


def test_mgm_parse_odds_decimal_fallback():
    assert mgm_parse_odds({"oddsDecimal": 1.5}) == -200
    assert mgm_parse_odds({"price": "2.5"}) == 150


def test_mgm_parse_odds_none():
    assert mgm_parse_odds({}) is None
    assert mgm_parse_odds({"oddsDecimal": "garbage"}) is None


# ─────────────────────────────────────────────────────────────────────
# Scanner format conversion
# ─────────────────────────────────────────────────────────────────────

def test_props_to_scanner_format_groups_by_book_and_market():
    props = [
        {"book": "draftkings", "market": "player_points", "side": "Over",
         "price": -110, "line": 24.5, "player": "LeBron James"},
        {"book": "draftkings", "market": "player_points", "side": "Under",
         "price": +100, "line": 24.5, "player": "LeBron James"},
        {"book": "fanduel", "market": "player_rebounds", "side": "Over",
         "price": -120, "line": 7.5, "player": "Jayson Tatum"},
    ]
    out = facade.props_to_scanner_format(props)
    books = {b["key"]: b for b in out["bookmakers"]}
    assert set(books) == {"draftkings", "fanduel"}
    assert books["draftkings"]["title"] == "DraftKings"
    dk_pts = [m for m in books["draftkings"]["markets"] if m["key"] == "player_points"]
    assert len(dk_pts) == 1
    outcomes = dk_pts[0]["outcomes"]
    assert {(o["name"], o["point"], o["description"]) for o in outcomes} == {
        ("Over", 24.5, "LeBron James"),
        ("Under", 24.5, "LeBron James"),
    }


def test_props_to_scanner_format_empty():
    assert facade.props_to_scanner_format([]) == {"bookmakers": []}


# ─────────────────────────────────────────────────────────────────────
# Scraper guards (no network)
# ─────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_scrapers_unknown_sport_error():
    assert _run(scrape_dk_props("bad_sport"))["error"]
    assert _run(scrape_fd_props("bad_sport"))["error"]
    assert _run(scrape_mgm_props("bad_sport"))["error"]


def test_scrape_all_props_empty_sources(monkeypatch):
    async def fake(sport):
        return {"sport": sport, "props": [], "prop_count": 0}
    monkeypatch.setattr(facade, "scrape_dk_props", fake)
    monkeypatch.setattr(facade, "scrape_fd_props", fake)
    result = _run(facade.scrape_all_props("basketball_nba"))
    assert result["prop_count"] == 0
    assert result["sources"] == []
    assert result["source"] == "free_prop_cascade"


def test_scrape_all_props_merges_and_counts_multi_book(monkeypatch):
    async def fake_dk(sport):
        return {"props": [
            {"player": "A", "market": "player_points", "line": 20.5,
             "side": "Over", "price": -110, "book": "draftkings"},
            {"player": "B", "market": "player_threes", "line": 1.5,
             "side": "Over", "price": +130, "book": "draftkings"},
        ]}

    async def fake_fd(sport):
        return {"props": [
            {"player": "A", "market": "player_points", "line": 20.5,
             "side": "Over", "price": -115, "book": "fanduel"},
        ]}

    monkeypatch.setattr(facade, "scrape_dk_props", fake_dk)
    monkeypatch.setattr(facade, "scrape_fd_props", fake_fd)
    result = _run(facade.scrape_all_props("basketball_nba"))
    assert result["prop_count"] == 3
    assert sorted(result["sources"]) == ["dk", "fd"]
    assert result["unique_player_markets"] == 2
    assert result["multi_book_count"] == 1
