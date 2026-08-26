"""Configuration constants for the pre-LIVE bankroll Monte Carlo simulator."""

from __future__ import annotations

import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Drawdown kill switch threshold — mirrors tools/bet_executor.MAX_DRAWDOWN_PCT.
# When a simulated bankroll path dips this far below its rolling peak, we
# freeze betting for the rest of the horizon (approximates the executor's
# real behavior where LIVE hyps get set to 'drawdown_paused').
DEFAULT_KILL_SWITCH_DRAWDOWN = float(
    os.getenv("CALLISTO_SIM_KILL_DRAWDOWN", "0.15")
)

# Default per-bet and portfolio caps mirror bet_executor defaults so sims
# reflect the same sizing the live path would produce.
SIM_MAX_BET_PCT = float(os.getenv("CALLISTO_SIM_MAX_BET_PCT", "0.05"))
SIM_MAX_GAME_EXPOSURE_PCT = float(os.getenv("CALLISTO_SIM_MAX_GAME_PCT", "0.08"))
SIM_MAX_SPORT_EXPOSURE_PCT = float(os.getenv("CALLISTO_SIM_MAX_SPORT_PCT", "0.15"))
SIM_MIN_BET_AMOUNT = 1.0

__all__ = [
    "DB_PATH",
    "DEFAULT_KILL_SWITCH_DRAWDOWN",
    "SIM_MAX_BET_PCT",
    "SIM_MAX_GAME_EXPOSURE_PCT",
    "SIM_MAX_SPORT_EXPOSURE_PCT",
    "SIM_MIN_BET_AMOUNT",
]
