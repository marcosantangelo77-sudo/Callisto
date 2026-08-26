"""Poisson and normal distribution helpers for low-scoring sports."""

import math


# ---------------------------------------------------------------------------
# 5. Poisson helpers for low-scoring sports
# ---------------------------------------------------------------------------


def poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (lam^k * e^-lam) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_total_distribution(
    home_expected: float,
    away_expected: float,
    max_score: int = 12,
) -> dict:
    """
    Generate full scoreline probability matrix using Poisson distribution.

    Returns:
        - scoreline_probs: dict of (home, away) -> probability
        - over_probs: dict of total_line -> P(over)
        - under_probs: dict of total_line -> P(under)
        - total_mean: expected total
        - total_std: standard deviation of total
    """
    scoreline_probs = {}
    for h in range(max_score + 1):
        for a in range(max_score + 1):
            prob = poisson_pmf(h, home_expected) * poisson_pmf(a, away_expected)
            scoreline_probs[(h, a)] = prob

    total_mean = home_expected + away_expected
    total_std = math.sqrt(home_expected + away_expected)

    # Over/under at every half-point
    over_probs = {}
    under_probs = {}
    for half in range(0, (max_score * 2 + 1) * 2):
        line = half * 0.5
        over_p = sum(p for (h, a), p in scoreline_probs.items() if h + a > line)
        over_probs[line] = round(over_p, 5)
        under_probs[line] = round(1.0 - over_p, 5)

    return {
        "scoreline_probs": scoreline_probs,
        "over_probs": over_probs,
        "under_probs": under_probs,
        "total_mean": round(total_mean, 2),
        "total_std": round(total_std, 2),
    }



def _normal_cdf(x: float) -> float:
    """
    Standard normal CDF using the complementary error function.
    P(Z <= x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
