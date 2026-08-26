"""tools.betexec — modular home for bet-executor helpers.

The public API is re-exported by ``tools.bet_executor`` (facade) and also
importable directly from here. Safety posture is unchanged: nothing in this
package enables the executor, arms live betting, or touches
``_PAPER_TRADE_SIGNAL_STATUSES``.
"""

from tools.betexec.config import (
    DB_PATH,
    DAILY_LOSS_LIMIT_PCT,
    DRAWDOWN_PEAK_WINDOW_DAYS,
    FULL_QUARTER_KELLY_FRACTION,
    HALF_KELLY_FRACTION,
    KELLY_FRACTION,
    MAX_BET_PCT,
    MAX_DRAWDOWN_PCT,
    MAX_GAME_EXPOSURE_PCT,
    MAX_OPEN_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
    MIN_BET_AMOUNT,
    MIN_EDGE_TO_EXECUTE,
    REGIME_MAX_MULT,
    REGIME_MIN_MULT,
    REGIME_SAFETY_ENABLED,
    REGIME_SIZING_ENABLED,
    SCREENSHOT_DIR,
    SESSION_DIR,
    VAR_DAMPENER_HIGH_N,
    VAR_DAMPENER_LOW_N,
)
from tools.betexec.dk_constants import DK_BASE_URL, DK_SPORT_SLUGS
from tools.betexec.regime import clamped_regime_multiplier, regime_safe
from tools.betexec.sizing import (
    apply_exposure_caps,
    build_portfolio_requests,
    compute_stake,
    signals_n_to_kelly_fraction,
)
from tools.betexec.drawdown import build_kill_switch_alert, evaluate_drawdown

__all__ = [
    "DB_PATH",
    "DK_BASE_URL",
    "DK_SPORT_SLUGS",
    "DAILY_LOSS_LIMIT_PCT",
    "DRAWDOWN_PEAK_WINDOW_DAYS",
    "FULL_QUARTER_KELLY_FRACTION",
    "HALF_KELLY_FRACTION",
    "KELLY_FRACTION",
    "MAX_BET_PCT",
    "MAX_DRAWDOWN_PCT",
    "MAX_GAME_EXPOSURE_PCT",
    "MAX_OPEN_EXPOSURE_PCT",
    "MAX_SPORT_EXPOSURE_PCT",
    "MIN_BET_AMOUNT",
    "MIN_EDGE_TO_EXECUTE",
    "REGIME_MAX_MULT",
    "REGIME_MIN_MULT",
    "REGIME_SAFETY_ENABLED",
    "REGIME_SIZING_ENABLED",
    "SCREENSHOT_DIR",
    "SESSION_DIR",
    "VAR_DAMPENER_HIGH_N",
    "VAR_DAMPENER_LOW_N",
    "apply_exposure_caps",
    "build_kill_switch_alert",
    "build_portfolio_requests",
    "clamped_regime_multiplier",
    "compute_stake",
    "evaluate_drawdown",
    "regime_safe",
    "signals_n_to_kelly_fraction",
]
