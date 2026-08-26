"""
Lifecycle operations for the line monitor — slice-6 extraction.

Extracted from tools/line_monitor.py so the LineMonitor class stays a
thin facade over tools/lines/ internals. Everything here takes its
collaborators explicitly (the monitor instance for shared state, config
values) and never imports tools.line_monitor, so there is no import
cycle:

- wait_for_drain   — pause the monitor and guarantee no in-flight DB ops
                     (acquires _snapshot_lock FIRST — security audit C-8)
- resume_monitor   — release the drain lock and unpause
- monitor_loop     — main monitoring loop: pause/ack handshake, cycle
                     delegation to tools.lines.monitor_loop.run_monitor_cycle,
                     adaptive inter-cycle sleep
- build_status     — DB-backed status dict for LineMonitor.get_status()

Behavior is identical to the inline code it was extracted from; only the
plumbing moved.
"""

import asyncio
import logging

logger = logging.getLogger("callisto.line_monitor.lifecycle")

# Sleep between pause-poll retries inside wait_for_drain.
DRAIN_POLL_INTERVAL_S = 0.5

# Error-path sleep for the main monitor loop.
MONITOR_LOOP_ERROR_SLEEP_S = 30


async def wait_for_drain(monitor, timeout: float = 60) -> bool:
    """Pause the monitor and wait until all in-flight DB ops complete.

    Sets _paused, waits for _pause_ack, then ACQUIRES the snapshot lock
    to guarantee no new snapshot can start. Returns True if drained.
    Caller MUST eventually call resume_monitor() to release the lock.
    """
    monitor._paused = True
    deadline = asyncio.get_event_loop().time() + timeout
    # SECURITY (audit C-8): acquire the lock FIRST, then verify under-lock that
    # ack is set and no DB op is in flight. Previously we checked ack outside the
    # lock and then acquired — between those two operations a snapshot could start
    # and set _in_flight_db=True, leaving the caller with the lock but a live writer
    # racing it. By holding the lock during verification we guarantee mutual
    # exclusion: if someone else has the lock we wait; once we hold it no new
    # snapshot can begin (the loop body acquires _snapshot_lock before doing work).
    while asyncio.get_event_loop().time() < deadline:
        try:
            await asyncio.wait_for(
                monitor._snapshot_lock.acquire(),
                timeout=max(1.0, deadline - asyncio.get_event_loop().time()),
            )
        except asyncio.TimeoutError:
            break
        # Lock held — re-check invariants under it.
        if monitor._pause_ack.is_set() and not monitor._in_flight_db:
            return True
        # Caller hasn't fully drained; release and retry after a short sleep.
        try:
            monitor._snapshot_lock.release()
        except RuntimeError:
            pass
        await asyncio.sleep(DRAIN_POLL_INTERVAL_S)
    logger.warning(
        f"wait_for_drain timed out after {timeout}s "
        f"(ack={monitor._pause_ack.is_set()}, in_flight={monitor._in_flight_db})"
    )
    return False


def resume_monitor(monitor) -> None:
    """Release the drain lock and unpause the monitor.

    Must be called after wait_for_drain() succeeds, in a try/finally
    block to guarantee the lock is released.
    """
    monitor._paused = False
    if monitor._snapshot_lock.locked():
        try:
            monitor._snapshot_lock.release()
        except RuntimeError:
            # Already released — non-fatal
            pass


async def monitor_loop(monitor, *, monitored_sports: list[str],
                       snapshot_interval: int, get_credit_status) -> None:
    """Main monitoring loop body — snapshot, compare, alert.

    Delegates each cycle to tools.lines.monitor_loop.run_monitor_cycle
    (credit-aware fallback switch, adaptive interval stretch, per-sport
    backoff, free prop cascade). This function owns the pause/ack
    handshake and the inter-cycle sleep; the caller wraps it in
    `while monitor._running`.
    """
    while monitor._running:
        # Yield to backtests when paused
        if monitor._paused:
            monitor._pause_ack.set()
            await asyncio.sleep(5)
            continue
        monitor._pause_ack.clear()
        try:
            interval = await run_one_cycle(
                monitor,
                monitored_sports=monitored_sports,
                snapshot_interval=snapshot_interval,
                get_credit_status=get_credit_status,
            )

            # If paused mid-cycle (broke out of sport loop), skip the
            # interval sleep and loop back immediately so _pause_ack fires.
            # Without this, autonomous waits 30s, always times out, and
            # proceeds with WAL-contending DB writes.
            if monitor._paused:
                continue

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(MONITOR_LOOP_ERROR_SLEEP_S)


async def run_one_cycle(monitor, *, monitored_sports: list[str],
                        snapshot_interval: int, get_credit_status) -> int:
    """Thin indirection over tools.lines.monitor_loop.run_monitor_cycle.

    Kept separate so tests can patch the cycle runner without touching
    the loop scaffolding above.
    """
    from tools.lines.monitor_loop import run_monitor_cycle

    return await run_monitor_cycle(
        monitor,
        monitored_sports=monitored_sports,
        snapshot_interval=snapshot_interval,
        get_credit_status=get_credit_status,
    )


async def build_status(monitor, *, monitored_sports: list[str],
                       snapshot_interval: int, get_credit_status) -> dict:
    """Assemble the monitor status dict with DB-backed counts."""
    counts = {}
    if monitor._db is not None:
        from tools.lines.monitor_loop import collect_status_counts
        counts = await collect_status_counts(monitor._db)

    return {
        "running": monitor._running,
        "monitored_sports": list(monitored_sports),
        "snapshot_interval_seconds": snapshot_interval,
        "cached_snapshots": list(monitor._snapshots.keys()),
        "db_snapshots_total": counts.get("db_snapshots_total", 0),
        "db_movements_total": counts.get("db_movements_total", 0),
        "db_closing_lines": counts.get("db_closing_lines", 0),
        "latest_snapshot_at": counts.get("latest_snapshot_at"),
        "recent_alerts_in_memory": len(monitor._alerts),
        "credits": get_credit_status(),
    }
