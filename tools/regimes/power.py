"""
Regime-aware power ratings.

Extracted from the original ``tools/regime.py`` (section 3).
"""

import logging
import math
from dataclasses import dataclass

import numpy as np

from tools.regimes.changepoint import analyze_regimes

logger = logging.getLogger("callisto.regime")


@dataclass
class PowerRating:
    """Regime-aware power rating for a team."""
    rating: float
    regime: str           # "improving", "declining", "stable", "volatile"
    confidence: float     # 0-1
    regime_rating: float  # rating from current regime only
    season_rating: float  # full season rating
    regime_start_index: int  # when current regime began
    regime_games: int     # how many games in current regime


def _classify_regime(segment_means: list[float], current_variance: float,
                     season_std: float) -> str:
    """
    Classify the current regime based on segment trajectory.

    Returns: "improving", "declining", "stable", or "volatile"
    """
    if len(segment_means) < 2:
        return "stable"

    # Look at the last two segments
    prev = segment_means[-2]
    curr = segment_means[-1]
    diff = curr - prev

    # Volatile: if current segment variance is much higher than season
    if current_variance > 0 and season_std > 0:
        if math.sqrt(current_variance) > 1.5 * season_std:
            return "volatile"

    # Threshold for meaningful change: 0.5 season SDs
    threshold = 0.5 * season_std if season_std > 0 else abs(diff) * 0.3

    if diff > threshold:
        return "improving"
    elif diff < -threshold:
        return "declining"
    else:
        return "stable"


def calculate_power_rating(team_data: dict,
                           regime_weight: float = 0.6) -> dict:
    """
    Calculate a regime-aware power rating for a team.

    Instead of blindly averaging the whole season, we:
    1. Detect regime changes in the performance data
    2. Weight the current regime more heavily
    3. But also check if the "regime change" passes statistical tests

    Parameters:
        team_data: Dict with:
            - "performance_history": list[float] — game-by-game efficiency
            - "name": str — team name (for logging)
            - "league_avg": float (optional) — league average for normalization
        regime_weight: How much to weight the current regime vs full season.
            0.6 = 60% current regime, 40% full season. Adjustable.

    Returns:
        Dict with rating, regime, confidence, component ratings.
    """
    history = team_data.get("performance_history", [])
    name = team_data.get("name", "Unknown")
    league_avg = team_data.get("league_avg", None)

    if not history or len(history) < 4:
        season_mean = float(np.mean(history)) if history else 0.0
        return PowerRating(
            rating=round(season_mean, 4),
            regime="insufficient_data",
            confidence=0.0,
            regime_rating=season_mean,
            season_rating=season_mean,
            regime_start_index=0,
            regime_games=len(history),
        ).__dict__

    data = np.array(history, dtype=float)
    season_mean = float(np.mean(data))
    season_std = float(np.std(data))

    # Detect regime changes
    regime_result = analyze_regimes(history)

    if regime_result.indices:
        # Current regime starts at the last change point
        last_cp = regime_result.indices[-1]
        current_regime_data = data[last_cp:]
        regime_mean = float(np.mean(current_regime_data))
        regime_var = float(np.var(current_regime_data))
        regime_games = len(current_regime_data)

        # Classify the regime
        regime_label = _classify_regime(
            regime_result.segment_means, regime_var, season_std
        )

        # Adjust regime weight by confidence.
        # If the change point isn't very confident, lean more on season average.
        effective_weight = regime_weight * regime_result.confidence
        effective_weight = max(0.2, min(0.9, effective_weight))

        # Also adjust by regime sample size — tiny regimes get less weight
        sample_factor = min(1.0, regime_games / 8.0)  # full weight at 8+ games
        effective_weight *= sample_factor
        effective_weight = max(0.2, effective_weight)  # floor

        # Blended rating
        rating = effective_weight * regime_mean + (1 - effective_weight) * season_mean

        # Confidence in the rating: based on regime detection confidence
        # and sample size
        confidence = regime_result.confidence * sample_factor

    else:
        # No regime change detected — use full season with slight recency tilt
        # Apply exponential weighting: recent games matter a bit more
        n = len(data)
        weights = np.exp(np.linspace(-1, 0, n))  # gentle exponential decay
        weights /= weights.sum()
        rating = float(np.dot(data, weights))

        regime_label = "stable"
        regime_mean = season_mean
        regime_games = len(data)
        last_cp = 0
        confidence = 0.7  # moderate confidence in stable regime

    # Normalize to league average if provided
    if league_avg is not None and league_avg != 0:
        rating = rating - league_avg  # positive = above average

    result = PowerRating(
        rating=round(rating, 4),
        regime=regime_label,
        confidence=round(confidence, 4),
        regime_rating=round(regime_mean, 4),
        season_rating=round(season_mean, 4),
        regime_start_index=last_cp,
        regime_games=regime_games,
    )

    logger.info("%s power rating: %.2f (regime=%s, confidence=%.2f)",
                name, result.rating, result.regime, result.confidence)

    return result.__dict__
