"""
Temporal analysis engine — Polars-based data loading, temporal splits, and pattern discovery.

This module enforces the fundamental rule of time-series backtesting:
  NEVER derive a hypothesis from data and then backtest it on the same data.

Every pattern discovered is tagged with training period metadata so the
backtest engine can enforce temporal isolation automatically.

Facade: the implementation now lives in ``tools/temporal/``. This module
re-exports the full public API so existing imports continue to work.

Dependencies: polars, aiosqlite (for async loading)
"""

import logging
import os

from dotenv import load_dotenv

from tools.temporal.loading import (  # noqa: F401
    DB_PATH,
    _connect,
    load_backtest_events,
    load_game_results,
    load_odds_snapshots,
    load_player_stats,
)
from tools.temporal.hypotheses import (  # noqa: F401
    generate_hypotheses_from_analysis,
    get_data_summary,
)
from tools.temporal.patterns import (  # noqa: F401
    _binomial_pvalue,
    _bonferroni_finalize,
    _find_group_patterns,
    _pattern_hash,
    cross_tabulate,
    find_ats_patterns,
    find_player_prop_patterns,
)
from tools.temporal.splits import create_temporal_split, rolling_window_splits  # noqa: F401
from tools.temporal.stats import _erfc, _norm_sf  # noqa: F401
from tools.temporal.validation import validate_temporal_isolation  # noqa: F401

load_dotenv()

logger = logging.getLogger("callisto.temporal_analysis")


def get_training_window(
    window_days: int | None = None,
    cutoff_date: str | None = None,
) -> dict:
    """Return the training window used for research-phase pattern discovery.

    Kept in the facade for backwards compatibility with
    ``tools.loop.phases_impl``. The window ends ``cutoff_date`` (or today
    minus a 7-day safety gap) and spans ``window_days`` (default 180).
    """
    from datetime import date, datetime, timedelta

    if cutoff_date is None:
        cutoff_dt = date.today() - timedelta(days=7)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
    else:
        cutoff_dt = datetime.strptime(cutoff_date, "%Y-%m-%d").date()

    if window_days is None:
        window_days = int(os.getenv("CALLISTO_TRAINING_WINDOW_DAYS", "180"))

    start_dt = cutoff_dt - timedelta(days=window_days)

    return {
        "training_period_start": start_dt.strftime("%Y-%m-%d"),
        "training_cutoff": cutoff_date,
        "window_days": window_days,
    }
