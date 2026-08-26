"""Independent watchdog — monitors the research loop and Claude availability.

Runs as a separate async task, NOT inside the research loop.
If the loop stalls, Hermes records it and Telegram alerts.
If Claude gets stuck, it resets the counter.
"""

import asyncio
import logging
import time
from typing import Optional

from .config import HEARTBEAT_INTERVAL, LOOP_STALL_THRESHOLD

logger = logging.getLogger("callisto.self_repair")


class Heartbeat:
    """Independent watchdog — monitors the research loop and Claude availability."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_cycle_seen = 0
        self._last_cycle_time = time.monotonic()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat started — monitoring loop health every 5 min")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # Initial delay
        while self._running:
            try:
                await self._check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _check(self) -> None:
        import httpx

        # 1. Check if research loop is cycling
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://localhost:8420/system/full-status")
                d = r.json()
                rl = d.get("research_loop", {})
                current_cycle = rl.get("cycles_completed", 0)

                if current_cycle > self._last_cycle_seen:
                    self._last_cycle_seen = current_cycle
                    self._last_cycle_time = time.monotonic()
                else:
                    stall_duration = time.monotonic() - self._last_cycle_time
                    # Suppress stall warnings when Claude is rate-limited —
                    # the loop is expected to idle during cooldown periods.
                    claude_info = rl.get("claude_code", {})
                    claude_cooldown = (
                        not claude_info.get("available", True)
                        and claude_info.get("calls_this_window", 0)
                            >= claude_info.get("max_calls_per_hour", 999)
                    )
                    if stall_duration > LOOP_STALL_THRESHOLD and not claude_cooldown:
                        logger.warning(f"Heartbeat: research loop stalled for {stall_duration:.0f}s (cycle {current_cycle})")
                        # Record to Hermes
                        try:
                            from tools.hermes_memory import get_hermes_memory
                            hermes = get_hermes_memory()
                            await hermes.record_learning(
                                key="loop_stall_detected",
                                value=f"Research loop stalled at cycle {current_cycle} for {stall_duration:.0f}s",
                                confidence=0.9,
                                source="heartbeat",
                            )
                            await hermes.send_message(
                                "heartbeat",
                                f"WARNING: Research loop stalled at cycle {current_cycle} for {stall_duration/60:.0f} min",
                            )
                        except Exception:
                            pass
                        # Alert via Telegram
                        try:
                            from tools import telegram
                            await telegram.alert_system(
                                f"Research loop stalled at cycle {current_cycle} for {stall_duration/60:.0f} min",
                                is_error=True,
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Heartbeat: API unreachable — {e}")

        # 2. Check Claude availability + track downtime
        try:
            from tools.claude_code import is_available, get_usage_stats
            stats = get_usage_stats()

            # Track downtime transitions for work queue
            try:
                from tools.work_queue import get_downtime_tracker
                tracker = get_downtime_tracker()
                if is_available():
                    tracker.mark_available()
                else:
                    tracker.mark_unavailable()
                # Periodically record pattern to Hermes
                if tracker._total_outages > 0 and tracker._total_outages % 5 == 0:
                    await tracker.record_to_hermes()
            except Exception:
                pass

            if not is_available() and stats.get("elapsed_seconds", 0) > 3600:
                # Stuck — force reset
                import tools.claude_code as cc
                cc._call_count = 0
                cc._last_reset = time.monotonic()
                logger.info("Heartbeat: reset stuck Claude counter")
                try:
                    from tools.hermes_memory import get_hermes_memory
                    hermes = get_hermes_memory()
                    await hermes.record_learning(
                        key="claude_auto_reset",
                        value=f"Heartbeat auto-reset Claude counter (was stuck at {stats.get('calls_this_window')}/{stats.get('max_calls_per_hour')})",
                        confidence=0.8,
                        source="heartbeat",
                    )
                except Exception:
                    pass
        except Exception:
            pass
