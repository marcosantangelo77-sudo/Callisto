"""tools.betexec.methods — instance-level executor method bodies.

Slice-6 split (2026-08): the last remaining inline method bodies on
``BetExecutor`` moved here as free functions that receive the executor
instance explicitly (same pattern as ``bootstrap`` / ``session``):

  - read-only DB accessors   (get_bankroll / get_daily_stakes /
                              get_open_exposure / get_daily_losses)
  - the preflight gather     (enablement short-circuit → live values →
                              tools.betexec.preflight.evaluate_preflight)
  - recording / audit seams  (_record_bet / _log_action bindings used by
                              tools.betexec.wiring)
  - drawdown peak seams      (_record_bankroll_peak / _rolling_peak)
  - kill-switch + status     (bind the db handle + disarm callback to
                              tools.betexec.lifecycle flows)
  - the lazy Telegram sender (import-time failure never blocks recording)

SAFETY: nothing here arms the executor. Every function treats the
executor's ``_enabled`` flag as read-only input (only ``disable`` — invoked
by the drawdown kill switch via the caller-supplied callback — flips it,
and only to False). The CALLISTO_LOCAL_ONLY arming gate stays inline in the
facade's ``enable()`` by design (source-contract pinned).
"""

from __future__ import annotations

from typing import Optional

from tools.betexec import logging as betexec_logging
from tools.betexec import lifecycle as betexec_lifecycle
from tools.betexec.db_state import (
    get_bankroll as _db_get_bankroll,
    get_daily_losses as _db_get_daily_losses,
    get_daily_stakes as _db_get_daily_stakes,
    get_open_exposure as _db_get_open_exposure,
)
from tools.betexec.preflight import evaluate_preflight


# ---------------------------------------------------------------------------
# Read-only DB accessors
# ---------------------------------------------------------------------------


async def get_bankroll(executor) -> float:
    """Get current bankroll balance."""
    return await _db_get_bankroll(executor._db)


async def get_daily_stakes(executor) -> float:
    """Get total stakes placed today."""
    return await _db_get_daily_stakes(executor._db)


async def get_open_exposure(executor) -> float:
    """Total stake across all currently-pending bets.

    SECURITY (audit H-1): used as the denominator of the portfolio cap that
    keeps simultaneous bets from compounding past MAX_OPEN_EXPOSURE_PCT of
    bankroll.
    """
    return await _db_get_open_exposure(executor._db)


async def get_daily_losses(executor) -> float:
    """Get net losses today (negative = losing)."""
    return await _db_get_daily_losses(executor._db)


# ---------------------------------------------------------------------------
# Preflight gather
# ---------------------------------------------------------------------------


async def preflight_check(
    executor,
    sport: str,
    odds: int,
    edge: float,
    stake: float,
) -> tuple[bool, str]:
    """Run all safety checks before placing a bet.

    Returns (ok, reason). The enablement gate fires FIRST — before any DB
    access — so a disabled executor never touches storage. The pure gate
    logic lives in tools.betexec.preflight.evaluate_preflight.
    """
    if not executor._enabled:
        return False, "Executor is disabled"
    bankroll = await executor.get_bankroll()
    daily_losses = await executor.get_daily_losses()
    return evaluate_preflight(
        enabled=executor._enabled,
        edge=edge,
        bankroll=bankroll,
        stake=stake,
        daily_losses=daily_losses,
        sport=sport,
    )


# ---------------------------------------------------------------------------
# Recording / audit seams (consumed by tools.betexec.wiring)
# ---------------------------------------------------------------------------


def notify(msg: str) -> None:
    """Best-effort Telegram send — imported lazily so missing webhook config
    never blocks bet recording."""
    import asyncio

    from tools.telegram import send_alert
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # Inside a running loop (execution pipeline): fire-and-forget task.
        loop.create_task(send_alert(msg, throttle_key="bet_placed"))
        return
    asyncio.run(send_alert(msg, throttle_key="bet_placed"))


async def record_bet(
    executor,
    *,
    sport,
    event_id,
    game_description,
    team,
    market,
    bookmaker,
    odds,
    point,
    stake,
    edge,
    fair_prob,
    hypothesis_id,
) -> int:
    """Record bet in the bets table and update bankroll (via tools.betexec.logging).

    The bankroll read-modify-write stays serialized by the executor's
    ``_bankroll_lock`` held inside tools.betexec.logging.record_bet (H-4).
    """
    return await betexec_logging.record_bet(
        executor._db,
        executor.get_bankroll,
        executor._bankroll_lock,
        sport=sport,
        event_id=event_id,
        game_description=game_description,
        team=team,
        market=market,
        bookmaker=bookmaker,
        odds=odds,
        point=point,
        stake=stake,
        edge=edge,
        fair_prob=fair_prob,
        hypothesis_id=hypothesis_id,
    )


async def log_action(
    executor,
    action,
    sport,
    team,
    market,
    side,
    odds,
    stake,
    edge,
    hypothesis_id,
    bet_id=None,
    screenshot=None,
    reason=None,
) -> None:
    """Log executor action for audit trail (via tools.betexec.logging)."""
    await betexec_logging.log_action(
        executor._db, action, sport, team, market, side, odds, stake, edge,
        hypothesis_id, bet_id=bet_id, screenshot=screenshot, reason=reason,
    )


# ---------------------------------------------------------------------------
# Drawdown peak seams
# ---------------------------------------------------------------------------


async def record_bankroll_peak(executor, bankroll: float) -> None:
    """Record an observation of bankroll into the peak table (append-only)."""
    await betexec_logging.record_bankroll_peak(executor._db, bankroll)


async def rolling_peak(executor, window_days: Optional[int] = None) -> float:
    """Return MAX(balance) over the rolling peak window."""
    return await betexec_logging.rolling_peak(executor._db, window_days)


# ---------------------------------------------------------------------------
# Kill-switch + health status binding
# ---------------------------------------------------------------------------


async def check_drawdown_and_kill(executor) -> dict:
    """Evaluate rolling drawdown; if past MAX_DRAWDOWN_PCT, kill-switch.

    Flow lives in tools.betexec.lifecycle.run_check_drawdown_and_kill; this
    adapter supplies the db handle and the disarm callback (which may only
    ever flip ``_enabled`` to False — never back on).
    """
    return await betexec_lifecycle.run_check_drawdown_and_kill(
        executor._db,
        disable_fn=executor.disable,
    )


async def status(executor) -> dict:
    """Return executor status for health checks (assembly in tools.betexec.lifecycle)."""
    return await betexec_lifecycle.run_status(
        executor._db,
        enabled=executor._enabled,
        logged_in=executor._logged_in,
        browser_active=executor._page is not None,
    )
