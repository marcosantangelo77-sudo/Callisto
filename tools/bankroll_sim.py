"""
Pre-LIVE bankroll Monte Carlo simulation framework.

feat/bankroll-montecarlo-sim (2026-04-22):

Before promoting a hypothesis (or portfolio of hypotheses) to real money, we
simulate N parallel bankroll paths through bootstrapped historical signal data
and quantify:
  * Ruin probability at multiple drawdown thresholds (5%, 15%, 30%)
  * Expected ROI / month (median, p10, p90)
  * Max drawdown distribution (p50, p90, p99)
  * Days-to-ruin (when it happens)
  * Sharpe / Sortino

Why this exists (the 2026-04-22 audit):
  The 16-of-22 LIVE-hypothesis correlation finding made clear that
  per-hypothesis Kelly + per-game/sport caps are not enough. Two hyps that
  fire on the same MLB game are NOT two independent edges. A 22-hyp portfolio
  of correlated MLB bets can route ~80% of bankroll into a single blowout
  evening. The only honest way to bound that tail risk is to bootstrap real
  historical signals jointly (preserving same-event correlation) and walk
  forward through the portfolio-Kelly sizer + drawdown kill switch.

Data source:
  `backtest_events` — 37k rows with resolved outcomes. Each row has
  ``hypothesis_id, event_id, game_date, side, book_odds_american, edge,
  ev_pct, actual_result`` (won/lost/push).

Lookahead filter:
  The schema does NOT (yet) have a ``snapshot_quality`` column. The audit
  wanting us to restrict to ``snapshot_quality='pre_commence'`` predicts a
  schema that doesn't exist. We defensively filter:
    * ``signal_generated = 1`` — only rows that actually triggered a bet
    * ``actual_result IN ('won', 'lost', 'push')`` — resolved rows only
    * Any row where ``snapshot_time > game_date + 1 day`` (post-commence
      snapshot, i.e., known-lookahead) is dropped.
  The sim logs counts of excluded rows so the operator can see what fraction
  of the pool was filtered out.

Correlation:
  Same-event correlation is preserved automatically by the bootstrap: when we
  sample a day, we pull ALL signals on that day for every hypothesis in the
  portfolio. If hyp A and hyp B both fire on event X on 2026-03-27, they both
  win or both lose jointly (the real historical outcome). This is the
  strongest possible correlation model — it matches reality exactly for the
  historical window.

Reproducibility:
  ``seed`` parameter (default 42). Same seed + same DB = deterministic result.

Split (2026-08): the implementation lives in the ``tools.bankrollsim``
package; this module is the facade re-exporting every public name so all
existing importers keep working unchanged.
"""

from __future__ import annotations

import logging

from tools.bankrollsim import (  # noqa: F401
    DB_PATH,
    DEFAULT_KILL_SWITCH_DRAWDOWN,
    SIM_MAX_BET_PCT,
    SIM_MAX_GAME_EXPOSURE_PCT,
    SIM_MAX_SPORT_EXPOSURE_PCT,
    SIM_MIN_BET_AMOUNT,
    PortfolioSimResult,
    _group_signals_by_day,
    _load_signals,
    _resolve_bets,
    _size_slate,
    ascii_bankroll_histogram,
    simulate_before_promote,
    simulate_portfolio,
)

logger = logging.getLogger("callisto.bankroll_sim")

__all__ = [
    "DB_PATH",
    "DEFAULT_KILL_SWITCH_DRAWDOWN",
    "SIM_MAX_BET_PCT",
    "SIM_MAX_GAME_EXPOSURE_PCT",
    "SIM_MAX_SPORT_EXPOSURE_PCT",
    "SIM_MIN_BET_AMOUNT",
    "PortfolioSimResult",
    "_load_signals",
    "_group_signals_by_day",
    "_size_slate",
    "_resolve_bets",
    "simulate_portfolio",
    "simulate_before_promote",
    "ascii_bankroll_histogram",
]
