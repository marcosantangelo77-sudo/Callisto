"""Synthetic-book arbitrage math tests.

These are the lowest-level unit tests for arbitrage_scanner. Every assertion
here is a known closed-form answer — no ML, no randomness, no DB.

A pure 2-way arb exists when 1/d1 + 1/d2 < 1. The canonical example uses
American -105 on both sides of a totally vigless market (decimal = 1.9524),
which gives 1/1.9524 + 1/1.9524 = 1.024... → NOT an arb. To get a real arb
you need opposite sides at two books where BOTH prices are better than
-100/-100; e.g. +105 on home at book A and +105 on away at book B:
    d_home = 2.05, d_away = 2.05, sum = 0.9756 < 1 → arb, ~2.5% profit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.arbitrage_scanner import (
    scan_pure_arb,
    full_arbitrage_scan,
    DEFAULT_BUDGET,
)


NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
FRESH_TS = NOW.isoformat()


def _make_game(legs: list[dict], market: str = "h2h", gid: str = "g1") -> dict:
    """Build a synthetic odds-api style game dict from a flat leg list.

    Each leg: {book, team, price, point?, fetched_at?}
    """
    by_book: dict[str, list] = {}
    for l in legs:
        by_book.setdefault(l["book"], []).append(l)

    bookmakers = []
    for book_key, items in by_book.items():
        outcomes = []
        for l in items:
            o = {
                "name": l["team"],
                "price": l["price"],
                "fetched_at": l.get("fetched_at", FRESH_TS),
            }
            if l.get("point") is not None:
                o["point"] = l["point"]
            outcomes.append(o)
        bookmakers.append({
            "key": book_key,
            "title": book_key.title(),
            "last_update": FRESH_TS,
            "fetched_at": FRESH_TS,
            "markets": [{"key": market, "outcomes": outcomes}],
        })

    return {
        "id": gid,
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": bookmakers,
    }


def test_known_arb_is_found():
    """+105 / +105 on two sides = 2.44% guaranteed profit."""
    game = _make_game([
        {"book": "pinnacle", "team": "Home", "price": 105},
        {"book": "fanduel", "team": "Away", "price": 105},
    ])
    arbs = scan_pure_arb(game, "h2h", now=NOW)
    assert len(arbs) == 1
    arb = arbs[0]
    # 1/2.05 + 1/2.05 = 0.9756
    assert abs(arb.total_implied - 0.9756) < 1e-3
    # profit = (1 - 0.9756) / 0.9756 ≈ 2.5%
    assert abs(arb.profit_pct - 0.025) < 0.005
    assert len(arb.legs) == 2
    assert arb.thesis_tag == "arb"


def test_no_arb_when_vig_present():
    """-110 / -110 = 4.5% total vig, definitely not an arb."""
    game = _make_game([
        {"book": "draftkings", "team": "Home", "price": -110},
        {"book": "fanduel", "team": "Away", "price": -110},
    ])
    assert scan_pure_arb(game, "h2h", now=NOW) == []


def test_stake_proportionality_guarantees_equal_return():
    """For any arb, stake_i * decimal_i should be the same across legs."""
    game = _make_game([
        {"book": "pinnacle", "team": "Home", "price": 120},
        {"book": "fanduel", "team": "Away", "price": 110},
    ])
    arbs = scan_pure_arb(game, "h2h", budget=1000.0, now=NOW)
    assert arbs
    arb = arbs[0]
    returns = [leg.stake * leg.decimal_odds for leg in arb.legs]
    # All returns equal within rounding
    assert max(returns) - min(returns) < 1.0


def test_budget_respected_total_stake_near_budget():
    """Sum of leg stakes should equal effective_budget within rounding."""
    game = _make_game([
        {"book": "pinnacle", "team": "Home", "price": 110},
        {"book": "bovada", "team": "Away", "price": 105},
    ])
    arbs = scan_pure_arb(game, "h2h", budget=500.0, now=NOW)
    assert arbs
    arb = arbs[0]
    total_stake = sum(leg.stake for leg in arb.legs)
    # Pinnacle/Bovada caps high enough that no capping should trigger.
    assert not arb.limited_by_book_caps
    assert abs(total_stake - 500.0) < 2.0


def test_multi_way_dutch_is_tagged_as_dutch():
    """3-way soccer-style market priced as a small arb tags thesis='dutch'."""
    # 1/2.5 + 1/3.5 + 1/3.5 = 0.4 + 0.286 + 0.286 = 0.971 → ~3% arb, well
    # inside MAX_PROFIT_PCT.
    game = _make_game([
        {"book": "pinnacle", "team": "Home", "price": 150},
        {"book": "fanduel", "team": "Draw", "price": 250},
        {"book": "betmgm", "team": "Away", "price": 250},
    ], market="h2h")
    arbs = scan_pure_arb(game, "h2h", now=NOW)
    assert len(arbs) == 1
    assert arbs[0].thesis_tag == "dutch"
    assert len(arbs[0].legs) == 3


def test_full_scan_aggregates_across_markets():
    """full_arbitrage_scan should find h2h, spreads, and totals independently."""
    # Build a game with an h2h arb + a totals arb.
    game = {
        "id": "g7",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "last_update": FRESH_TS, "fetched_at": FRESH_TS,
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Home", "price": 110, "fetched_at": FRESH_TS},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 105, "point": 8.5, "fetched_at": FRESH_TS},
                    ]},
                ],
            },
            {
                "key": "draftkings", "title": "DraftKings",
                "last_update": FRESH_TS, "fetched_at": FRESH_TS,
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Away", "price": 110, "fetched_at": FRESH_TS},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Under", "price": 105, "point": 8.5, "fetched_at": FRESH_TS},
                    ]},
                ],
            },
        ],
    }
    snapshot = {"sport": "baseball_mlb", "games": [game]}
    res = full_arbitrage_scan(snapshot, now=NOW)
    assert res["summary"]["pure_count"] >= 2
