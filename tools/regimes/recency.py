"""
Recency bias quantification — how much public perception diverges from reality.

Extracted from the original ``tools/regime.py`` (section 2).
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

logger = logging.getLogger("callisto.regime")


@dataclass
class RecencyBiasResult:
    """Quantification of recency bias for a team."""
    bias_direction: str         # "overvalued" or "undervalued" by public
    bias_magnitude: float       # 0-1 scale, how severe the bias is
    mean_reversion_probability: float  # probability recent streak reverts
    recent_performance: float   # average of recent window
    underlying_performance: float  # average of full season metrics
    perception_gap: float       # signed difference (positive = public overvalues)


def recency_bias_score(recent_results: list,
                       season_results: list,
                       metric_key: Optional[str] = None) -> dict:
    """
    Quantify how much the public is likely overweighting recent results.

    The logic:
    - Public sees W-W-W and thinks "hot team" → bets them
    - Books shade the line toward public money
    - If the underlying metrics (efficiency, expected points, etc.) haven't
      actually improved, the line is inflated → value on the other side

    Parameters:
        recent_results: Last N games' performance metrics (typically 3-5 games).
            Can be floats (raw metric) or dicts with a metric_key field.
        season_results: Full season performance metrics (same format).
        metric_key: If results are dicts, which key to extract.

    Returns:
        Dict with bias_direction, bias_magnitude, mean_reversion_probability.
    """
    # Extract numeric values
    if metric_key and isinstance(recent_results[0], dict):
        recent_vals = np.array([g[metric_key] for g in recent_results], dtype=float)
        season_vals = np.array([g[metric_key] for g in season_results], dtype=float)
    else:
        recent_vals = np.array(recent_results, dtype=float)
        season_vals = np.array(season_results, dtype=float)

    recent_mean = float(np.mean(recent_vals))
    season_mean = float(np.mean(season_vals))
    season_std = float(np.std(season_vals))

    if season_std < 1e-12:
        # No variance in season data — can't assess bias
        return RecencyBiasResult(
            bias_direction="neutral",
            bias_magnitude=0.0,
            mean_reversion_probability=0.5,
            recent_performance=recent_mean,
            underlying_performance=season_mean,
            perception_gap=0.0,
        ).__dict__

    # Z-score of recent window vs season distribution
    z = (recent_mean - season_mean) / season_std

    # Bias magnitude: how extreme is the recent window?
    # Use the CDF to convert z-score to a 0-1 scale.
    # |z| > 1 means the recent window is notably different from season average.
    raw_magnitude = float(2 * abs(stats.norm.cdf(z) - 0.5))  # 0 to 1

    # Scale it: small samples amplify randomness.
    # Fewer recent games → more likely to be noise → higher bias potential.
    sample_noise_factor = 1.0 / math.sqrt(len(recent_vals))
    # If the recent divergence is large relative to what we'd expect from
    # random sampling, the public is probably overreacting.
    expected_divergence = season_std * sample_noise_factor
    actual_divergence = abs(recent_mean - season_mean)
    divergence_ratio = actual_divergence / expected_divergence if expected_divergence > 0 else 0.0

    # Bias magnitude: clamp to 0-1
    bias_magnitude = float(min(1.0, raw_magnitude * min(divergence_ratio / 2.0, 1.5)))

    # Direction: if recent > season, public overvalues → line inflated
    if z > 0.25:
        bias_direction = "overvalued"
    elif z < -0.25:
        bias_direction = "undervalued"
    else:
        bias_direction = "neutral"

    # Mean reversion probability: how likely is the recent streak to revert?
    # Based on the empirical observation that extreme performance regresses.
    # Use a logistic function of the z-score.
    # At z=0, P(reversion) = 0.5 (coin flip). At z=2, P ≈ 0.88.
    reversion_prob = float(1.0 / (1.0 + math.exp(-0.8 * abs(z))))

    # Also check: did the underlying distribution actually shift?
    # Run a quick t-test of recent vs rest-of-season.
    rest_of_season = np.array([v for v in season_vals
                               if v not in recent_vals[:3]], dtype=float)
    if len(rest_of_season) >= 3 and len(recent_vals) >= 3:
        _, p_shift = stats.ttest_ind(recent_vals, rest_of_season, equal_var=False)
        # If p < 0.05, the shift might be real → lower reversion probability
        if p_shift < 0.05:
            reversion_prob *= 0.6  # real shift detected, reduce reversion expectation
            bias_magnitude *= 0.5  # less bias if shift is genuine

    perception_gap = float(recent_mean - season_mean)

    result = RecencyBiasResult(
        bias_direction=bias_direction,
        bias_magnitude=round(bias_magnitude, 4),
        mean_reversion_probability=round(reversion_prob, 4),
        recent_performance=round(recent_mean, 4),
        underlying_performance=round(season_mean, 4),
        perception_gap=round(perception_gap, 4),
    )

    logger.info("Recency bias: %s (magnitude=%.3f, reversion_prob=%.3f)",
                result.bias_direction, result.bias_magnitude,
                result.mean_reversion_probability)

    return result.__dict__
