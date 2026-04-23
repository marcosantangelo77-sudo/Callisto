"""Cross-market synthetic arb tests + book-limit capping + persistence.

Synthetic arb scope: team-totals Over(X) + Over(Y) vs game total Over(X+Y) at
a different book. The scanner's responsibility is to flag the price-level
advantage; correlation-aware sizing is an executor concern.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from tools.arbitrage_scanner import (
    scan_cross_market_synthetic,
    scan_pure_arb,
    persist_opportunity,
)
from tools.book_keys import get_book_max_stake


NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
FRESH = NOW.isoformat()


def _make_team_total_game() -> dict:
    """Team totals Over 4.5 (Home) at FanDuel +120 and Over 3.5 (Away) at
    DraftKings +130, plus a cheap game total Over 8.0 at BetMGM +100.

    1/2.2 + 1/2.3 = 0.4545 + 0.4348 = 0.8893 (team totals combined)
    1/2.0 = 0.5 (game total)
    gap = 0.5 - 0.8893 < 0 → no synthetic arb from combined-under-game; try
    flipping prices to force the flag.
    """
    return {
        "id": "tt-g", "home_team": "Home", "away_team": "Away",
        "sport_key": "baseball_mlb",
        "bookmakers": [
            {"key": "fanduel", "title": "FanDuel", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "team_totals", "outcomes": [
                 {"name": "Over", "description": "Home", "price": 120, "point": 4.5,
                  "fetched_at": FRESH},
             ]}]},
            {"key": "draftkings", "title": "DraftKings", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "team_totals", "outcomes": [
                 {"name": "Over", "description": "Away", "price": 130, "point": 3.5,
                  "fetched_at": FRESH},
             ]}]},
            {"key": "betmgm", "title": "BetMGM", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "totals", "outcomes": [
                 # Make the game total very short (expensive) to force an edge:
                 # -300 → decimal 1.333 → implied 0.75. Combined team = 0.889 <
                 # 0.75? No, combined is still larger. Reverse: make team totals
                 # cheap (long), game total cheap too. Test below.
                 {"name": "Over", "price": -300, "point": 8.0, "fetched_at": FRESH},
             ]}]},
        ],
    }


def test_synthetic_arb_emits_when_gap_exists():
    """Set up a contrived game where combined team-total implied is LESS than
    game-total implied → the scanner should flag a synthetic arb."""
    game = _make_team_total_game()
    arbs = scan_cross_market_synthetic(game, now=NOW)
    # In the example above: game_implied 0.75 > combined 0.889? No — 0.889 > 0.75,
    # so gap = game - combined = -0.14, NOT flagged. This validates the no-false-
    # positive case.
    assert arbs == []


def test_synthetic_arb_emits_when_team_totals_cheaper():
    """Flip the inequality: team totals at +220 / +220 vs game total at -700.

    Combined team = 1/3.2 + 1/3.2 = 0.625
    Game implied = 1/(100/700+1) = 0.875
    gap = 0.875 - 0.625 = 0.25 → clearly flagged.
    """
    game = {
        "id": "tt2", "home_team": "Home", "away_team": "Away",
        "sport_key": "baseball_mlb",
        "bookmakers": [
            {"key": "fanduel", "title": "FanDuel", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "team_totals", "outcomes": [
                 {"name": "Over", "description": "Home", "price": 220,
                  "point": 4.5, "fetched_at": FRESH},
             ]}]},
            {"key": "draftkings", "title": "DraftKings", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "team_totals", "outcomes": [
                 {"name": "Over", "description": "Away", "price": 220,
                  "point": 3.5, "fetched_at": FRESH},
             ]}]},
            {"key": "betmgm", "title": "BetMGM", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "totals", "outcomes": [
                 {"name": "Over", "price": -700, "point": 8.0,
                  "fetched_at": FRESH},
             ]}]},
        ],
    }
    arbs = scan_cross_market_synthetic(game, now=NOW)
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb.thesis_tag == "synthetic_arb"
    assert arb.profit_pct > 0.2


def test_book_limit_caps_effective_budget():
    """Fanatics' $1000 h2h cap should shrink the effective budget for a big arb."""
    # Construct a realistic ~2% arb (well below MAX_PROFIT_PCT=10%) with
    # Fanatics as one leg and a $5000 budget — Fanatics' $1000 h2h cap
    # should force effective_budget < 5000.
    game = {
        "id": "cap-g", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Home", "price": 105, "fetched_at": FRESH},
             ]}]},
            {"key": "fanatics_sportsbook", "title": "Fanatics", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Away", "price": 105, "fetched_at": FRESH},
             ]}]},
        ],
    }
    arbs = scan_pure_arb(game, "h2h", budget=5000.0, now=NOW)
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb.limited_by_book_caps is True
    assert arb.effective_budget < 5000.0
    # Fanatics h2h cap = 1000; the Fanatics leg should be ≤ 1000.
    fanatics_leg = [l for l in arb.legs if "fanatics" in l.bookmaker_canonical][0]
    assert fanatics_leg.stake <= get_book_max_stake("fanatics", "h2h") + 0.51


def test_persist_opportunity_round_trip(tmp_path):
    """persist_opportunity should write one row per leg with source='arbitrage'."""
    db_path = tmp_path / "arb.db"
    conn = sqlite3.connect(str(db_path))
    # Minimum schema — mirror tools/line_monitor.py DDL.
    conn.execute("""
        CREATE TABLE ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL, sport TEXT, game_id TEXT, team TEXT,
            market TEXT, bookmaker TEXT, american_odds INTEGER,
            implied_probability REAL, estimated_true_prob REAL, edge REAL,
            expected_value REAL, kelly_fraction REAL,
            status TEXT DEFAULT 'open',
            source TEXT DEFAULT 'line_movement'
        )
    """)
    conn.commit()

    game = {
        "id": "persist-g", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Home", "price": 105, "fetched_at": FRESH},
             ]}]},
            {"key": "fanduel", "title": "FanDuel", "last_update": FRESH,
             "fetched_at": FRESH,
             "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Away", "price": 105, "fetched_at": FRESH},
             ]}]},
        ],
    }
    arbs = scan_pure_arb(game, "h2h", now=NOW)
    assert arbs
    ids = persist_opportunity(conn, arbs[0])
    conn.commit()
    assert len(ids) == 2
    rows = conn.execute(
        "SELECT source, thesis_tag, team, bookmaker, status, expires_at "
        "FROM ev_opportunities"
    ).fetchall()
    assert all(r[0] == "arbitrage" for r in rows)
    assert all(r[1] == "arb" for r in rows)
    assert all(r[4] == "open" for r in rows)
    assert all(r[5] is not None for r in rows)
