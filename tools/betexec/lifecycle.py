"""tools.betexec.lifecycle — drawdown kill-switch orchestration + arm gates.

Slice-4 split (2026-08): ``BetExecutor.check_drawdown_and_kill``,
``enable``/``disable``, and the status assembly moved here. The module owns
the *sequence* of the kill-switch (read → record peak → evaluate → CAS
hypotheses → alert) while the pure pieces stay in their slice-3 homes:

  - ``tools.betexec.drawdown.evaluate_drawdown``  — threshold arithmetic
  - ``tools.betexec.kill_switch.pause_live_hypotheses`` — DB CAS
  - ``tools.betexec.notify`` / drawdown message builders

SAFETY: the local-only refusal lives in :func:`arm_gate_refusal` and is
checked BEFORE any state is flipped. The executor never arms itself; this
module merely implements the gate the facade exposes.
"""

from __future__ import annotations

import logging
import os

from tools.betexec.config import MAX_DRAWDOWN_PCT
from tools.betexec.kill_switch import attach_pause_result, pause_live_hypotheses
from tools.betexec.drawdown import build_kill_switch_alert, evaluate_drawdown
from tools.betexec import logging as betexec_logging

logger = logging.getLogger("callisto.executor")

LOCAL_ONLY_ENV = "CALLISTO_LOCAL_ONLY"


def is_local_only() -> bool:
    """True when CALLISTO_LOCAL_ONLY is truthy (appliance-wide nuclear switch)."""
    return os.getenv(LOCAL_ONLY_ENV, "").lower() in ("1", "true", "yes")


def arm_gate_refusal() -> str:
    """Return a refusal reason when arming must be blocked, else empty string."""
    if is_local_only():
        return (
            "Bet executor NOT enabled: CALLISTO_LOCAL_ONLY is set — "
            "local-only mode refuses to arm live betting"
        )
    return ""


async def run_check_drawdown_and_kill(
    db,
    *,
    disable_fn,
) -> dict:
    """Evaluate rolling drawdown; if past MAX_DRAWDOWN_PCT, kill-switch.

    Flow:
      1. Read current bankroll and rolling peak.
      2. Record current bankroll into bankroll_peak (append-only history).
      3. If current < peak * (1 - MAX_DRAWDOWN_PCT):
         - disarm the executor via ``disable_fn``
         - CAS all LIVE hyps to 'drawdown_paused'
         - fire Telegram alert (best-effort; missing webhook is fine)

    Returns a status dict describing the action taken.
    """
    from tools.betexec.db_state import get_bankroll

    current = await get_bankroll(db)
    peak = await betexec_logging.rolling_peak(db)
    await betexec_logging.record_bankroll_peak(db, current)

    status = evaluate_drawdown(current, peak)

    if not status["triggered"]:
        return status

    # Kill switch fires.
    logger.error(
        f"DRAWDOWN KILL SWITCH: current=${current:,.2f} peak=${peak:,.2f} "
        f"drawdown={status['drawdown_pct']:.1%} exceeds threshold {MAX_DRAWDOWN_PCT:.1%}"
    )
    disable_fn()

    # CAS all LIVE hypotheses to drawdown_paused (tools.betexec.kill_switch).
    try:
        paused = await pause_live_hypotheses(db)
        status = attach_pause_result(status, paused)
    except Exception as e:  # noqa: BLE001 — kill switch must not raise past here
        status = attach_pause_result(status, [], error=e)

    # Best-effort Telegram alert.
    try:
        from tools.telegram import send_alert  # noqa: WPS433
        msg = build_kill_switch_alert(
            current, peak, status["drawdown_pct"], len(status["paused_hypotheses"])
        )
        await send_alert(msg, throttle_key="drawdown_kill")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Telegram drawdown alert skipped: {e}")

    return status


async def run_status(
    db,
    *,
    enabled: bool,
    logged_in: bool,
    browser_active: bool,
) -> dict:
    """Gather live values and assemble the health-check status dict.

    Delegates the dict shape to tools.betexec.db_state.build_status so the
    contract stays in one place.
    """
    from tools.betexec.db_state import build_status, get_bankroll, get_daily_losses

    bankroll = await get_bankroll(db) if db else 0
    daily_losses = await get_daily_losses(db) if db else 0
    return build_status(
        enabled=enabled,
        logged_in=logged_in,
        browser_active=browser_active,
        bankroll=bankroll,
        daily_losses=daily_losses,
    )
