"""ml_features — read-only feature store for Callisto's ML baseline.

Callisto has accumulated substantial history in ``player_stats``,
``backtest_events``, ``game_results``, ``game_contexts``, ``line_movements``
and ``closing_lines``. The hand-crafted thesis seeds cover specific cells in
that space; an ML baseline can learn patterns the seeds miss — but only if
we can produce a clean feature vector for any ``(player, stat, asof)`` or
``(event, asof)`` tuple without leakage.

This module is STRICTLY read-only. It does not insert/update/delete rows and
it never reaches over the network. Every query is bounded by an ``asof_ts``
cutoff so the same feature vector can be reproduced deterministically at
training time and at inference time. No lookahead — rolling windows filter
``game_date < asof_date`` (strict).

Public surface:

    build_player_prop_features(
        player, stat_type, event_id, asof_ts, sport=..., conn=None
    ) -> FeatureVector

    build_game_total_features(
        event_id, asof_ts, sport=..., conn=None
    ) -> FeatureVector

    feature_names_player_prop() -> list[str]
    feature_names_game_total()  -> list[str]

A ``FeatureVector`` is a thin wrapper around an ordered numpy array plus a
name list so downstream callers can keep train/predict alignment without
relying on pandas column ordering. All features are float64; missing values
are surfaced as ``numpy.nan`` (the classifier layer uses XGBoost, which
handles NaN natively).

The implementation lives in the ``tools.mlfeat`` package (split out of this
formerly ~1100-line module); this file is kept as a thin facade so all
existing ``tools.ml_features`` imports keep working.
"""
from __future__ import annotations

from tools.mlfeat import (
    FeatureVector,
    build_game_total_features,
    build_player_prop_features,
    feature_names_game_total,
    feature_names_player_prop,
)
from tools.mlfeat.base import (
    _altitude_factor,
    _asof_date,
    _is_dome,
    _mean,
    _open_ro,
    _park_factor,
    _resolve_db_path,
    _safe_stdev,
    _trend_slope,
)
from tools.mlfeat.fetchers import (
    _batter_stands,
    _fetch_event_context,
    _fetch_opp_allowed,
    _fetch_player_clv_deviation,
    _fetch_player_history,
    _line_movement_features,
    _lineup_recent_mean,
    _pitcher_handedness,
    _team_games_in_window,
    _team_last_game_date,
    _team_recent_totals,
)
from tools.mlfeat.game_total import _season_week

__all__ = [
    "FeatureVector",
    "build_player_prop_features",
    "build_game_total_features",
    "feature_names_player_prop",
    "feature_names_game_total",
]
