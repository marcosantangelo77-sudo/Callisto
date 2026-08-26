"""Mutable endpoint health/load state and the hosted-cost ledger."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from tools.infrouter.config import EndpointConfig


class _EndpointState:
    """Mutable runtime state for one endpoint: health, load, queue slot."""
    __slots__ = ("cfg", "semaphore", "consecutive_failures",
                 "cooldown_until", "in_flight")

    def __init__(self, cfg: EndpointConfig):
        self.cfg = cfg
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.in_flight = 0

    @property
    def available(self) -> bool:
        return (
            not self.cfg.extra.get("_unresolved")
            and time.monotonic() >= self.cooldown_until
        )

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        # Exponential cooldown: 2s, 4s, 8s... capped at 60s.
        delay = min(60.0, 2.0 * (2 ** (self.consecutive_failures - 1)))
        self.cooldown_until = time.monotonic() + delay


class CostLedger:
    """Tracks token usage + USD cost per tier. Hosted calls are budgeted;
    local calls are free at the margin and show up as $0."""

    def __init__(self, budget_usd: Optional[float] = None):
        self.budget_usd = budget_usd
        self.total_cost_usd = 0.0
        self.by_tier: dict = {}
        self._lock = asyncio.Lock()

    async def record(self, tier: str, input_tokens: int,
                     output_tokens: int, cost_usd: float) -> None:
        async with self._lock:
            self.total_cost_usd += cost_usd
            t = self.by_tier.setdefault(
                tier, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cost_usd": 0.0}
            )
            t["calls"] += 1
            t["input_tokens"] += input_tokens
            t["output_tokens"] += output_tokens
            t["cost_usd"] += cost_usd

    def snapshot(self) -> dict:
        return {
            "budget_usd": self.budget_usd,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "remaining_usd": (
                None if self.budget_usd is None
                else round(self.budget_usd - self.total_cost_usd, 6)
            ),
            "over_budget": (
                self.budget_usd is not None
                and self.total_cost_usd > self.budget_usd
            ),
            "by_tier": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in sorted(self.by_tier.items())
            },
        }
