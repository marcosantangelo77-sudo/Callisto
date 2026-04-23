"""Dutch-book math tests — 3-way markets across books."""

from __future__ import annotations

from datetime import datetime, timezone

from tools.arbitrage_scanner import scan_dutch_book, scan_pure_arb


NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
FRESH = NOW.isoformat()


def _three_way_game(home_price: int, draw_price: int, away_price: int) -> dict:
    return {
        "id": "soccer-g", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Home", "price": home_price, "fetched_at": FRESH},
             ]}]},
            {"key": "fanduel", "title": "FanDuel", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Draw", "price": draw_price, "fetched_at": FRESH},
             ]}]},
            {"key": "betmgm", "title": "BetMGM", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Away", "price": away_price, "fetched_at": FRESH},
             ]}]},
        ],
    }


def test_three_way_dutch_found_and_profitable():
    """Small realistic 3-way dutch: Home +160, Draw +260, Away +260 →
    1/2.6 + 1/3.6 + 1/3.6 = 0.385 + 0.278 + 0.278 = 0.94 → ~6% dutch.

    Deliberately kept below MAX_PROFIT_PCT (10%) so the sanity guard
    accepts it.
    """
    game = _three_way_game(160, 260, 260)
    arbs = scan_dutch_book(game, "h2h", now=NOW)
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb.thesis_tag == "dutch"
    assert len(arb.legs) == 3
    assert arb.total_implied < 1.0
    # Verify equal return across legs
    returns = [l.stake * l.decimal_odds for l in arb.legs]
    assert max(returns) - min(returns) < 1.5


def test_three_way_with_vig_not_arb():
    """Typical vig'd 3-way (+120 / +240 / +150) sums to ~1.05 — NOT an arb."""
    # 1/2.2 + 1/3.4 + 1/2.5 = 0.4545 + 0.2941 + 0.4000 = 1.149 → no arb.
    game = _three_way_game(120, 240, 150)
    arbs = scan_dutch_book(game, "h2h", now=NOW)
    assert arbs == []


def test_dutch_scan_returns_same_as_pure_for_binary():
    """scan_dutch_book is scan_pure_arb with a re-tag for 3+ legs; a binary
    market should come back as 'arb' (not 'dutch') from either."""
    game = {
        "id": "g", "home_team": "H", "away_team": "A",
        "bookmakers": [
            {"key": "p", "title": "P", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "H", "price": 115, "fetched_at": FRESH},
             ]}]},
            {"key": "f", "title": "F", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "A", "price": 115, "fetched_at": FRESH},
             ]}]},
        ],
    }
    dutch = scan_dutch_book(game, "h2h", now=NOW)
    pure = scan_pure_arb(game, "h2h", now=NOW)
    assert len(dutch) == 1 and len(pure) == 1
    assert dutch[0].thesis_tag == "arb"
    assert pure[0].thesis_tag == "arb"
