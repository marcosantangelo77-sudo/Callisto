"""
Mean reversion detection — extreme performers tend to regress.

Extracted from the original ``tools/regime.py`` (section 5).
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

logger = logging.getLogger("callisto.regime")


@dataclass
class MeanReversionSignal:
    """Signal for expected mean reversion."""
    reversion_expected: bool
    magnitude: float       # expected size of reversion (in metric units)
    confidence: float      # 0-1
    current_zscore: float  # how many SDs from historical mean
    historical_mean: float
    current_value: float
    half_life_games: float  # estimated games until halfway reverted


def mean_reversion_signal(team_metric_history: list[float],
                          league_avg: float,
                          historical_mean: Optional[float] = None,
                          regression_rate: float = 0.5) -> dict:
    """
    Detect whether a team is likely to regress toward the mean.

    Sports metrics revert to the mean. Teams performing far above or below
    their historical baseline tend to revert — the question is how fast
    and how much.

    Key insight for betting: if a team is 2 SDs above their norm, the
    public prices them as if they'll stay there. We price in the reversion.

    Parameters:
        team_metric_history: Time series of the team's metric values.
            At least 10 data points recommended for reliability.
        league_avg: League-wide average for this metric.
        historical_mean: Team's long-term average. If None, uses the mean
            of team_metric_history.
        regression_rate: How fast metrics regress. 0.5 = regress halfway
            to the mean each "period." Sport-specific:
            - NBA shooting %: ~0.4-0.6
            - NFL passer rating: ~0.3-0.5
            - MLB BABIP: ~0.6-0.8 (high regression)
            - Soccer xG: ~0.4-0.6

    Returns:
        Dict with reversion_expected, magnitude, confidence, z-score, etc.
    """
    if len(team_metric_history) < 3:
        return MeanReversionSignal(
            reversion_expected=False,
            magnitude=0.0,
            confidence=0.0,
            current_zscore=0.0,
            historical_mean=league_avg,
            current_value=float(np.mean(team_metric_history)) if team_metric_history else 0.0,
            half_life_games=float("inf"),
        ).__dict__

    data = np.array(team_metric_history, dtype=float)
    current_value = float(np.mean(data[-5:]))  # recent 5-game window

    if historical_mean is None:
        historical_mean = float(np.mean(data))

    # Blend historical mean with league average
    # The "true talent" is somewhere between the team's history and league avg
    # More data → lean more on team history. Less data → lean on league avg.
    sample_reliability = min(1.0, len(data) / 30.0)  # full reliability at 30+ games
    true_mean = sample_reliability * historical_mean + (1 - sample_reliability) * league_avg

    # Standard deviation from history
    hist_std = float(np.std(data))
    if hist_std < 1e-12:
        hist_std = abs(current_value - true_mean) * 0.5
        if hist_std < 1e-12:
            return MeanReversionSignal(
                reversion_expected=False,
                magnitude=0.0,
                confidence=0.0,
                current_zscore=0.0,
                historical_mean=round(true_mean, 4),
                current_value=round(current_value, 4),
                half_life_games=float("inf"),
            ).__dict__

    # Z-score: how far is current performance from expected true level?
    z = (current_value - true_mean) / hist_std

    # Expected reversion magnitude
    # Based on regression_rate: how much of the deviation we expect to revert
    deviation = current_value - true_mean
    expected_reversion = deviation * regression_rate
    reversion_magnitude = abs(expected_reversion)

    # Half-life in games: how quickly does this metric regress?
    # Half-life = -1 / log2(1 - regression_rate)
    if regression_rate > 0 and regression_rate < 1:
        half_life = -1.0 / math.log2(1.0 - regression_rate)
    else:
        half_life = float("inf")

    # Is reversion expected?
    # Yes if z-score is extreme enough (> 1 SD from mean)
    reversion_expected = abs(z) > 1.0

    # Confidence: based on z-score magnitude and sample size
    # Higher z → more confidence reversion will happen
    # More data → more confidence in the z-score
    z_confidence = float(2.0 * abs(stats.norm.cdf(z) - 0.5))  # 0 to 1
    sample_confidence = min(1.0, len(data) / 20.0)
    confidence = z_confidence * sample_confidence

    # Additional check: is the trend accelerating or decelerating?
    # If recent games are moving BACK toward the mean, reversion is already
    # underway → signal is less actionable.
    if len(data) >= 8:
        recent_half = data[len(data) // 2:]
        earlier_half = data[:len(data) // 2]
        recent_dev = abs(float(np.mean(recent_half)) - true_mean)
        earlier_dev = abs(float(np.mean(earlier_half)) - true_mean)
        if recent_dev < earlier_dev * 0.7:
            # Already reverting — reduce signal
            confidence *= 0.5
            reversion_magnitude *= 0.5

    result = MeanReversionSignal(
        reversion_expected=reversion_expected,
        magnitude=round(reversion_magnitude, 4),
        confidence=round(confidence, 4),
        current_zscore=round(z, 4),
        historical_mean=round(true_mean, 4),
        current_value=round(current_value, 4),
        half_life_games=round(half_life, 2),
    )

    if reversion_expected:
        direction = "downward" if z > 0 else "upward"
        logger.info("Mean reversion signal: %s (z=%.2f, magnitude=%.3f, confidence=%.2f)",
                     direction, z, reversion_magnitude, confidence)

    return result.__dict__
