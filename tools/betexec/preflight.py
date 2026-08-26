"""Preflight safety-gate evaluation (slice 3 split).

Extracted from ``BetExecutor.preflight_check`` in
``tools/bet_executor.py``: the pure decision logic that decides whether a
bet may be placed. The executor gathers the live values (bankroll, daily
losses) from the DB and hands them here; this module stays synchronous,
stateless, and side-effect free.
"""

from tools.betexec.config import (
    DAILY_LOSS_LIMIT_PCT,
    MAX_BET_PCT,
    MIN_EDGE_TO_EXECUTE,
)
from tools.betexec.dk_constants import DK_SPORT_SLUGS


def evaluate_preflight(
    *,
    enabled: bool,
    edge: float,
    bankroll: float,
    stake: float,
    daily_losses: float,
    sport: str,
) -> tuple[bool, str]:
    """Run all safety gates against already-fetched values.

    Returns ``(ok, reason)`` in the exact order the legacy method checked:
    enablement → min edge → positive bankroll → max-single-bet cap →
    daily loss limit → supported sport. Never raises, never writes.
    """
    if not enabled:
        return False, "Executor is disabled"

    if edge < MIN_EDGE_TO_EXECUTE:
        return False, f"Edge {edge:.3f} below minimum {MIN_EDGE_TO_EXECUTE}"

    if bankroll <= 0:
        return False, "No bankroll"

    if stake > bankroll * MAX_BET_PCT:
        return False, (
            f"Stake ${stake:.2f} exceeds {MAX_BET_PCT*100:.0f}% of "
            f"bankroll ${bankroll:.2f}"
        )

    if daily_losses < -(bankroll * DAILY_LOSS_LIMIT_PCT):
        return False, (
            f"Daily loss limit hit: ${daily_losses:.2f} "
            f"(limit: ${bankroll * DAILY_LOSS_LIMIT_PCT:.2f})"
        )

    if sport not in DK_SPORT_SLUGS:
        return False, f"Sport {sport} not supported for DK execution"

    return True, "OK"
