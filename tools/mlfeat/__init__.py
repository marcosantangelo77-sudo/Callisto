"""tools.mlfeat — read-only feature store for Callisto's ML baseline.

Split out of the original monolithic ``tools/ml_features.py``:

  * ``base``        — FeatureVector container, read-only DB helpers,
                      asof/numeric helpers, static venue metadata
  * ``fetchers``    — read-only SQL fetchers (history, opponents, contexts,
                      CLV deviations, handedness, totals, line movements)
  * ``player_prop`` — ``build_player_prop_features`` + its feature names
  * ``game_total``  — ``build_game_total_features`` + its feature names

This package is STRICTLY read-only: no inserts/updates/deletes, no network.
Every query is bounded by an ``asof_ts`` cutoff; rolling windows filter
``game_date < asof_date`` (strict) so features are leakage-free and
reproducible at train and inference time.
"""
from tools.mlfeat.base import FeatureVector
from tools.mlfeat.game_total import (
    build_game_total_features,
    feature_names_game_total,
)
from tools.mlfeat.player_prop import (
    build_player_prop_features,
    feature_names_player_prop,
)

__all__ = [
    "FeatureVector",
    "build_player_prop_features",
    "build_game_total_features",
    "feature_names_player_prop",
    "feature_names_game_total",
]
