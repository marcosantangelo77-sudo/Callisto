"""
Bayesian prior management — seasonal weight schedules and conjugate updates.

Extracted from the original ``tools/regime.py`` (section 4).
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger("callisto.regime")


@dataclass
class BayesianResult:
    """Result of Bayesian prior update."""
    posterior: float
    prior_decay_applied: float   # how much the prior was decayed
    prior_contribution: float    # portion of posterior from prior
    evidence_contribution: float  # portion from evidence
    credible_interval: tuple[float, float]  # 90% credible interval


def prior_weight_schedule(games_played: int, sport: str = "nba") -> float:
    """
    How much to weight priors vs current-season data.

    Early season: lean heavily on priors (last year + offseason changes).
    Mid season: balanced.
    Late season: mostly current data.

    The schedule follows a logistic decay — the transition from prior-heavy
    to data-heavy is smooth, not a hard cutoff.

    Parameters:
        games_played: Number of games completed this season.
        sport: Sport determines the season length and transition points.

    Returns:
        Float 0-1. 1.0 = full prior weight, 0.0 = ignore prior entirely.
    """
    # Season lengths and transition midpoints by sport
    schedules = {
        "nba": {"season_length": 82, "midpoint": 25, "steepness": 0.12},
        "nfl": {"season_length": 17, "midpoint": 6, "steepness": 0.5},
        "mlb": {"season_length": 162, "midpoint": 50, "steepness": 0.06},
        "nhl": {"season_length": 82, "midpoint": 25, "steepness": 0.12},
        "ncaab": {"season_length": 35, "midpoint": 12, "steepness": 0.25},
        "ncaaf": {"season_length": 12, "midpoint": 4, "steepness": 0.6},
        "soccer": {"season_length": 38, "midpoint": 12, "steepness": 0.2},
    }

    params = schedules.get(sport.lower(), schedules["nba"])
    midpoint = params["midpoint"]
    steepness = params["steepness"]

    # Logistic decay: starts near 1.0, transitions to near 0.0
    # w = 1 / (1 + exp(steepness * (games - midpoint)))
    prior_w = 1.0 / (1.0 + math.exp(steepness * (games_played - midpoint)))

    # Floor at 0.05 — never completely ignore priors
    # (even late-season, a small prior prevents overfitting to recent noise)
    prior_w = max(0.05, prior_w)

    return round(prior_w, 4)


def bayesian_update(prior: float, evidence: list[float],
                    prior_weight: float = 0.5,
                    prior_variance: Optional[float] = None) -> dict:
    """
    Bayesian update of a team rating given new evidence.

    Uses conjugate normal-normal model:
    - Prior: N(prior, prior_variance)
    - Likelihood: N(evidence_mean, evidence_variance / n)
    - Posterior: weighted combination

    Parameters:
        prior: Prior estimate (e.g., last season's rating + offseason adjustments).
        evidence: List of observed values this season.
        prior_weight: 0-1, how much to weight the prior. Use prior_weight_schedule().
        prior_variance: Uncertainty in the prior. If None, estimated from evidence.

    Returns:
        Dict with posterior, prior_decay_applied, contributions, credible interval.
    """
    if not evidence:
        return BayesianResult(
            posterior=prior,
            prior_decay_applied=0.0,
            prior_contribution=1.0,
            evidence_contribution=0.0,
            credible_interval=(prior - 2.0, prior + 2.0),
        ).__dict__

    ev = np.array(evidence, dtype=float)
    ev_mean = float(np.mean(ev))
    ev_var = float(np.var(ev, ddof=1)) if len(ev) > 1 else 1.0
    n = len(ev)

    # Estimate prior variance if not provided
    if prior_variance is None:
        # Use evidence variance as a rough prior variance estimate
        # Scale up because prior uncertainty is typically larger
        prior_variance = ev_var * 2.0
    prior_variance = max(prior_variance, 1e-6)

    # Precision (inverse variance) formulation for conjugate update
    prior_precision = prior_weight / prior_variance
    evidence_precision = (1.0 - prior_weight) * n / ev_var if ev_var > 0 else 0.0
    total_precision = prior_precision + evidence_precision

    if total_precision < 1e-12:
        posterior = (prior + ev_mean) / 2.0
        posterior_var = prior_variance
    else:
        # Posterior mean: precision-weighted average
        posterior = (prior_precision * prior + evidence_precision * ev_mean) / total_precision
        posterior_var = 1.0 / total_precision

    # Contributions
    prior_contribution = prior_precision / total_precision if total_precision > 0 else 0.5
    evidence_contribution = 1.0 - prior_contribution

    # 90% credible interval
    posterior_std = math.sqrt(posterior_var)
    z90 = 1.645
    ci_low = posterior - z90 * posterior_std
    ci_high = posterior + z90 * posterior_std

    # Prior decay: how much the prior was discounted from its original value
    prior_decay = 1.0 - prior_weight

    result = BayesianResult(
        posterior=round(posterior, 4),
        prior_decay_applied=round(prior_decay, 4),
        prior_contribution=round(prior_contribution, 4),
        evidence_contribution=round(evidence_contribution, 4),
        credible_interval=(round(ci_low, 4), round(ci_high, 4)),
    )

    logger.debug("Bayesian update: prior=%.2f → posterior=%.2f "
                 "(prior_contrib=%.2f, evidence_contrib=%.2f)",
                 prior, result.posterior,
                 result.prior_contribution, result.evidence_contribution)

    return result.__dict__


def seasonal_bayesian_rating(prior_rating: float,
                             game_metrics: list[float],
                             sport: str = "nba",
                             prior_variance: Optional[float] = None) -> dict:
    """
    Convenience function: applies prior_weight_schedule + bayesian_update.

    Takes a prior (e.g., preseason projection) and the season's game-by-game
    metrics, automatically determines the right prior/evidence balance based
    on how many games have been played, and returns the updated rating.

    Parameters:
        prior_rating: Preseason or prior-year rating.
        game_metrics: This season's game-by-game performance values.
        sport: For weight schedule calculation.
        prior_variance: Prior uncertainty. If None, estimated.

    Returns:
        BayesianResult dict with the updated rating.
    """
    games_played = len(game_metrics)
    pw = prior_weight_schedule(games_played, sport)
    return bayesian_update(prior_rating, game_metrics, prior_weight=pw,
                           prior_variance=prior_variance)
