"""
Event bus — asyncio pub/sub for inter-component signaling.

Replaces interval-based polling with reactive event dispatch.
Producers publish typed events; subscribers receive them via async callbacks.
Dispatch is fire-and-forget via asyncio.create_task to prevent slow subscribers
from blocking producers. Optional SQLite audit drain for observability.

Usage:
    bus = get_event_bus()
    bus.subscribe(EVENT_EDGE_DETECTED, my_handler)
    await bus.publish(EVENT_EDGE_DETECTED, {"sport": "nba", "edge": 3.5})
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

import aiosqlite

logger = logging.getLogger("callisto.event_bus")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ── Event Type Constants ──
EVENT_SNAPSHOT_TAKEN = "snapshot_taken"
EVENT_LINE_MOVED = "line_moved"
EVENT_GAME_COMPLETED = "game_completed"
EVENT_GAME_STARTING = "game_starting"
EVENT_GAME_IMMINENT = "game_imminent"
EVENT_GAME_LINEUP_WINDOW = "game_lineup_window"
EVENT_CLAUDE_AVAILABLE = "claude_available"
EVENT_CLAUDE_UNAVAILABLE = "claude_unavailable"
EVENT_BACKTEST_COMPLETE = "backtest_complete"
EVENT_EDGE_DETECTED = "edge_detected"
EVENT_SHARP_MONEY = "sharp_money"

# Events worth persisting to SQLite for audit trail
_AUDIT_EVENTS = {
    EVENT_EDGE_DETECTED,
    EVENT_GAME_COMPLETED,
    EVENT_BACKTEST_COMPLETE,
    EVENT_SHARP_MONEY,
}


class EventBus:
    """Lightweight asyncio event bus with typed channels."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._audit_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._audit_task: Optional[asyncio.Task] = None
        self._running = False
        self._event_count = 0
        self._error_count = 0
        self._last_event_time: Optional[float] = None

    def subscribe(self, event_type: str, callback: Callable) -> None:
        """Register an async callback for an event type."""
        self._subscribers[event_type].append(callback)
        logger.debug(f"Subscriber added for {event_type}: {callback.__qualname__}")

    def unsubscribe(self, event_type: str, callback: Callable) -> None:
        """Remove a callback from an event type."""
        try:
            self._subscribers[event_type].remove(callback)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: Optional[dict] = None) -> None:
        """
        Publish an event to all subscribers.

        Dispatch is fire-and-forget: each subscriber runs in its own task.
        A slow or failing subscriber does not block the publisher or other subscribers.
        """
        data = data or {}
        data["_event_type"] = event_type
        data["_timestamp"] = datetime.now(timezone.utc).isoformat()

        self._event_count += 1
        self._last_event_time = time.time()

        subscribers = self._subscribers.get(event_type, [])
        for callback in subscribers:
            asyncio.create_task(self._safe_dispatch(callback, event_type, data))

        # Queue audit events for persistence
        if event_type in _AUDIT_EVENTS:
            try:
                self._audit_queue.put_nowait({
                    "event_type": event_type,
                    "event_data": json.dumps(data),
                })
            except asyncio.QueueFull:
                # Track drops so we know if the audit trail has gaps
                if not hasattr(self, "_audit_drops"):
                    self._audit_drops = 0
                    self._last_audit_alert_drops = 0
                self._audit_drops += 1
                # SECURITY (audit H-9): escalate audit-queue drops to CRITICAL
                # and Telegram-page on every 100 drops. Backtest results are
                # invalid when the audit trail has gaps, so silent error logs
                # are insufficient — operator must know NOW.
                logger.critical(
                    f"AUDIT QUEUE FULL — dropped event {event_type} "
                    f"(total drops: {self._audit_drops}). "
                    f"Audit trail has gaps. Backtest invariants may be violated."
                )
                if self._audit_drops - getattr(self, "_last_audit_alert_drops", 0) >= 100:
                    self._last_audit_alert_drops = self._audit_drops
                    try:
                        from tools import telegram as _tg
                        # Fire-and-forget; alert_system itself swallows errors.
                        asyncio.create_task(_tg.alert_system(
                            f"🛑 event_bus audit queue dropped {self._audit_drops} events "
                            f"(latest: {event_type}). Backtest data integrity at risk.",
                            is_error=True,
                        ))
                    except Exception:
                        pass

    async def _safe_dispatch(self, callback: Callable, event_type: str, data: dict) -> None:
        """Dispatch to a single subscriber with error isolation."""
        try:
            await callback(data)
        except Exception as e:
            self._error_count += 1
            logger.warning(
                f"Event subscriber error on {event_type} "
                f"({callback.__qualname__}): {e}"
            )

    async def start_audit_drain(self, db_path: str = DB_PATH) -> None:
        """Start background task that persists audit events to SQLite."""
        self._running = True
        self._audit_task = asyncio.create_task(self._drain_audit(db_path))
        logger.info("Event bus audit drain started")

    async def _drain_audit(self, db_path: str) -> None:
        """Background task: batch-write audit events to SQLite every 10 seconds."""
        while self._running:
            try:
                await asyncio.sleep(10)
                batch = []
                while not self._audit_queue.empty() and len(batch) < 100:
                    try:
                        batch.append(self._audit_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    # WriteCoordinator path (single-writer pattern). Skips opening
                    # a transient connection for every drain cycle.
                    try:
                        from tools.db_writer import get_writer_if_running
                        coord = get_writer_if_running(db_path)
                    except Exception:
                        coord = None
                    if coord is not None:
                        await coord.executemany(
                            "INSERT INTO event_log (event_type, event_data) VALUES (?, ?)",
                            [(e["event_type"], e["event_data"]) for e in batch],
                        )
                    else:
                        async with aiosqlite.connect(db_path) as db:
                            await db.execute("PRAGMA busy_timeout = 60000")
                            await db.executemany(
                                "INSERT INTO event_log (event_type, event_data) VALUES (?, ?)",
                                [(e["event_type"], e["event_data"]) for e in batch],
                            )
                            await db.commit()
                    logger.debug(f"Audit drain: persisted {len(batch)} events")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Audit drain error: {e}")

    async def stop(self) -> None:
        """Stop the audit drain."""
        self._running = False
        if self._audit_task:
            self._audit_task.cancel()
            try:
                await self._audit_task
            except asyncio.CancelledError:
                pass

    def get_stats(self) -> dict:
        """Return event bus statistics."""
        return {
            "total_events_published": self._event_count,
            "total_subscriber_errors": self._error_count,
            "subscriber_counts": {
                event_type: len(callbacks)
                for event_type, callbacks in self._subscribers.items()
            },
            "audit_queue_size": self._audit_queue.qsize(),
            "last_event_seconds_ago": (
                round(time.time() - self._last_event_time, 1)
                if self._last_event_time else None
            ),
        }


# ── Module-level singleton ──
_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Get or create the singleton event bus."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
