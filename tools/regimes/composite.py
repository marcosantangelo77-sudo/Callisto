"""
Composite: full regime analysis pipeline for a single team.

Extracted from the original ``tools/regime.py`` (final section).
"""

import logging

import numpy as np

from tools.regimes.changepoint import analyze_regimes
from tools.regimes.recency import recency_bias_score
from tools.regimes.power import calculate_power_rating
from tools.regimes.bayes import seasonal_bayesian_rating
from tools.regimes.reversion import mean_reversion_signal

logger = logging.getLogger("callisto.regime")


def full_regime_analysis(team_data: dict, sport: str = "nba") -> dict:
    """
    Run the complete regime analysis pipeline for a single team.

    Combines all five modules into a single assessment:
    1. Detect regime changes in performance data
    2. Quantify recency bias
    3. Calculate regime-aware power rating
    4. Apply Bayesian prior management
    5. Check for mean reversion signals

    Parameters:
        team_data: Dict with:
            - "name": str
            - "performance_history": list[float] — game-by-game efficiency
            - "prior_rating": float (optional) — preseason/prior-year rating
            - "league_avg": float (optional) — league average
            - "recent_window": int (optional) — games for recency analysis (default 5)

    Returns:
        Dict with all analysis results combined.
    """
    name = team_data.get("name", "Unknown")
    history = team_data.get("performance_history", [])
    prior_rating = team_data.get("prior_rating")
    league_avg = team_data.get("league_avg", float(np.mean(history)) if history else 0.0)
    recent_window = team_data.get("recent_window", 5)

    logger.info("Running full regime analysis for %s (%d games)", name, len(history))

    results = {"team": name, "games_analyzed": len(history)}

    # 1. Regime detection
    results["regime_changes"] = analyze_regimes(history).__dict__ if len(history) >= 6 else None

    # 2. Recency bias
    if len(history) >= recent_window + 3:
        recent = history[-recent_window:]
        results["recency_bias"] = recency_bias_score(recent, history)
    else:
        results["recency_bias"] = None

    # 3. Power rating
    results["power_rating"] = calculate_power_rating(team_data)

    # 4. Bayesian update
    if prior_rating is not None:
        results["bayesian_rating"] = seasonal_bayesian_rating(
            prior_rating, history, sport=sport
        )
    else:
        results["bayesian_rating"] = None

    # 5. Mean reversion
    if len(history) >= 8:
        results["mean_reversion"] = mean_reversion_signal(
            history, league_avg
        )
    else:
        results["mean_reversion"] = None

    # Composite signal: combine everything into an actionable summary
    signals = []
    if results["recency_bias"] and results["recency_bias"]["bias_magnitude"] > 0.3:
        signals.append(f"recency_bias_{results['recency_bias']['bias_direction']}")
    if results["mean_reversion"] and results["mean_reversion"]["reversion_expected"]:
        direction = "down" if results["mean_reversion"]["current_zscore"] > 0 else "up"
        signals.append(f"mean_reversion_{direction}")
    if results["power_rating"]["regime"] in ("improving", "declining"):
        signals.append(f"regime_{results['power_rating']['regime']}")

    results["actionable_signals"] = signals
    results["has_edge_signal"] = len(signals) > 0

    logger.info("%s analysis complete: %d actionable signal(s): %s",
                name, len(signals), signals)

    return results
