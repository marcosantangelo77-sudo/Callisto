"""SystemHealth monitor — periodic checks, breaker orchestration, reporting."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .breakers import CircuitBreaker, ErrorTracker
from .checks import (
    MemoryLeakDetector,
    check_data_collector,
    check_disk,
    check_memory,
    check_network,
    check_ollama,
    check_sqlite,
)
from .config import (
    BREAKER_COOLDOWN,
    CHECK_INTERVAL,
    db_path,
    SUBSYSTEMS,
    SUBSYSTEM_BREAKER_CFG,
)

logger = logging.getLogger("callisto.health")


class SystemHealth:
    """
    Comprehensive system health monitor.

    Call check_all() periodically. Each check updates the internal state
    and takes corrective action if possible. The full status is available
    via get_full_report() for the /health API endpoint.
    """

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        for name in SUBSYSTEMS:
            cfg = SUBSYSTEM_BREAKER_CFG.get(name, {})
            self._breakers[name] = CircuitBreaker(
                name,
                fast=bool(cfg.get("fast", False)),
            )
        self._errors = ErrorTracker()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_count = 0
        self._started_at = time.monotonic()
        self._last_check: dict[str, dict] = {}
        self._memory_detector = MemoryLeakDetector()
        # Network check caching / escalation state (shared with healthz.checks)
        self._network_state = {
            "cache": None,
            "cache_ts": 0.0,
            "first_failure_ts": None,
        }
        self._stalled_phases: set[str] = set()
        # Subsystem trip history: list of {name, opened_at, error, reopened_s}
        self._trip_history: list[dict] = []

        # External references (set after initialization)
        self.research_loop = None
        self.autonomous_loop = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(self._loop())
        logger.info("System health monitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("System health monitor stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # Let other systems start first
        while self._running:
            try:
                await self.check_all()
                self._check_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def check_all(self) -> dict:
        """Run all health checks. Returns summary."""
        results = {}

        checks = [
            ("ollama", check_ollama),
            ("sqlite", check_sqlite),
            ("disk", _sync_fn(check_disk)),
            ("memory", _memory_check_fn(self._memory_detector)),
            ("network", _network_check_fn(self._network_state)),
            ("data_collector", check_data_collector),
        ]

        for name, check_fn in checks:
            breaker = self._breakers[name]
            if not breaker.should_attempt():
                results[name] = {
                    "status": "circuit_open",
                    "message": f"Disabled — retry in {breaker.to_dict()['cooldown_remaining']:.0f}s",
                }
                continue

            try:
                result = await check_fn()
                results[name] = result
                self._last_check[name] = result

                status = result.get("status", "ok")
                if status == "ok":
                    breaker.record_success()
                elif status == "warning":
                    # Intermediate state — e.g. memory leak suspected but RSS
                    # still under limit, or transient network hiccup. Don't
                    # trip the breaker, but DON'T reset the consecutive-failure
                    # counter either: a sustained warning should eventually
                    # escalate on its own terms (network cache, memory growth,
                    # etc). This is the fix for silent suppression where
                    # "warning" acted as success and hid real degradation.
                    breaker.record_intermediate()
                    self._errors.record(
                        name, result.get("warning", result.get("message", "warning"))
                    )
                else:
                    error_msg = result.get("error", result.get("message", "unknown"))
                    tripped = breaker.record_failure(error_msg)
                    self._errors.record(name, error_msg)

                    if tripped:
                        await self._on_breaker_trip(name, error_msg)

            except Exception as e:
                error_str = str(e)
                results[name] = {"status": "error", "error": error_str}
                breaker.record_failure(error_str)
                self._errors.record(name, error_str)

        return results

    async def _check_ollama(self) -> dict:
        return await check_ollama()

    async def _check_sqlite(self) -> dict:
        return await check_sqlite()

    async def _check_disk(self) -> dict:
        return check_disk()

    async def _check_memory(self) -> dict:
        return check_memory(self._memory_detector)

    async def _check_network(self) -> dict:
        return await check_network(self._network_state)

    async def _check_data_collector(self) -> dict:
        return await check_data_collector()

    # ── Corrective actions ──

    async def _on_breaker_trip(self, subsystem: str, error: str) -> None:
        """Called when a circuit breaker trips. Take corrective action."""
        logger.error(f"Breaker tripped for {subsystem}: {error}")
        # Record trip history for /health/detailed visibility
        self._trip_history.append({
            "name": subsystem,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "error": error[:500],
        })
        # Keep last 50 trips
        if len(self._trip_history) > 50:
            self._trip_history = self._trip_history[-50:]

        # Try Telegram alert
        try:
            from tools import telegram
            await telegram.alert_system(
                f"HEALTH ALERT: {subsystem} circuit breaker OPEN\n"
                f"Error: {error}\n"
                f"Subsystem disabled for {BREAKER_COOLDOWN}s.\n"
                f"Other subsystems continue running.",
                is_error=True,
            )
        except Exception as e:
            logger.warning(f"Telegram health alert failed for {subsystem}: {e}")

        # Subsystem-specific recovery
        if subsystem == "ollama":
            # Try restarting Ollama
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ollama", "serve",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info("Attempted Ollama restart")
            except Exception as e:
                logger.warning(f"Ollama restart failed: {e}")

        elif subsystem == "sqlite" and "corrupt" in error.lower():
            # SQLite corruption — try WAL checkpoint
            try:
                import aiosqlite
                async with aiosqlite.connect(db_path()) as db:
                    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.info("SQLite WAL checkpoint forced")
            except Exception as e:
                logger.warning(f"SQLite WAL checkpoint failed: {e}")

    # ── Public API ──

    def get_breaker(self, subsystem: str) -> Optional[CircuitBreaker]:
        return self._breakers.get(subsystem)

    def is_subsystem_healthy(self, subsystem: str) -> bool:
        """Check if a subsystem is healthy (breaker closed)."""
        breaker = self._breakers.get(subsystem)
        if not breaker:
            return True
        return not breaker.is_open

    def get_full_report(self) -> dict:
        """Full health report for /health endpoint."""
        uptime = time.monotonic() - self._started_at

        breaker_status = {
            name: b.to_dict() for name, b in self._breakers.items()
        }

        all_healthy = all(not b.is_open for b in self._breakers.values())

        return {
            "healthy": all_healthy,
            "uptime_seconds": round(uptime, 0),
            "uptime_hours": round(uptime / 3600, 1),
            "checks_completed": self._check_count,
            "check_interval_seconds": CHECK_INTERVAL,
            "subsystems": breaker_status,
            "error_rates": self._errors.get_summary(),
            "last_checks": self._last_check,
            "stalled_phases": list(self._stalled_phases),
            "trip_history": list(self._trip_history),
        }

    def write_health_file(self) -> None:
        """
        Write health status to a file for Layer 3 (sentinel) to read.
        This works even if the HTTP server is down.
        """
        health_file = os.path.join(
            os.path.dirname(os.path.abspath(db_path())), "health.json"
        )
        try:
            report = self.get_full_report()
            report["timestamp"] = datetime.now(timezone.utc).isoformat()
            report["pid"] = os.getpid()
            with open(health_file, "w") as f:
                json.dump(report, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write health file: {e}")


def _sync_fn(fn):
    """Adapt a sync check fn to the awaited check contract."""
    async def runner():
        return fn()
    return runner


def _memory_check_fn(detector: MemoryLeakDetector):
    async def runner():
        return check_memory(detector)
    return runner


def _network_check_fn(state: dict):
    async def runner():
        return await check_network(state)
    return runner
