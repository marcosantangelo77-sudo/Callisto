"""
news_loop — background coroutines for periodic news / injury ingestion.

Follows the pattern of ``prop_resolver.prop_resolution_loop``: designed to
be scheduled by the API's FastAPI lifespan in a later integration PR (not
part of this worktree's deliverable).

Cadences
--------
* Injuries: every 5 minutes per sport
* Lineups: every 15 minutes per sport (these are noisy pre-game and quiet
  otherwise, so a tighter loop on the approach to tip is a TODO)
* Coaching: every 30 minutes (very rare signal)
* Impact scoring: every 5 minutes, scans the last 60min of news_events

All loops:
  * Wrap each iteration in try/except so one bad fetch doesn't kill the loop.
  * Sleep between iterations (not on entry) so the first call happens
    immediately on coroutine creation.
  * Respect CancelledError for orderly lifespan shutdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable, Optional

from tools.news_ingestion import (
    fetch_coaching_news,
    fetch_injuries,
    fetch_lineup_changes,
    persist_news_rows,
)
from tools.news_impact import process_news_events

logger = logging.getLogger("callisto.news_loop")

DEFAULT_SPORTS = (
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "americanfootball_nfl",
)

DEFAULT_INJURY_INTERVAL_S = 300     # 5min
DEFAULT_LINEUP_INTERVAL_S = 900     # 15min
DEFAULT_COACHING_INTERVAL_S = 1800  # 30min
DEFAULT_IMPACT_INTERVAL_S = 300     # 5min


async def _run_once_injuries(sports: Iterable[str], db_path: Optional[str]) -> dict:
    counts: dict[str, int] = {}
    for sport in sports:
        try:
            rows = await fetch_injuries(sport)
            if rows:
                inserted = await persist_news_rows(rows, db_path=db_path)
                counts[sport] = inserted
        except Exception as e:
            logger.warning(f"injury fetch {sport} error: {e}", exc_info=False)
    return counts


async def _run_once_lineups(sports: Iterable[str], db_path: Optional[str]) -> dict:
    counts: dict[str, int] = {}
    for sport in sports:
        try:
            rows = await fetch_lineup_changes(sport)
            if rows:
                inserted = await persist_news_rows(rows, db_path=db_path)
                counts[sport] = inserted
        except Exception as e:
            logger.warning(f"lineup fetch {sport} error: {e}", exc_info=False)
    return counts


async def _run_once_coaching(sports: Iterable[str], db_path: Optional[str]) -> dict:
    counts: dict[str, int] = {}
    for sport in sports:
        try:
            rows = await fetch_coaching_news(sport)
            if rows:
                inserted = await persist_news_rows(rows, db_path=db_path)
                counts[sport] = inserted
        except Exception as e:
            logger.warning(f"coaching fetch {sport} error: {e}", exc_info=False)
    return counts


async def news_injury_loop(
    db_path: Optional[str] = None,
    sports: Iterable[str] = DEFAULT_SPORTS,
    interval_seconds: int = DEFAULT_INJURY_INTERVAL_S,
) -> None:
    """Background loop: poll injuries every ``interval_seconds``."""
    logger.info(f"news_injury_loop start (interval={interval_seconds}s)")
    while True:
        try:
            result = await _run_once_injuries(sports, db_path)
            if any(result.values()):
                logger.info(f"news_injury_loop: inserted {json.dumps(result)}")
        except asyncio.CancelledError:
            logger.info("news_injury_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"news_injury_loop iteration error: {e}", exc_info=True)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def news_lineup_loop(
    db_path: Optional[str] = None,
    sports: Iterable[str] = DEFAULT_SPORTS,
    interval_seconds: int = DEFAULT_LINEUP_INTERVAL_S,
) -> None:
    """Background loop: poll lineups every ``interval_seconds``."""
    logger.info(f"news_lineup_loop start (interval={interval_seconds}s)")
    while True:
        try:
            result = await _run_once_lineups(sports, db_path)
            if any(result.values()):
                logger.info(f"news_lineup_loop: inserted {json.dumps(result)}")
        except asyncio.CancelledError:
            logger.info("news_lineup_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"news_lineup_loop iteration error: {e}", exc_info=True)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def news_coaching_loop(
    db_path: Optional[str] = None,
    sports: Iterable[str] = DEFAULT_SPORTS,
    interval_seconds: int = DEFAULT_COACHING_INTERVAL_S,
) -> None:
    logger.info(f"news_coaching_loop start (interval={interval_seconds}s)")
    while True:
        try:
            result = await _run_once_coaching(sports, db_path)
            if any(result.values()):
                logger.info(f"news_coaching_loop: inserted {json.dumps(result)}")
        except asyncio.CancelledError:
            logger.info("news_coaching_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"news_coaching_loop iteration error: {e}", exc_info=True)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def news_impact_loop(
    db_path: Optional[str] = None,
    interval_seconds: int = DEFAULT_IMPACT_INTERVAL_S,
    since_minutes: int = 60,
) -> None:
    """Background loop: score recent news_events against odds movements."""
    logger.info(f"news_impact_loop start (interval={interval_seconds}s)")
    while True:
        try:
            report = await process_news_events(
                db_path=db_path,
                since_minutes=since_minutes,
            )
            if report.get("emitted") or report.get("under_reactions"):
                logger.info(f"news_impact_loop: {json.dumps(report)}")
        except asyncio.CancelledError:
            logger.info("news_impact_loop cancelled")
            raise
        except Exception as e:
            logger.error(f"news_impact_loop iteration error: {e}", exc_info=True)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


__all__ = [
    "news_injury_loop",
    "news_lineup_loop",
    "news_coaching_loop",
    "news_impact_loop",
    "DEFAULT_SPORTS",
    "DEFAULT_INJURY_INTERVAL_S",
    "DEFAULT_LINEUP_INTERVAL_S",
    "DEFAULT_COACHING_INTERVAL_S",
    "DEFAULT_IMPACT_INTERVAL_S",
]
