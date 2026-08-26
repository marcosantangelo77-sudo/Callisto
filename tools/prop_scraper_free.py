"""
Free prop scraper cascade — player props from DK, FanDuel, BetMGM.

Scrapes player prop markets (points, rebounds, assists, threes, PRA, etc.)
from all three sportsbook APIs for FREE, then merges into a unified format
compatible with prop_scanner.py's edge detection pipeline.

This is the prop equivalent of line_monitor's free_cascade for game-level odds.
Each scraper hits the same public APIs already used for game odds, just
parsing the prop/player market categories that were previously ignored.

Zero API cost. Rate-limited per source (2s intervals).

The per-book scrapers/parsers live in tools/propscrape/ (draftkings.py,
fanduel.py, betmgm.py). This module remains the stable facade: it keeps the
public names (scrape_dk_props, scrape_fd_props, scrape_mgm_props,
scrape_all_props, props_to_scanner_format, store_prop_snapshot, ...) plus
backwards-compatible private aliases.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import aiosqlite
from dotenv import load_dotenv

from tools.propscrape import (
    classify_dk_nash_prop,
    classify_fd_prop,
    classify_mgm_prop,
    close_fd_client,
    close_mgm_session,
    close_shared_sessions,
    mgm_decimal_to_american,
    mgm_parse_odds,
    parse_nash_american_odds,
    scrape_dk_props,
    scrape_fd_props,
    scrape_mgm_props,
)

load_dotenv()

logger = logging.getLogger("callisto.prop_scraper_free")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Backwards-compatible private aliases (pre-split names)
_classify_dk_nash_prop = classify_dk_nash_prop
_parse_nash_american_odds = parse_nash_american_odds
_classify_fd_prop = classify_fd_prop
_classify_mgm_prop = classify_mgm_prop
_mgm_decimal_to_american = mgm_decimal_to_american
_mgm_parse_odds = mgm_parse_odds

# ─────────────────────────────────────────────────────────────────────
# Standard prop market keys (matches The Odds API / prop_scanner.py)
# ─────────────────────────────────────────────────────────────────────

PROP_MARKETS = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
    "player_steals",
    "player_blocks",
    "player_turnovers",
    "player_double_double",
    # MLB
    "pitcher_strikeouts",
    "pitcher_outs",
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
    "batter_runs",
    "batter_stolen_bases",
    "batter_home_runs",
    # NHL
    "player_points_nhl",
    "player_shots_on_goal",
    "player_goals",
    "player_assists_nhl",
    "player_saves",
    # NFL
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_rec_yds",
    "player_receptions",
    "player_touchdowns",
    "player_interceptions",
}


# ─────────────────────────────────────────────────────────────────────
# UNIFIED PROP CASCADE — merge all sources
# ─────────────────────────────────────────────────────────────────────

async def scrape_all_props(sport: str) -> dict:
    """
    Full free prop cascade: DK → FanDuel → BetMGM.
    Runs all three in parallel, merges results.

    Returns unified prop data with multi-book coverage per player/market/line.
    """
    # Run scrapers concurrently (BetMGM disabled — redundant with odds-api.io Pro)
    dk_task = asyncio.create_task(scrape_dk_props(sport))
    fd_task = asyncio.create_task(scrape_fd_props(sport))

    dk_result, fd_result = await asyncio.gather(
        dk_task, fd_task, return_exceptions=True
    )

    all_props = []
    sources_ok = []

    for name, result in [("dk", dk_result), ("fd", fd_result)]:
        if isinstance(result, Exception):
            logger.warning(f"{name} prop scrape raised: {result}")
            continue
        if result.get("error"):
            logger.warning(f"{name} prop scrape error: {result['error']}")
            continue
        props = result.get("props", [])
        if props:
            all_props.extend(props)
            sources_ok.append(name)

    # Build summary by player and market
    player_markets = {}
    for p in all_props:
        key = f"{p['player']}|{p['market']}|{p['line']}"
        if key not in player_markets:
            player_markets[key] = {"books": set()}
        player_markets[key]["books"].add(p["book"])

    multi_book_count = sum(1 for pm in player_markets.values() if len(pm["books"]) >= 2)

    logger.info(
        f"Prop cascade {sport}: {len(all_props)} total lines from {sources_ok}, "
        f"{len(player_markets)} unique player/market/lines, "
        f"{multi_book_count} with 2+ books"
    )

    return {
        "sport": sport,
        "props": all_props,
        "prop_count": len(all_props),
        "sources": sources_ok,
        "unique_player_markets": len(player_markets),
        "multi_book_count": multi_book_count,
        "source": "free_prop_cascade",
    }


# ─────────────────────────────────────────────────────────────────────
# DATABASE STORAGE
# ─────────────────────────────────────────────────────────────────────

PROP_SCHEMA_SQL = """
-- Player prop snapshots — one row per book/player/market/line/side/timestamp
CREATE TABLE IF NOT EXISTS prop_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    home_team TEXT,
    away_team TEXT,
    player TEXT NOT NULL,
    market TEXT NOT NULL,
    line REAL NOT NULL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    price_american INTEGER NOT NULL,
    snapshot_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prop_snap_player
    ON prop_snapshots(player, market, line, book, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_sport_time
    ON prop_snapshots(sport, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_event
    ON prop_snapshots(event_id, market, snapshot_time);
-- Single-column time index for retention pruning. Without this, a
-- `DELETE FROM prop_snapshots WHERE snapshot_time < ?` cannot use the
-- compound (sport, snapshot_time) index (sport is the leading column)
-- and degrades to a full-table scan (observed 45s on 522k rows).
CREATE INDEX IF NOT EXISTS idx_prop_snap_time
    ON prop_snapshots(snapshot_time);
"""


async def ensure_prop_schema(db_path: str = DB_PATH) -> None:
    """Create prop_snapshots table if it doesn't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (s.strip() for s in PROP_SCHEMA_SQL.split(";") if s.strip()):
            await db.execute(stmt)
        await db.commit()
    logger.info("Prop schema ensured")


async def store_prop_snapshot(props: list[dict], sport: str, db_path: str = DB_PATH) -> int:
    """
    Store a batch of prop lines into the database.

    Args:
        props: List of prop dicts from scrape_all_props()
        sport: Sport key
        db_path: Database path

    Returns:
        Number of rows inserted
    """
    if not props:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            sport,
            p.get("event_id", ""),
            p.get("home_team", ""),
            p.get("away_team", ""),
            p["player"],
            p["market"],
            p["line"],
            p["side"],
            p["book"],
            p["price"],
            now,
        )
        for p in props
    ]

    # WriteCoordinator path (single-writer pattern). Avoids opening a new
    # connection for every snapshot batch.
    #
    # CHUNKING (2026-04-18): the prop scraper dumps tens of thousands of rows
    # in one call (e.g., a full round of NBA player props across 4 books is
    # 20-40k rows). A single executemany of that size blocks the coordinator's
    # writer loop for 25-40s wall-clock, which blocks every other producer
    # behind it (hermes learnings, hypothesis promotions, /task POST). Break
    # the batch into CHUNK_SIZE-row sub-batches so the queue drains between
    # chunks. 5000 rows at ~1ms/row ≈ 200ms per chunk — plenty of room for
    # small writes to slip in. Same strategy for the legacy fallback path.
    CHUNK_SIZE = 5000
    sql = (
        "INSERT INTO prop_snapshots "
        "(sport, event_id, home_team, away_team, player, market, line, side, book, price_american, snapshot_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    try:
        from tools.db_writer import get_writer_if_running
        coord = get_writer_if_running(db_path)
    except Exception:
        coord = None
    if coord is not None:
        for start in range(0, len(rows), CHUNK_SIZE):
            await coord.executemany(sql, rows[start:start + CHUNK_SIZE])
        logger.info(
            f"Stored {len(rows)} prop snapshot rows for {sport} "
            f"({(len(rows) + CHUNK_SIZE - 1) // CHUNK_SIZE} chunk(s))"
        )
        return len(rows)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        for start in range(0, len(rows), CHUNK_SIZE):
            await db.executemany(sql, rows[start:start + CHUNK_SIZE])
            await db.commit()

    logger.info(f"Stored {len(rows)} prop snapshot rows for {sport}")
    return len(rows)


# ─────────────────────────────────────────────────────────────────────
# CONVERSION TO PROP_SCANNER FORMAT
# ─────────────────────────────────────────────────────────────────────

def props_to_scanner_format(props: list[dict]) -> dict:
    """
    Convert flat prop list to the format expected by prop_scanner.scan_props_ev().

    This bridges the free scrapers with the existing EV detection pipeline.
    The prop_scanner expects data shaped like The Odds API's player props response:
    {
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 24.5, "description": "LeBron James"},
                        ]
                    }
                ]
            }
        ]
    }
    """
    # Group by book -> market -> outcomes
    books = {}
    for p in props:
        book_key = p["book"]
        if book_key not in books:
            books[book_key] = {}

        market_key = p["market"]
        if market_key not in books[book_key]:
            books[book_key][market_key] = []

        books[book_key][market_key].append({
            "name": p["side"],
            "price": p["price"],
            "point": p["line"],
            "description": p["player"],
        })

    book_titles = {
        "draftkings": "DraftKings",
        "fanduel": "FanDuel",
        "betmgm": "BetMGM",
    }

    bookmakers = []
    for book_key, markets_dict in books.items():
        markets = []
        for mkt_key, outcomes in markets_dict.items():
            markets.append({
                "key": mkt_key,
                "outcomes": outcomes,
            })
        bookmakers.append({
            "key": book_key,
            "title": book_titles.get(book_key, book_key),
            "markets": markets,
        })

    return {"bookmakers": bookmakers}


# ─────────────────────────────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────────────────────────────

async def close_clients() -> None:
    """Close all HTTP clients."""
    await close_fd_client()
    close_mgm_session()
    close_shared_sessions()
