"""Odds conversion helpers and joint-probability math.

Extracted verbatim from tools/correlation.py.
"""

import math

from tools.odds_api import calculate_implied_probability


def _american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability (no vig removal)."""
    return calculate_implied_probability(odds)


def _implied_to_american(prob: float) -> int:
    """Convert a probability to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    else:
        return int(100 * (1 - prob) / prob)


def _prob_to_decimal(prob: float) -> float:
    """Convert probability to decimal odds."""
    if prob <= 0:
        return float("inf")
    return 1.0 / prob


def _adjust_joint_probability(
    prob_a: float,
    prob_b: float,
    rho: float,
) -> float:
    """
    Calculate the joint probability of two events given their correlation.

    Uses the Gaussian copula approximation:
        P(A and B) = P(A)*P(B) + rho * sqrt(P(A)*(1-P(A)) * P(B)*(1-P(B)))

    This comes from the bivariate normal relationship where:
        Cov(X,Y) = rho * sigma_X * sigma_Y

    For Bernoulli variables, sigma = sqrt(p*(1-p)), so:
        P(A and B) = E[X]*E[Y] + Cov(X,Y)
                    = p_a * p_b + rho * sqrt(p_a*(1-p_a)) * sqrt(p_b*(1-p_b))

    The result is clamped to [0, min(p_a, p_b)] for validity.

    Args:
        prob_a: Marginal probability of event A
        prob_b: Marginal probability of event B
        rho: Pearson correlation coefficient (-1 to 1)

    Returns:
        Adjusted joint probability.
    """
    independent = prob_a * prob_b
    sigma_a = math.sqrt(prob_a * (1 - prob_a)) if 0 < prob_a < 1 else 0.0
    sigma_b = math.sqrt(prob_b * (1 - prob_b)) if 0 < prob_b < 1 else 0.0
    adjustment = rho * sigma_a * sigma_b
    joint = independent + adjustment

    # Clamp to valid probability range
    # Frechet-Hoeffding bounds: max(0, p_a + p_b - 1) <= P(A,B) <= min(p_a, p_b)
    lower_bound = max(0.0, prob_a + prob_b - 1.0)
    upper_bound = min(prob_a, prob_b)
    joint = max(lower_bound, min(upper_bound, joint))

    return joint
