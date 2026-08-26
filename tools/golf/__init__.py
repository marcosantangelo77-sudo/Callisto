"""Masters Tournament analysis package.

Split from the former monolithic ``tools/golf_masters.py``:

- ``tools.golf.db``          — schema, DB path, connection helpers
- ``tools.golf.historical``  — historical results collection (ESPN + embedded fallback)
- ``tools.golf.field``       — current season stats & Masters field
- ``tools.golf.backtest``    — leave-one-out / rolling-window backtests
- ``tools.golf.predictions`` — 2026 predictions & composite scoring
"""

from tools.golf.db import DB_PATH, MASTERS_SCHEMA, ensure_masters_schema
from tools.golf.historical import (
    _fetch_espn_masters_year,
    _fetch_masters_year_fallback,
    _get_embedded_masters_data,
    _normalize_player_name,
    _parse_position,
    fetch_masters_historical,
)
from tools.golf.field import fetch_current_season_stats, fetch_masters_field
from tools.golf.backtest import (
    _compute_masters_fit_score_for_player,
    _spearman_rank_correlation,
    leave_one_out_backtest,
    rolling_window_backtest,
)
from tools.golf.predictions import compute_masters_fit_score, generate_2026_predictions

__all__ = [
    "DB_PATH",
    "MASTERS_SCHEMA",
    "ensure_masters_schema",
    "fetch_masters_historical",
    "_normalize_player_name",
    "_parse_position",
    "_get_embedded_masters_data",
    "_fetch_espn_masters_year",
    "_fetch_masters_year_fallback",
    "fetch_current_season_stats",
    "fetch_masters_field",
    "_spearman_rank_correlation",
    "_compute_masters_fit_score_for_player",
    "leave_one_out_backtest",
    "rolling_window_backtest",
    "generate_2026_predictions",
    "compute_masters_fit_score",
]
