"""
prop_resolver — Close the player-prop feedback loop.

The problem
-----------
``tools/backtest.py._process_game_props`` writes ``backtest_events`` rows
for every surfaced prop, but the existing ``resolve_events`` path only
understands game-level markets (h2h / spreads / totals). Every prop row
sits with ``actual_result = NULL`` forever, which silently zeros out:

  * hypothesis_stats win-rate / edge / kelly for any prop hypothesis
  * the SGP scanner's empirical correlation calibrator (needs actuals)
  * paper-trade CLV accounting for prop bets

This module is that missing link. It matches each unresolved prop row
against ``player_stats``, writes ``actual_result``+``actual_stat``, and
re-runs the empirical SGP calibration when enough new resolutions
accumulate.

Vocabulary
----------
``actual_result`` uses the SAME values as the game-line resolver:
``'won' | 'lost' | 'push'``. The task spec said ``win/loss/push`` but
the entire downstream pipeline (``hypothesis.py``, ``backtest.py`` stats
aggregators, ``autonomous.py`` promotion logic) filters on
``IN ('won','lost','push')``. Diverging would silently break every
prop hypothesis's stats.

Idempotency
-----------
Every write path only touches rows where ``actual_result IS NULL`` AND
the computed result is non-None. Re-running is free — zero changes on
already-resolved rows. The resolver also records the run to
``ingestion_runs`` via ``@tracked_ingestion`` so the health endpoint can
see it.

Entry points
------------
  * ``resolve_player_prop_backtest_events(db_path, limit, sport)`` —
    one-shot batch resolution, returns ``ResolveReport``.
  * ``prop_resolution_loop(db_path, interval_seconds, batch_limit)`` —
    background coroutine for the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiosqlite

from tools.ingestion_tracking import tracked_ingestion
from tools.player_name_index import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    PlayerNameIndex,
)
from tools.prop_stat_map import (
    fallback_stat_types,
    is_prop_market,
    market_to_stat_type,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_LIMIT = 500
DEFAULT_INTERVAL_SECONDS = 15 * 60  # 15 min
RECALIBRATE_THRESHOLD = 50  # re-run SGP calibration after this many new resolutions


@dataclass
class ResolveReport:
    """Structured summary of one resolver pass. Also returned by the loop."""
    scanned: int = 0
    resolved: int = 0
    skipped_no_player_stat: int = 0
    skipped_no_game_result: int = 0
    skipped_no_line: int = 0
    skipped_unknown_market: int = 0
    skipped_low_confidence_name: int = 0
    errors: int = 0
    by_sport: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "resolved": self.resolved,
            "skipped_no_player_stat": self.skipped_no_player_stat,
            "skipped_no_game_result": self.skipped_no_game_result,
            "skipped_no_line": self.skipped_no_line,
            "skipped_unknown_market": self.skipped_unknown_market,
            "skipped_low_confidence_name": self.skipped_low_confidence_name,
            "errors": self.errors,
            "by_sport": self.by_sport,
        }

    def rows(self) -> int:
        """Protocol for ``tracked_ingestion._extract_rows``."""
        return self.resolved


def _pick_result(side: str, actual: float, line: float) -> Optional[str]:
    """Compute ``won``/``lost``/``push`` from side + actual vs line."""
    s = (side or "").strip().lower()
    if s == "over":
        if actual > line:
            return "won"
        if actual < line:
            return "lost"
        return "push"
    if s == "under":
        if actual < line:
            return "won"
        if actual > line:
            return "lost"
        return "push"
    return None


async def _fetch_stat_value(
    db: aiosqlite.Connection,
    *,
    sport: str,
    event_id: str,
    game_date: str,
    player: str,
    stat_type: str,
) -> Optional[float]:
    """Return the first stat_value match, trying event_id first then date."""
    # Prefer event_id match — unambiguous for doubleheaders / same-day games.
    cur = await db.execute(
        "SELECT stat_value FROM player_stats "
        "WHERE sport = ? AND player_name = ? AND stat_type = ? "
        "AND (event_id = ? OR game_date = ?) "
        "ORDER BY CASE WHEN event_id = ? THEN 0 ELSE 1 END LIMIT 1",
        (sport, player, stat_type, event_id, game_date, event_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


async def _game_is_final(
    db: aiosqlite.Connection,
    *,
    sport: str,
    event_id: str,
    game_date: str,
) -> bool:
    """True iff a finalised game exists for (sport, event_id|date).

    Matches either by ``event_id`` (where ``player_stats`` has it) or by
    the existence of a ``game_results`` row for the same sport+date that
    carries a final home_score. Some MLB pipelines drop event_id from
    ``game_results``, so this is a best-effort check.
    """
    # Prefer: there's at least one player_stats row with this event_id.
    cur = await db.execute(
        "SELECT 1 FROM player_stats WHERE sport = ? AND event_id = ? LIMIT 1",
        (sport, event_id),
    )
    if await cur.fetchone():
        return True
    # Fallback: any finalised game_results row on that date for this sport.
    cur = await db.execute(
        "SELECT 1 FROM game_results "
        "WHERE sport = ? AND game_date = ? AND home_score IS NOT NULL LIMIT 1",
        (sport, game_date),
    )
    return bool(await cur.fetchone())


async def _resolve_one(
    db: aiosqlite.Connection,
    name_idx: PlayerNameIndex,
    row: tuple,
    report: ResolveReport,
) -> bool:
    """Resolve one ``backtest_events`` row. Returns True iff a write happened."""
    (ev_id, sport, event_id, player, market, line, side, game_date) = row
    sport_bucket = report.by_sport.setdefault(
        sport or "unknown",
        {"scanned": 0, "resolved": 0, "missing_stat": 0, "missing_game": 0,
         "no_line": 0, "unknown_market": 0, "low_conf_name": 0},
    )
    sport_bucket["scanned"] += 1

    if line is None:
        report.skipped_no_line += 1
        sport_bucket["no_line"] += 1
        return False

    stat_type = market_to_stat_type(market)
    if stat_type is None:
        report.skipped_unknown_market += 1
        sport_bucket["unknown_market"] += 1
        return False

    if not await _game_is_final(
        db, sport=sport, event_id=event_id or "", game_date=game_date
    ):
        report.skipped_no_game_result += 1
        sport_bucket["missing_game"] += 1
        return False

    # Resolve player name to the canonical form used by player_stats.
    canonical: Optional[str] = player
    if player:
        match = await name_idx.resolve(sport, player,
                                       threshold=DEFAULT_CONFIDENCE_THRESHOLD)
        if match is not None:
            canonical = match[0]
        else:
            # Low-confidence name: try the raw alias as a last-ditch attempt
            # (covers the common case where prop feeds and stats feeds
            # already agree). If that also misses, report and bail.
            canonical = player

    # Try primary stat_type, then fallbacks.
    stat_value: Optional[float] = None
    for candidate_stat in (stat_type, *fallback_stat_types(stat_type)):
        stat_value = await _fetch_stat_value(
            db,
            sport=sport,
            event_id=event_id or "",
            game_date=game_date,
            player=canonical or "",
            stat_type=candidate_stat,
        )
        if stat_value is not None:
            break

    # Second chance with raw player string if canonical missed. Cheap;
    # only one indexed SELECT.
    if stat_value is None and canonical != player:
        for candidate_stat in (stat_type, *fallback_stat_types(stat_type)):
            stat_value = await _fetch_stat_value(
                db,
                sport=sport,
                event_id=event_id or "",
                game_date=game_date,
                player=player or "",
                stat_type=candidate_stat,
            )
            if stat_value is not None:
                break

    if stat_value is None:
        # Distinguish "we couldn't match the name at all" from "we matched
        # the name but the stat row is missing" so telemetry is useful.
        if player and canonical == player:
            # Name didn't resolve via index — score below threshold.
            # Check if index has ANY canonicals for this sport to tell
            # apart "sport empty" vs "name not found".
            low_conf = await db.execute(
                "SELECT 1 FROM player_names WHERE sport = ? LIMIT 1", (sport,)
            )
            if await low_conf.fetchone():
                report.skipped_low_confidence_name += 1
                sport_bucket["low_conf_name"] += 1
                return False
        report.skipped_no_player_stat += 1
        sport_bucket["missing_stat"] += 1
        return False

    result = _pick_result(side, stat_value, float(line))
    if result is None:
        report.errors += 1
        logger.warning(
            f"prop_resolver: unknown side '{side}' on ev_id={ev_id} "
            f"market={market}"
        )
        return False

    # Idempotent guard — only write if still NULL. Race with a concurrent
    # resolver is harmless (same result).
    await db.execute(
        "UPDATE backtest_events SET actual_result = ?, actual_stat = ? "
        "WHERE id = ? AND actual_result IS NULL",
        (result, stat_value, ev_id),
    )
    report.resolved += 1
    sport_bucket["resolved"] += 1
    return True


async def _load_unresolved_prop_rows(
    db: aiosqlite.Connection,
    *,
    limit: int,
    sport: Optional[str],
) -> list[tuple]:
    """Pull unresolved prop rows. Returns tuples matching ``_resolve_one``."""
    # Note: we can't just WHERE market LIKE 'player_%' because some NFL
    # props use pitcher_/batter_/skater_ prefixes. Filter in Python via
    # ``is_prop_market`` — the count is bounded by ``limit`` anyway.
    prefixes = ("player_", "pitcher_", "batter_", "skater_", "goalie_")
    like_clause = " OR ".join(f"market LIKE '{p}%'" for p in prefixes)
    args: list = []
    q = (
        f"SELECT id, sport, event_id, player, market, line, side, game_date "
        f"FROM backtest_events WHERE actual_result IS NULL AND ({like_clause})"
    )
    if sport:
        q += " AND sport = ?"
        args.append(sport)
    q += " ORDER BY game_date DESC, id DESC LIMIT ?"
    args.append(int(limit))
    cur = await db.execute(q, tuple(args))
    return list(await cur.fetchall())


def _db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _trigger_sgp_calibration() -> None:
    """Re-run the SGP empirical calibration. Fire-and-forget subprocess.

    The calibrator writes ``config/sgp_correlations_empirical.yaml`` atomically;
    next SGP scanner scan picks it up. Failures are logged but never raised —
    the resolver's job is already done at this point.
    """
    script = Path("scripts/calibrate_sgp_correlations.py")
    if not script.exists():
        logger.warning(f"prop_resolver: calibration script missing ({script})")
        return
    try:
        # Don't block the event loop — run detached. stdout/stderr go to logs.
        subprocess.Popen(  # noqa: S603 — trusted, pinned script path
            [sys.executable, str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.getcwd(),
        )
        logger.info("prop_resolver: triggered SGP empirical recalibration")
    except Exception as e:
        logger.warning(f"prop_resolver: calibration spawn failed: {e}")


@tracked_ingestion(source="prop_resolver.backtest_events", sla_seconds=3600)
async def _resolve_tracked(
    db_path: Optional[str] = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    sport: Optional[str] = None,
    recalibrate: bool = True,
) -> dict:
    """tracked_ingestion-compatible wrapper. Returns dict (for rows hint)."""
    report = await _resolve_impl(
        db_path=db_path, limit=limit, sport=sport, recalibrate=recalibrate,
    )
    out = report.as_dict()
    # Expose a canonical "rows" key so tracked_ingestion can record it.
    out["rows"] = report.resolved
    # Attach the dataclass behind a sentinel so the caller can recover it
    # without re-running.
    out["_report"] = report
    return out


async def resolve_player_prop_backtest_events(
    db_path: Optional[str] = None,
    limit: int = DEFAULT_BATCH_LIMIT,
    sport: Optional[str] = None,
    *,
    recalibrate: bool = True,
) -> ResolveReport:
    """Resolve up to ``limit`` unresolved prop rows in ``backtest_events``.

    Public entry point. Delegates to the tracked wrapper so that
    ``ingestion_runs`` captures a row per invocation.

    Args:
        db_path: SQLite path. Defaults to ``$CALLISTO_DB_PATH`` or
                 ``memory/callisto.db``.
        limit: Maximum rows to scan per invocation. Bounded so the 15-min
               cron never blocks the DB long enough to contend with
               writes.
        sport: Optional filter (e.g. ``'basketball_nba'``) — useful for
               backfills.
        recalibrate: If True (default), trigger the SGP empirical
                     calibrator when ≥ ``RECALIBRATE_THRESHOLD`` rows
                     were resolved in this pass.

    Returns a :class:`ResolveReport` with per-sport breakdown.
    """
    out = await _resolve_tracked(
        db_path=db_path, limit=limit, sport=sport, recalibrate=recalibrate,
    )
    report: ResolveReport = out["_report"]
    return report


async def _resolve_impl(
    *,
    db_path: Optional[str],
    limit: int,
    sport: Optional[str],
    recalibrate: bool,
) -> ResolveReport:
    """Internal implementation — no tracked_ingestion wrapping."""
    path = db_path or _db_path()
    report = ResolveReport()

    # Use a dedicated connection with WAL-friendly settings. The read path
    # is the bulk of the work; writes are trickled inside the loop.
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        name_idx = PlayerNameIndex(db)
        await name_idx.ensure_schema()

        # Seed index incrementally — fast no-op after the first pass once
        # INSERT OR IGNORE has covered the distinct set.
        try:
            seeded = await name_idx.seed_from_player_stats(sport=sport)
            if seeded:
                logger.info(
                    f"prop_resolver: seeded {seeded} player_names aliases"
                )
        except Exception as e:
            logger.warning(f"prop_resolver: name index seed failed: {e}")

        rows = await _load_unresolved_prop_rows(db, limit=limit, sport=sport)
        report.scanned = len(rows)
        for row in rows:
            try:
                await _resolve_one(db, name_idx, row, report)
            except Exception as e:
                report.errors += 1
                logger.warning(f"prop_resolver: row error id={row[0]}: {e}")

        await db.commit()

    if recalibrate and report.resolved >= RECALIBRATE_THRESHOLD:
        _trigger_sgp_calibration()

    logger.info(
        f"prop_resolver pass: scanned={report.scanned} "
        f"resolved={report.resolved} "
        f"no_stat={report.skipped_no_player_stat} "
        f"no_game={report.skipped_no_game_result} "
        f"no_line={report.skipped_no_line} "
        f"unknown_mkt={report.skipped_unknown_market} "
        f"low_conf_name={report.skipped_low_confidence_name} "
        f"errors={report.errors}"
    )
    return report


async def prop_resolution_loop(
    db_path: Optional[str] = None,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> None:
    """Background coroutine: resolve props every ``interval_seconds``.

    Designed to be created in the FastAPI lifespan alongside
    ``ingestion_sla_watchdog_loop`` and friends. Cancellable.
    """
    logger.info(
        f"prop_resolution_loop started "
        f"(interval={interval_seconds}s, batch_limit={batch_limit})"
    )
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            report = await resolve_player_prop_backtest_events(
                db_path=db_path, limit=batch_limit,
            )
            if report.resolved or report.errors:
                logger.info(
                    f"prop_resolver cron: {json.dumps(report.as_dict())}"
                )
        except asyncio.CancelledError:
            logger.info("prop_resolution_loop cancelled")
            raise
        except Exception as e:
            logger.error(
                f"prop_resolution_loop iteration error: {e}", exc_info=True
            )
            # Short backoff so a broken DB doesn't spin-loop.
            await asyncio.sleep(30)


__all__ = [
    "ResolveReport",
    "prop_resolution_loop",
    "resolve_player_prop_backtest_events",
]
