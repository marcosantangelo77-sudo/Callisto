"""
Agent health monitor for Callisto.

Uses Ollama's model list API to check availability without loading models.
Avoids causing VRAM model swaps during active sessions.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from inference import OLLAMA_HOST, AGENT_CONFIGS

logger = logging.getLogger("callisto.monitor")


class HealthMonitor:
    """Monitors agent health without loading models into VRAM."""

    def __init__(self, interval: float = 60.0):
        self.interval = interval
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
        """Check all agents are available via Ollama's tags API (no model loading)."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{OLLAMA_HOST}/api/tags")
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"Ollama unreachable: {e}")
            for name in AGENT_CONFIGS:
                self._status[name] = {
                    "status": "error",
                    "model": AGENT_CONFIGS[name].model,
                    "error": f"Ollama unreachable: {e}",
                    "checked_at": time.time(),
                }
            return

        elapsed_ms = (time.monotonic() - start) * 1000
        # Match both "model" and "model:latest" — Ollama returns tagged names
        available_models = set()
        for m in data.get("models", []):
            name = m["name"]
            available_models.add(name)
            if ":" in name:
                available_models.add(name.split(":")[0])

        for name, config in AGENT_CONFIGS.items():
            if config.model in available_models:
                self._status[name] = {
                    "status": "ok",
                    "model": config.model,
                    "response_time_ms": round(elapsed_ms, 1),
                    "checked_at": time.time(),
                }
            else:
                self._status[name] = {
                    "status": "error",
                    "model": config.model,
                    "error": f"Model {config.model} not found in Ollama",
                    "checked_at": time.time(),
                }
                logger.warning(f"Agent {name}: model {config.model} not available")

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
            "healthy": all_ok and len(self._status) == len(AGENT_CONFIGS),
            "agents": agents,
        }
