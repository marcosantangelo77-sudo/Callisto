"""
tools.regimes — Regime detection and recency bias exploitation.

Split out of the original monolithic ``tools/regime.py``. Sub-modules:

- ``changepoint``  : PELT + CUSUM change-point detection and segment analysis
- ``recency``      : public-perception vs reality (recency bias) quantification
- ``power``        : regime-aware power ratings
- ``bayes``        : Bayesian prior weight schedules and conjugate updates
- ``reversion``    : mean-reversion signal detection
- ``composite``    : full pipeline combining all of the above

The legacy ``tools.regime`` module is now a thin facade over this package.
"""

from tools.regimes.changepoint import (
    ChangePointResult,
    analyze_regimes,
    detect_regime_change,
)
from tools.regimes.recency import RecencyBiasResult, recency_bias_score
from tools.regimes.power import PowerRating, calculate_power_rating
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
    # changepoint
    "detect_regime_change",
    "analyze_regimes",
    # recency
    "recency_bias_score",
    # power
    "calculate_power_rating",
    # bayes
    "prior_weight_schedule",
    "bayesian_update",
    "seasonal_bayesian_rating",
    # reversion
    "mean_reversion_signal",
    # composite
    "full_regime_analysis",
]
