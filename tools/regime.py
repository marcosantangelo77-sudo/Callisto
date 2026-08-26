"""
Regime detection and recency bias exploitation — finding real shifts vs noise.

The public (and books shading to public money) overreacts to recent results.
A team wins 3 straight → public perception inflates → line moves away from
true value → we bet the other side.

But sometimes a team IS genuinely better. A coaching change, key player
returning, scheme adjustment — these create real regime changes in the
underlying data. We need to separate signal from noise.

This module provides:
1. Change-point detection (PELT + CUSUM) on performance time series
2. Recency bias quantification — how much is perception diverging from reality
3. Regime-aware power ratings that weight recent regimes appropriately
4. Bayesian prior management — seasonal weight schedules for priors
5. Mean reversion detection — extreme performers tend to regress

All of this feeds the edge scanner: if we detect a regime change the
public hasn't priced in, that's alpha. If we detect the public
overweighting a streak with no underlying shift, that's also alpha.

Implementation lives in the ``tools.regimes`` package; this module is a
facade re-exporting the full public API for backward compatibility.
"""

from tools.regimes.changepoint import (
    ChangePointResult,
    _cost_normal,
    _cusum_search,
    _pelt_search,
    analyze_regimes,
    detect_regime_change,
)
from tools.regimes.recency import RecencyBiasResult, recency_bias_score
from tools.regimes.power import PowerRating, _classify_regime, calculate_power_rating
from tools.regimes.bayes import (
    BayesianResult,
    bayesian_update,
    prior_weight_schedule,
    seasonal_bayesian_rating,
)
from tools.regimes.reversion import MeanReversionSignal, mean_reversion_signal
from tools.regimes.composite import full_regime_analysis

__all__ = [
    # data structures
    "ChangePointResult",
    "RecencyBiasResult",
    "PowerRating",
    "BayesianResult",
    "MeanReversionSignal",
    # 1. changepoint detection
    "detect_regime_change",
    "analyze_regimes",
    "_cost_normal",
    "_pelt_search",
    "_cusum_search",
    # 2. recency bias
    "recency_bias_score",
    # 3. power ratings
    "calculate_power_rating",
    "_classify_regime",
    # 4. bayesian prior management
    "prior_weight_schedule",
    "bayesian_update",
    "seasonal_bayesian_rating",
    # 5. mean reversion
    "mean_reversion_signal",
    # composite
    "full_regime_analysis",
]
