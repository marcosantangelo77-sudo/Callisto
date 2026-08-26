"""tools.betexec.db_state — read-only bankroll / PnL / exposure DB queries.

Slice-4 split (2026-08): these were previously private methods on
``BetExecutor`` (get_bankroll, get_daily_stakes, get_open_exposure,
get_daily_losses) plus the status-dict assembly. They are pure reads over
the ``bankroll`` and ``bets`` tables and need nothing but an
``aiosqlite.Connection`` (or any compatible async-execution fake used by
tests). No writes happen here — record-side writes live in
``tools.betexec.logging``.

SECURITY context (audit H-1/H-4): callers that feed these values into
sizing or exposure-cap decisions must hold ``BetExecutor._bankroll_lock``
across read → size → write; see ``tools.betexec.execution.run_execute_bet``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.betexec.config import (
    DAILY_LOSS_LIMIT_PCT,
    KELLY_FRACTION,
    MAX_BET_PCT,
    MIN_EDGE_TO_EXECUTE,
)


async def get_bankroll(db) -> float:
    """Get current bankroll balance (latest bankroll row)."""
    cursor = await db.execute(
        "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return row[0] if row else 0.0


async def get_daily_stakes(db) -> float:
    """Get total stakes placed today (UTC date prefix match on placed_at)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = await db.execute(
        "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE placed_at >= ?",
        (today,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0.0


async def get_open_exposure(db) -> float:
    """Total stake across all currently-pending bets.

    SECURITY (audit H-1): used as the denominator of the portfolio cap that
    keeps simultaneous bets from compounding past MAX_OPEN_EXPOSURE_PCT of
    bankroll.
    """
    cursor = await db.execute(
        "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE result = 'pending'"
    )
    row = await cursor.fetchone()
    return float(row[0]) if row else 0.0


async def get_daily_losses(db) -> float:
    """Get net losses today (negative = losing)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = await db.execute(
        """SELECT COALESCE(SUM(
            CASE WHEN result = 'won' THEN payout - stake
                 WHEN result = 'lost' THEN -stake
                 ELSE 0 END
        ), 0) FROM bets WHERE placed_at >= ?""",
        (today,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0.0


def build_status(
    *,
    enabled: bool,
    logged_in: bool,
    browser_active: bool,
    bankroll: float,
    daily_losses: float,
) -> dict:
    """Assemble the executor health-check status dict.

    Pure function: takes already-fetched live values and layers the static
    config constants on top. Kept out of the facade so health endpoints can
    be tested without touching aiosqlite.
    """
    return {
        "enabled": enabled,
        "logged_in": logged_in,
        "browser_active": browser_active,
        "bankroll": bankroll,
        "daily_losses": daily_losses,
        "daily_loss_limit": bankroll * DAILY_LOSS_LIMIT_PCT,
        "max_single_bet_pct": MAX_BET_PCT,
        "kelly_fraction": KELLY_FRACTION,
        "min_edge": MIN_EDGE_TO_EXECUTE,
    }
