"""
Agent health monitor for Callisto.

Pings all three Ollama models periodically and reports status.
"""

import asyncio
import logging
import time
from typing import Optional

from inference import get_architect, get_manager, get_sentinel, OllamaInference

logger = logging.getLogger("callisto.monitor")


class HealthMonitor:
    """Monitors agent health by pinging models periodically."""

    def __init__(self, interval: float = 30.0):
        self.interval = interval
        self._agents: dict[str, OllamaInference] = {
            "architect": get_architect(),
            "manager": get_manager(),
            "sentinel": get_sentinel(),
        }
        self._status: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start the monitoring loop."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Health monitor started")

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    async def _monitor_loop(self) -> None:
        while self._running:
            await self._check_all()
            await asyncio.sleep(self.interval)

    async def _check_all(self) -> None:
        """Ping all agents and record results."""
        for name, agent in self._agents.items():
            start = time.monotonic()
            result = await agent.aping()
            elapsed_ms = (time.monotonic() - start) * 1000

            self._status[name] = {
                **result,
                "response_time_ms": round(elapsed_ms, 1),
                "checked_at": time.time(),
            }

            if result["status"] != "ok":
                logger.warning(f"Agent {name} unresponsive: {result.get('error', 'unknown')}")

    def get_status(self) -> dict:
        """Return structured health report for all agents."""
        agents = {}
        for name, status in self._status.items():
            agents[name] = {
                "status": status.get("status", "unknown"),
                "model": status.get("model", ""),
                "response_time_ms": status.get("response_time_ms"),
                "error": status.get("error"),
            }

        all_ok = all(s.get("status") == "ok" for s in self._status.values())
        return {
            "healthy": all_ok and len(self._status) == 3,
            "agents": agents,
        }
