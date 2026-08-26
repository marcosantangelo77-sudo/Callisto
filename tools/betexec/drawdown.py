"""Drawdown kill-switch arithmetic (feat/portfolio-kelly-live-loop, 2026-04-22).

Pure evaluation helper extracted from ``BetExecutor.check_drawdown_and_kill``:
given current bankroll and the rolling peak, decide whether the kill switch
fires. DB writes, hypothesis pausing, and Telegram alerts stay in the facade.
"""

from tools.betexec.config import MAX_DRAWDOWN_PCT


def evaluate_drawdown(current: float, peak: float) -> dict:
    """Return a drawdown status dict for (current, peak).

    Mirrors the former in-method status construction: ``triggered`` is True
    only when a positive peak exists and the relative drawdown meets or
    exceeds MAX_DRAWDOWN_PCT.
    """
    status: dict = {
        "current_bankroll": current,
        "rolling_peak": peak,
        "drawdown_pct": 0.0,
        "threshold_pct": MAX_DRAWDOWN_PCT,
        "triggered": False,
        "paused_hypotheses": [],
    }

    if peak <= 0 or current >= peak:
        return status

    drawdown_pct = (peak - current) / peak
    status["drawdown_pct"] = round(drawdown_pct, 4)

    if drawdown_pct < MAX_DRAWDOWN_PCT:
        return status

    status["triggered"] = True
    return status


def build_kill_switch_alert(
    current: float, peak: float, drawdown_pct: float, n_paused: int
) -> str:
    """HTML Telegram alert body for a fired drawdown kill switch."""
    return (
        f"<b>DRAWDOWN KILL SWITCH FIRED</b>\n"
        f"\n"
        f"Current bankroll: ${current:,.2f}\n"
        f"30d peak: ${peak:,.2f}\n"
        f"Drawdown: {drawdown_pct:.1%} (threshold {MAX_DRAWDOWN_PCT:.1%})\n"
        f"\n"
        f"Executor disabled. {n_paused} LIVE "
        f"hyps → drawdown_paused. Manual review required."
    )
