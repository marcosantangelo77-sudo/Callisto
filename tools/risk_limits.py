"""
Centralized risk-limit configuration and reporting.

feat/bet-execution-hardening (2026-04-23):
This module is the single-source-of-truth for bet execution risk gates.
Prior to this, limits were scattered across bet_executor.py module-level
constants, inline magic numbers in Kelly math, and env vars. That made
it impossible to:

  1. Render a human-readable risk report (the /bets/risk-report endpoint)
  2. Run a circuit-breaker sweep that evaluates EVERY gate together
  3. Keep the paper-trading vs live-execution thresholds separate
     (the user runs paper first, then lives once the edge holds up)

Env var conventions (all optional — safe defaults shown):
  EXECUTOR_MAX_BET_PCT              0.05   per-bet cap as fraction of bankroll
  EXECUTOR_MIN_EDGE                 0.02   min edge for paper/default path
  EXECUTOR_LIVE_MIN_EDGE            0.03   HARDER min edge once lives execute
  EXECUTOR_MAX_OPEN_EXPOSURE_PCT    0.25   sum of pending bets cap
  EXECUTOR_DAILY_LOSS_PCT           0.20   stop-loss of net P/L today
  CALLISTO_MAX_DAILY_RISK_PCT       0.30   sum of stakes placed today cap
  CALLISTO_MAX_GAME_EXPOSURE_PCT    0.08   all stakes on one event cap
  CALLISTO_MAX_SPORT_EXPOSURE_PCT   0.15   all stakes on one sport cap
  CALLISTO_MAX_DRAWDOWN_PCT         0.15   rolling-peak drawdown kill switch
  CALLISTO_DRAWDOWN_WINDOW_DAYS     30     peak window for drawdown kill

Every gate is also enumerated in ``CIRCUIT_BREAKERS`` below so the risk
report shows operators exactly which rule is tripped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    """Parse an env var as float. Invalid -> default; negative -> default."""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v < 0:
        return default
    return v


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Canonical circuit-breaker identifiers
# ---------------------------------------------------------------------------

CB_MIN_EDGE = "min_edge"
CB_LIVE_MIN_EDGE = "live_min_edge"
CB_MAX_SINGLE_BET = "max_single_bet_pct"
CB_MAX_OPEN_EXPOSURE = "max_open_exposure_pct"
CB_DAILY_LOSS = "daily_loss_pct"
CB_DAILY_RISK = "daily_risk_pct"
CB_GAME_EXPOSURE = "max_game_exposure_pct"
CB_SPORT_EXPOSURE = "max_sport_exposure_pct"
CB_DRAWDOWN_KILL = "drawdown_kill"
CB_DUPLICATE = "duplicate_bet"
CB_EXECUTOR_DISABLED = "executor_disabled"

CIRCUIT_BREAKERS: tuple[str, ...] = (
    CB_MIN_EDGE,
    CB_LIVE_MIN_EDGE,
    CB_MAX_SINGLE_BET,
    CB_MAX_OPEN_EXPOSURE,
    CB_DAILY_LOSS,
    CB_DAILY_RISK,
    CB_GAME_EXPOSURE,
    CB_SPORT_EXPOSURE,
    CB_DRAWDOWN_KILL,
    CB_DUPLICATE,
    CB_EXECUTOR_DISABLED,
)


# ---------------------------------------------------------------------------
# RiskLimits snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskLimits:
    """Immutable snapshot of the risk configuration at the moment it was read.

    Keeping this immutable means the preflight check, Kelly sizing, and
    the risk-report all see the same numbers inside one request. Mutating
    env vars between preflight and the actual placement would otherwise
    produce a subtle split-brain.
    """

    max_bet_pct: float
    min_edge_default: float
    live_min_edge: float
    max_open_exposure_pct: float
    daily_loss_limit_pct: float
    max_daily_risk_pct: float
    max_game_exposure_pct: float
    max_sport_exposure_pct: float
    max_drawdown_pct: float
    drawdown_window_days: int
    min_bet_amount: float
    kelly_fraction: float

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            max_bet_pct=_env_float("EXECUTOR_MAX_BET_PCT", 0.05),
            min_edge_default=_env_float("EXECUTOR_MIN_EDGE", 0.02),
            live_min_edge=_env_float("EXECUTOR_LIVE_MIN_EDGE", 0.03),
            max_open_exposure_pct=_env_float("EXECUTOR_MAX_OPEN_EXPOSURE_PCT", 0.25),
            daily_loss_limit_pct=_env_float("EXECUTOR_DAILY_LOSS_PCT", 0.20),
            max_daily_risk_pct=_env_float("CALLISTO_MAX_DAILY_RISK_PCT", 0.30),
            max_game_exposure_pct=_env_float("CALLISTO_MAX_GAME_EXPOSURE_PCT", 0.08),
            max_sport_exposure_pct=_env_float("CALLISTO_MAX_SPORT_EXPOSURE_PCT", 0.15),
            max_drawdown_pct=_env_float("CALLISTO_MAX_DRAWDOWN_PCT", 0.15),
            drawdown_window_days=_env_int("CALLISTO_DRAWDOWN_WINDOW_DAYS", 30),
            min_bet_amount=_env_float("EXECUTOR_MIN_BET", 1.00),
            kelly_fraction=_env_float("EXECUTOR_KELLY_FRACTION", 0.25),
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Risk report helpers
# ---------------------------------------------------------------------------

def _today_utc_iso_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def compute_risk_report(db: aiosqlite.Connection, limits: Optional[RiskLimits] = None) -> dict:
    """Compute a snapshot of current exposure, drawdown, and cap utilisation.

    Reads from the live ``bets`` and ``bankroll`` tables. Safe to call
    concurrently with placements because every SELECT is a fresh cursor
    and no locks are held across the call.

    Returns a dict the /bets/risk-report endpoint renders as JSON.
    """
    if limits is None:
        limits = RiskLimits.from_env()

    # Current bankroll.
    try:
        cur = await db.execute(
            "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cur.fetchone()
        bankroll = float(row[0]) if row else 0.0
    except Exception:
        bankroll = 0.0

    # Open exposure (pending bets).
    try:
        cur = await db.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE result = 'pending'"
        )
        row = await cur.fetchone()
        open_exposure = float(row[0]) if row else 0.0
    except Exception:
        open_exposure = 0.0

    # Daily risk (sum of stakes placed today) and daily net P/L.
    today = _today_utc_iso_prefix()
    try:
        cur = await db.execute(
            "SELECT COALESCE(SUM(stake), 0) FROM bets WHERE placed_at >= ?",
            (today,),
        )
        row = await cur.fetchone()
        daily_risk = float(row[0]) if row else 0.0
    except Exception:
        daily_risk = 0.0

    try:
        cur = await db.execute(
            """SELECT COALESCE(SUM(
                CASE WHEN result='won' THEN payout - stake
                     WHEN result='lost' THEN -stake
                     ELSE 0 END
            ), 0) FROM bets WHERE placed_at >= ?""",
            (today,),
        )
        row = await cur.fetchone()
        daily_pnl = float(row[0]) if row else 0.0
    except Exception:
        daily_pnl = 0.0

    # Per-sport exposure.
    per_sport: dict[str, dict] = {}
    try:
        cur = await db.execute(
            "SELECT sport, COALESCE(SUM(stake), 0) FROM bets "
            "WHERE result='pending' GROUP BY sport"
        )
        rows = await cur.fetchall()
        sport_cap = bankroll * limits.max_sport_exposure_pct
        for sport, total in rows:
            total_f = float(total or 0.0)
            per_sport[sport or ""] = {
                "exposure": round(total_f, 2),
                "cap": round(sport_cap, 2),
                "utilization": round(total_f / sport_cap, 4) if sport_cap > 0 else 0.0,
            }
    except Exception:
        pass

    # Per-game exposure (top N most concentrated).
    per_game: list[dict] = []
    try:
        cur = await db.execute(
            "SELECT event_id, sport, COALESCE(SUM(stake), 0) FROM bets "
            "WHERE result='pending' AND event_id IS NOT NULL AND event_id != '' "
            "GROUP BY event_id ORDER BY 3 DESC LIMIT 10"
        )
        rows = await cur.fetchall()
        game_cap = bankroll * limits.max_game_exposure_pct
        for eid, sport, total in rows:
            total_f = float(total or 0.0)
            per_game.append({
                "event_id": eid,
                "sport": sport,
                "exposure": round(total_f, 2),
                "cap": round(game_cap, 2),
                "utilization": round(total_f / game_cap, 4) if game_cap > 0 else 0.0,
            })
    except Exception:
        pass

    # Rolling peak & drawdown.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=limits.drawdown_window_days)).isoformat()
    peak = 0.0
    for tbl in ("bankroll_peak", "bankroll"):
        try:
            ts_col = "observed_at" if tbl == "bankroll_peak" else "timestamp"
            cur = await db.execute(
                f"SELECT COALESCE(MAX(balance), 0) FROM {tbl} WHERE {ts_col} >= ?",
                (cutoff,),
            )
            row = await cur.fetchone()
            p = float(row[0]) if row else 0.0
            if p > peak:
                peak = p
        except Exception:
            continue
    drawdown_pct = 0.0
    if peak > 0 and bankroll < peak:
        drawdown_pct = (peak - bankroll) / peak

    # Utilisation summary.
    def _util(x: float, cap: float) -> float:
        return round(x / cap, 4) if cap > 0 else 0.0

    daily_risk_cap = bankroll * limits.max_daily_risk_pct
    daily_loss_cap = bankroll * limits.daily_loss_limit_pct
    exposure_cap = bankroll * limits.max_open_exposure_pct

    # Which circuit breakers are currently tripped?
    tripped: list[str] = []
    if bankroll > 0:
        if open_exposure >= exposure_cap:
            tripped.append(CB_MAX_OPEN_EXPOSURE)
        if daily_risk >= daily_risk_cap:
            tripped.append(CB_DAILY_RISK)
        if daily_pnl <= -daily_loss_cap:
            tripped.append(CB_DAILY_LOSS)
        if drawdown_pct >= limits.max_drawdown_pct:
            tripped.append(CB_DRAWDOWN_KILL)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bankroll": round(bankroll, 2),
        "rolling_peak": round(peak, 2),
        "drawdown_pct": round(drawdown_pct, 4),
        "drawdown_window_days": limits.drawdown_window_days,
        "open_exposure": {
            "amount": round(open_exposure, 2),
            "cap": round(exposure_cap, 2),
            "utilization": _util(open_exposure, exposure_cap),
        },
        "daily_risk": {
            "stakes_today": round(daily_risk, 2),
            "cap": round(daily_risk_cap, 2),
            "utilization": _util(daily_risk, daily_risk_cap),
        },
        "daily_pnl": {
            "net": round(daily_pnl, 2),
            "loss_cap": round(daily_loss_cap, 2),
            "utilization": _util(max(-daily_pnl, 0.0), daily_loss_cap),
        },
        "per_sport": per_sport,
        "per_game": per_game,
        "tripped_breakers": tripped,
        "limits": limits.to_dict(),
        "circuit_breakers": list(CIRCUIT_BREAKERS),
    }


__all__ = [
    "CIRCUIT_BREAKERS",
    "CB_MIN_EDGE",
    "CB_LIVE_MIN_EDGE",
    "CB_MAX_SINGLE_BET",
    "CB_MAX_OPEN_EXPOSURE",
    "CB_DAILY_LOSS",
    "CB_DAILY_RISK",
    "CB_GAME_EXPOSURE",
    "CB_SPORT_EXPOSURE",
    "CB_DRAWDOWN_KILL",
    "CB_DUPLICATE",
    "CB_EXECUTOR_DISABLED",
    "RiskLimits",
    "compute_risk_report",
]
