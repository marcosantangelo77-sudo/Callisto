"""Public multi-source fetch API + persistence for news events."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import aiosqlite

from tools.ingestion_tracking import tracked_ingestion
from tools.news.dedup import dedupe_injuries
from tools.news.espn import (
    fetch_espn_coaching,
    fetch_espn_injuries,
    fetch_espn_scoreboard_lineups,
)
from tools.news.models import CoachingEvent, InjuryEvent

logger = logging.getLogger("callisto.news_ingestion")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


async def fetch_injuries(sport: str) -> list[dict]:
    """Multi-source injury fetch, deduped. Returns schema-shaped rows.

    Row keys are the columns of ``news_events`` — caller can ``executemany``
    them directly. Never raises: partial-source failures are logged and
    surfaced as ``ingestion_runs`` rows via the decorator layer.
    """
    results: list[InjuryEvent] = []

    # Launch both primary sources concurrently. Any one failing still lets
    # the other contribute rows (we dedupe afterwards regardless).
    primary = fetch_espn_injuries(sport)
    secondary = _fetch_rotowire_news_compat(sport)
    got = await asyncio.gather(primary, secondary, return_exceptions=True)
    for g in got:
        if isinstance(g, Exception):
            logger.info(f"Injury source errored: {g}")
            continue
        if isinstance(g, list):
            results.extend(g)

    return dedupe_injuries(results)


def _fetch_rotowire_news_compat(sport: str):
    # Imported lazily so importing this module doesn't pull the scraper's
    # regex table into every consumer; behaviour is identical.
    from tools.news.rotowire import fetch_rotowire_news
    return fetch_rotowire_news(sport)


async def fetch_lineup_changes(sport: str, date: Optional[str] = None) -> list[dict]:
    """Late scratches + surprise lineups for a date.

    For now this routes through ESPN's scoreboard late-scratch signal. The
    RotoWire lineup parser is a follow-up once we have confirmed the ESPN
    base-rate is the critical signal (it usually is)."""
    try:
        events = await fetch_espn_scoreboard_lineups(sport, date)
    except Exception as e:
        logger.warning(f"fetch_lineup_changes error: {e}")
        return []
    return [ev.as_news_row() for ev in events]


async def fetch_coaching_news(sport: str, date: Optional[str] = None) -> list[dict]:
    """Coaching decisions likely to affect lines (rest days, mop-up lineups)."""
    try:
        events: list[CoachingEvent] = await fetch_espn_coaching(sport, date)
    except Exception as e:
        logger.warning(f"fetch_coaching_news error: {e}")
        return []
    rows: list[dict] = []
    for ev in events:
        # Team-level row only — no affected_players resolved in this v1.
        row = ev.as_news_row()
        row["player_name"] = None
        rows.append(row)
    return rows


async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Best-effort schema check: if ``news_events`` is missing, create it.

    The canonical schema home is migration 012, but tests and the smoke path
    build throwaway DBs — they hit this helper to ensure the table is there
    without running the migration runner.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            event_id TEXT,
            player_name TEXT,
            event_type TEXT,
            severity TEXT,
            body_part TEXT,
            status TEXT,
            first_seen_at TIMESTAMP,
            confirmed_at TIMESTAMP,
            source TEXT,
            source_url TEXT,
            raw_json TEXT,
            local_game_date DATE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_sport_date "
        "ON news_events(sport, local_game_date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_player "
        "ON news_events(player_name, first_seen_at DESC)"
    )
    await db.commit()


_NEWS_COLUMNS = (
    "sport", "event_id", "player_name", "event_type", "severity",
    "body_part", "status", "first_seen_at", "confirmed_at", "source",
    "source_url", "raw_json", "local_game_date",
)


async def persist_news_rows(
    rows: list[dict],
    db_path: Optional[str] = None,
) -> int:
    """Write rows to ``news_events``. Returns number inserted.

    Idempotency: a row is treated as a duplicate of an existing news_events
    entry if there is a row with the same ``(sport, player_name, body_part,
    event_type)`` whose ``first_seen_at`` is within the last 6 hours. This
    prevents the 5-min poller from inserting the same headline 12 times per
    hour. Dedup-across-sources happens BEFORE this call (in
    ``dedupe_injuries``); this is the time-window dedupe.
    """
    if not rows:
        return 0
    path = db_path or DB_PATH
    inserted = 0
    async with aiosqlite.connect(path) as db:
        await ensure_schema(db)
        for row in rows:
            # Within-window dedup: same sport+player+body_part+event_type in last 6h?
            dup = await db.execute(
                """
                SELECT id FROM news_events
                WHERE sport IS ?
                  AND COALESCE(player_name, '') = COALESCE(?, '')
                  AND COALESCE(body_part, '') = COALESCE(?, '')
                  AND event_type = ?
                  AND first_seen_at > datetime('now', '-6 hours')
                LIMIT 1
                """,
                (row.get("sport"), row.get("player_name"),
                 row.get("body_part"), row.get("event_type")),
            )
            if await dup.fetchone():
                continue
            values = tuple(row.get(c) for c in _NEWS_COLUMNS)
            await db.execute(
                f"INSERT INTO news_events ({', '.join(_NEWS_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_NEWS_COLUMNS))})",
                values,
            )
            inserted += 1
        await db.commit()
    return inserted
