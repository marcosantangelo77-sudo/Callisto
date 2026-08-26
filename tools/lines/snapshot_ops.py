"""
Snapshot persistence operations for the line monitor.

Extracted from tools/line_monitor.py so the monitor class stays a thin
orchestrator. Each helper takes its collaborators explicitly (db handle,
alert sink, CLV tracker module) and owns no state:

- insert_snapshot_record — odds_snapshots row write with retry
- cache_snapshot_for_backtest — historical_odds_cache upsert
- store_market_microstructure — HHI/entropy metrics from an edge report
- record_line_movement — line_movements row + alert-sink append
- capture_closing_lines — CLV bridge for games about to start

All functions are async and use tools.db_utils retry wrappers, matching
the behavior they were extracted from.
"""

import json
import logging
from datetime import datetime, timezone

from tools.db_utils import execute_with_retry, commit_with_retry

logger = logging.getLogger("callisto.line_monitor.snapshot_ops")


async def insert_snapshot_record(
    db,
    *,
    sport: str,
    snapshot: dict,
    now_iso: str,
    game_count: int,
    credits_remaining,
    ingest_source: str,
) -> None:
    """Insert one row into odds_snapshots.

    Uses execute/commit with retry because the autonomous loop does NOT
    acquire the write lock, so SQLite-level contention can occur.
    """
    await execute_with_retry(
        db,
        "INSERT INTO odds_snapshots "
        "(sport, timestamp, snapshot_json, game_count, credits_remaining, "
        "fetched_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sport, now_iso, json.dumps(snapshot), game_count, credits_remaining,
         now_iso, ingest_source),
        max_retries=10,
        operation="snapshot_insert",
    )
    await commit_with_retry(db, max_retries=10, operation="snapshot_store")


async def cache_snapshot_for_backtest(db, *, sport: str, snapshot: dict, now_iso: str) -> int:
    """Archive a snapshot in historical_odds_cache for backtesting.

    Every live snapshot becomes backtest-grade data — even single-book
    snapshots provide game context and can be cross-referenced with other
    snapshots from the same date. Returns the max book count seen across
    games (0 when there are no games).
    """
    book_count = 0
    for g in snapshot.get("games", []):
        book_count = max(book_count, len(g.get("bookmakers", [])))
        if not g.get("sport_key"):
            g["sport_key"] = sport
    game_count = snapshot.get("game_count", 0)
    if game_count <= 0:
        return book_count
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        await execute_with_retry(
            db,
            "INSERT OR REPLACE INTO historical_odds_cache "
            "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, fetched_at) "
            "VALUES (?, ?, NULL, 'h2h,spreads,totals', ?, 0, ?)",
            (sport, today, json.dumps(snapshot), now_iso),
            max_retries=10,
            operation=f"historical_odds_cache insert {sport}",
        )
        await commit_with_retry(
            db,
            max_retries=10,
            operation=f"historical_odds_cache commit {sport}",
        )
        logger.info(f"Cached multi-book snapshot for backtest: {sport} {today} ({book_count} books)")
    except Exception as e:
        logger.warning(f"Failed to cache snapshot for backtest: {e}")
    return book_count


async def store_market_microstructure(db, *, sport: str, edge_report: dict, now_iso: str) -> int:
    """Persist HHI/entropy metrics from an edge scan into market_microstructure.

    Returns the number of rows stored. Failures are logged and swallowed —
    microstructure telemetry must never break the snapshot pipeline.
    """
    try:
        stored = 0
        for market_key in ["cross_book_h2h", "cross_book_spreads", "cross_book_totals"]:
            edges = edge_report.get(market_key, [])
            for edge in edges:
                hhi_val = edge.get("hhi")
                entropy_val = edge.get("entropy")
                if hhi_val is not None or entropy_val is not None:
                    await execute_with_retry(
                        db,
                        "INSERT OR REPLACE INTO market_microstructure "
                        "(sport, game_id, market_type, timestamp, hhi_overall, "
                        "entropy_overall, num_books) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            sport,
                            edge.get("game_id", ""),
                            market_key.replace("cross_book_", ""),
                            now_iso,
                            hhi_val,
                            entropy_val,
                            edge.get("num_bookmakers", 0),
                        ),
                        max_retries=5,
                        operation="microstructure_insert",
                    )
                    stored += 1
        if stored > 0:
            await commit_with_retry(db, max_retries=5, operation="microstructure_store")
            logger.info(f"Stored {stored} microstructure metrics for {sport}")
        return stored
    except Exception as e:
        logger.warning(f"Market microstructure store failed: {e}")
        return 0


async def record_line_movement(db, alerts, *, sport: str, movement: dict) -> None:
    """Record one detected line movement to the database and the alert deque.

    `alerts` is any list-like sink (LineMonitor uses a bounded deque).
    """
    now = datetime.now(timezone.utc).isoformat()
    await execute_with_retry(
        db,
        "INSERT INTO line_movements "
        "(sport, detected_at, team, market, bookmaker, old_price, new_price, "
        "price_movement, old_point, new_point, point_movement, direction) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sport, now, movement["team"], movement["market"],
            movement["bookmaker"], movement["old_price"], movement["new_price"],
            movement["price_movement"], movement.get("old_point"),
            movement.get("new_point"), movement.get("point_movement", 0),
            movement["direction"],
        ),
        max_retries=5,
        operation=f"line_movement insert {sport}",
    )
    await commit_with_retry(db, max_retries=5, operation=f"line_movement commit {sport}")

    alerts.append({
        "sport": sport,
        "detected_at": now,
        **movement,
    })
    # Keep only last 100 alerts in memory
    if len(alerts) > 100:
        alerts[:] = list(alerts)[-100:]


def normalize_close_source(book_name: str) -> str:
    """Normalize a bookmaker title to clv_tracker key style (lowercase, underscores).

    Odds-api-io returns titles like "Pinnacle", "Betfair Exchange",
    "BetOnline.ag"; without this every reliable book later tests as
    unreliable against _RELIABLE_CLOSE_SOURCES.
    """
    return (book_name or "").lower().replace(" ", "_")


async def capture_closing_lines(clv_tracker, *, sport: str, snapshot: dict, closing_window_seconds: float) -> int:
    """Push closing lines to a CLV tracker for games about to start.

    For each game starting within `closing_window_seconds`, extract the
    consensus/sharp closing line per bookmaker/market/outcome and record it.
    Bridges the line_monitor → CLV tracker gap that was previously dead code.

    Returns the number of lines recorded.
    """
    now = datetime.now(timezone.utc)
    closing_count = 0
    games = snapshot.get("games", [])

    for game in games:
        commence_time_str = game.get("commence_time", "")
        if not commence_time_str:
            continue

        try:
            commence = datetime.fromisoformat(
                commence_time_str.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            continue

        seconds_until_start = (commence - now).total_seconds()

        # Game starts within closing window and hasn't already started
        if 0 < seconds_until_start <= closing_window_seconds:
            event_id = game.get("id", "")

            # Extract odds from all bookmakers for each market
            for bm in game.get("bookmakers", []):
                book_name = bm.get("title", bm.get("key", ""))
                for market_data in bm.get("markets", []):
                    market_key = market_data.get("key", "")
                    for outcome in market_data.get("outcomes", []):
                        team = outcome.get("name", "")
                        price = outcome.get("price")
                        point = outcome.get("point")

                        if price is None:
                            continue

                        src_key = normalize_close_source(book_name)
                        try:
                            await clv_tracker.record_closing_line(
                                event_id=event_id,
                                market=market_key,
                                team=team,
                                closing_odds=int(price),
                                closing_point=float(point) if point is not None else None,
                                source=src_key,
                                sport=sport,
                            )
                            closing_count += 1
                        except Exception as e:
                            logger.debug(f"CLV closing line record failed: {e}")

    if closing_count > 0:
        logger.info(
            f"CLV: captured {closing_count} closing lines for {sport} "
            f"(games starting within {closing_window_seconds}s)"
        )
    return closing_count


def default_closing_window(snapshot_interval: int) -> float:
    """Closing-line lookahead window: interval + buffer, at least 1 hour."""
    return max(snapshot_interval + 300, 3600)
