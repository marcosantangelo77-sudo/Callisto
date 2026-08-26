"""Core LineMonitor machinery — slice-7 extraction.

Owns the last substantial blocks that used to live inline on
tools.line_monitor.LineMonitor:

- ``init_state``        — every attribute set by ``LineMonitor.__init__``
- ``start``             — startup snapshots, loop task spawn, WS/incremental bring-up
- ``stop``              — task teardown, WS/incremental shutdown, DB close
- ``handle_ws_update``  — update counters + detector isolation around ws_stream
- ``fetch_recent_movements`` / ``fetch_ev_opportunities`` /
  ``fetch_snapshot_history`` — thin DB queries re-exported for symmetry
- ``get_edge_report``   — latest-scan cache lookup
- ``force_snapshot``    — manual snapshot trigger returning cached result

The LineMonitor import path is unchanged; the class remains a facade of
one-line delegations. No betting logic here — nothing in this module can
place a wager or widen the paper-trade signal surface.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import aiosqlite

from tools.lines.monitor_loop import (
    fetch_ev_opportunities,
    fetch_recent_movements,
    fetch_snapshot_history,
)
from tools.lines.schema import connect_and_tag, ensure_line_schema
from tools.lines.ws_stream import (
    handle_ws_update as _handle_ws_update_impl,
    incremental_loop as _incremental_loop_impl,
    start_ws as _start_ws_impl,
    stop_ws_and_incremental as _stop_ws_and_incremental_impl,
)

logger = logging.getLogger("callisto.line_monitor")


def init_state(
    monitor,
    db_path: str,
    *,
    monitored_sports: list,
) -> None:
    """Set every attribute the original LineMonitor.__init__ established.

    Extracted verbatim so behavior is byte-identical; the facade's
    __init__ just forwards here.
    """
    monitor.db_path = db_path
    monitor._db = None
    monitor._task = None
    monitor._running = False
    # Set True to pause snapshot writes (during backtests)
    monitor._paused = False
    # Signals when monitor has entered paused state (no in-flight DB ops)
    monitor._pause_ack = asyncio.Event()
    # True while a snapshot DB write is in progress
    monitor._in_flight_db = False
    # Atomic guard around _process_snapshot
    monitor._snapshot_lock = asyncio.Lock()
    # sport -> last snapshot (only latest per sport)
    monitor._snapshots = {}
    # Hard-capped at 100 (was unbounded list)
    monitor._alerts = deque(maxlen=100)
    # sport -> latest edge scan (only latest per sport)
    monitor._latest_edge_reports = {}
    # Self-healing: track consecutive all-source failures per sport.
    # Alert via Telegram only after 3+ consecutive failures.
    monitor._consecutive_failures = {}
    monitor._FAILURE_ALERT_THRESHOLD = 3

    # Event-driven odds state (WS + incremental poll) -------------------
    # _ws_client holds the odds-api.io WebSocket handle; _incremental_task
    # holds the /odds/updated?since=X poller. Both are None when the
    # feature is disabled or failed to initialize; the 15-min snapshot
    # loop runs regardless so WS outages degrade gracefully.
    monitor._ws_client = None
    monitor._ws_task = None
    monitor._ws_updates_received = 0
    monitor._ws_last_update_at = None
    monitor._incremental_task = None
    # Unix seconds of the last /odds/updated poll, per sport. Used as
    # the `since` cursor on the next call.
    monitor._last_incremental_since = {}


async def initialize(monitor, *, ensure_prop_schema) -> None:
    """Create tables for odds snapshots and alerts.

    Delegates DDL to tools.lines.schema (per-statement DDL avoids
    EXCLUSIVE lock contention — security audit C-6).
    """
    monitor._db = await connect_and_tag(monitor.db_path)
    await ensure_line_schema(monitor._db)
    # Ensure prop_snapshots table exists
    await ensure_prop_schema(monitor.db_path)
    logger.info("Line monitor initialized (with prop snapshots)")


async def start(
    monitor,
    *,
    monitored_sports: list,
    snapshot_interval: int,
    ws_enabled: bool,
    incremental_enabled: bool,
    monitor_loop_fn,
    incremental_loop_fn=None,
) -> None:
    """Start the background monitoring loop.

    Takes immediate startup snapshots before the loop begins — the
    autonomous loop has a 15s startup delay and we want at least one
    round of fresh data before backtests pause us. Event-driven paths
    are non-blocking: failure to open WS must NOT prevent the safety
    loop from running.
    """
    if monitor._running:
        return
    monitor._running = True
    for sport in monitored_sports:
        try:
            await monitor._snapshot_sport(sport.strip())
        except Exception as e:
            logger.warning(f"Startup snapshot for {sport} failed: {e}")
    monitor._task = asyncio.create_task(monitor._monitor_loop())

    if ws_enabled:
        try:
            await monitor._start_ws()
        except Exception as e:
            logger.warning(f"WS startup failed (will retry in background): {e}")
    if incremental_enabled:
        loop_fn = incremental_loop_fn or monitor._incremental_loop
        monitor._incremental_task = asyncio.create_task(loop_fn())

    logger.info(
        f"Line monitor started — {len(monitored_sports)} sports, "
        f"{snapshot_interval}s interval "
        f"(ws={'on' if ws_enabled else 'off'}, "
        f"incremental={'on' if incremental_enabled else 'off'})"
    )


async def stop(monitor) -> None:
    """Stop the monitoring loop and close DB."""
    monitor._running = False
    if monitor._task:
        monitor._task.cancel()
        try:
            await monitor._task
        except asyncio.CancelledError:
            pass
    # Stop WS and incremental tasks (each teardown isolated — see ws_stream).
    await _stop_ws_and_incremental_impl(monitor)
    if monitor._db:
        await monitor._db.close()
    logger.info("Line monitor stopped")


async def handle_ws_update(
    monitor,
    data: dict,
    *,
    process_snapshot,
) -> None:
    """WS callback — merge a single delta into our latest snapshot.

    This wrapper owns the update counters and isolates detector
    failures from odds ingestion; the merge itself lives in
    tools.lines.ws_stream.handle_ws_update.
    """
    monitor._ws_updates_received += 1
    monitor._ws_last_update_at = time.time()
    try:

        async def _eval_detectors(event_id: str):
            from tools.live_state import evaluate_detectors_for_event
            await evaluate_detectors_for_event(event_id, db_path=monitor.db_path)

        await _handle_ws_update_impl(
            monitor, data,
            process_snapshot=process_snapshot,
            evaluate_live_detectors=_eval_detectors,
        )
    except Exception as e:
        logger.warning(f"WS update handler failed: {e}")


def get_edge_report(monitor, sport: Optional[str] = None) -> dict:
    """Get the latest edge scan report."""
    if sport:
        return monitor._latest_edge_reports.get(sport, {"error": f"No report for {sport}"})
    return monitor._latest_edge_reports


async def force_snapshot(monitor, sport: str, snapshot_sport) -> dict:
    """Manually trigger a snapshot for a sport. Returns the snapshot data."""
    await snapshot_sport(sport)
    return monitor._snapshots.get(sport, {"error": "No snapshot taken"})


__all__ = [
    "init_state",
    "initialize",
    "start",
    "stop",
    "handle_ws_update",
    "get_edge_report",
    "force_snapshot",
    # re-exported DB queries so the facade imports from one place
    "fetch_ev_opportunities",
    "fetch_recent_movements",
    "fetch_snapshot_history",
]
