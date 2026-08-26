"""
tools.bankrollsim — split implementation of tools.bankroll_sim.

The canonical module remains ``tools.bankroll_sim`` (a thin facade that
re-exports every public name from this package). Internal structure:

- config:       env-driven constants (kill-switch drawdown, exposure caps)
- result:       PortfolioSimResult dataclass + degenerate-result builder
- signals:      backtest_events loading with lookahead defense, day grouping
- sizing:       portfolio-Kelly slate sizing mirroring the live executor,
                plus bet resolution (net P&L)
- simulator:    core Monte Carlo path loop + aggregation (simulate_portfolio)
- promote_gate: pre-LIVE promotion sim (simulate_before_promote)
- histogram:    ASCII bankroll-distribution plot

This is a PRE-LIVE simulation framework only. It never places bets and is
never wired into any live-betting path.
"""

from tools.bankrollsim.config import (
    DB_PATH,
    DEFAULT_KILL_SWITCH_DRAWDOWN,
    SIM_MAX_BET_PCT,
    SIM_MAX_GAME_EXPOSURE_PCT,
    SIM_MAX_SPORT_EXPOSURE_PCT,
    SIM_MIN_BET_AMOUNT,
)
from tools.bankrollsim.result import PortfolioSimResult, degenerate_result
from tools.bankrollsim.signals import _group_signals_by_day, _load_signals
from tools.bankrollsim.sizing import _resolve_bets, _size_slate
from tools.bankrollsim.simulator import simulate_portfolio
from tools.bankrollsim.promote_gate import simulate_before_promote
from tools.bankrollsim.histogram import ascii_bankroll_histogram

__all__ = [
    "DB_PATH",
    "DEFAULT_KILL_SWITCH_DRAWDOWN",
    "SIM_MAX_BET_PCT",
    "SIM_MAX_GAME_EXPOSURE_PCT",
    "SIM_MAX_SPORT_EXPOSURE_PCT",
    "SIM_MIN_BET_AMOUNT",
    "PortfolioSimResult",
    "degenerate_result",
    "_load_signals",
    "_group_signals_by_day",
    "_size_slate",
    "_resolve_bets",
    "simulate_portfolio",
    "simulate_before_promote",
    "ascii_bankroll_histogram",
]
