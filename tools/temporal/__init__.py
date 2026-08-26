"""Temporal analysis package.

Split from the former monolithic ``tools/temporal_analysis.py``. The
``tools.temporal_analysis`` module remains as a thin facade re-exporting
the full public API so all existing imports keep working.
"""

from tools.temporal.loading import (  # noqa: F401
    DB_PATH,
    _connect,
    load_backtest_events,
    load_game_results,
    load_odds_snapshots,
    load_player_stats,
)
from tools.temporal.patterns import (  # noqa: F401
    _binomial_pvalue,
    _bonferroni_finalize,
    cross_tabulate,
    find_ats_patterns,
    find_player_prop_patterns,
)
from tools.temporal.splits import create_temporal_split, rolling_window_splits  # noqa: F401
from tools.temporal.stats import _erfc, _norm_sf  # noqa: F401

__all__ = [
    "DB_PATH",
    "_connect",
    "load_backtest_events",
    "load_game_results",
    "load_odds_snapshots",
    "load_player_stats",
    "create_temporal_split",
    "rolling_window_splits",
    "_erfc",
    "_norm_sf",
    "_binomial_pvalue",
    "_bonferroni_finalize",
    "find_ats_patterns",
    "find_player_prop_patterns",
    "cross_tabulate",
]
