"""Pure-Python statistics helpers (no scipy dependency)."""

import math


def _erfc(x: float) -> float:
    """Complementary error function (Abramowitz & Stegun 7.1.26)."""
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
           t * (-1.453152027 + t * 1.061405429))))
    result = poly * math.exp(-x * x)
    return result if x >= 0 else 2.0 - result


def _norm_sf(x: float) -> float:
    """P(Z > x) for standard normal."""
    return 1.0 - 0.5 * _erfc(-x / math.sqrt(2))


def _binomial_pvalue(wins: int, total: int, expected_rate: float = 0.5) -> float:
    """One-sided binomial test via normal approximation with continuity correction."""
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 1.0
    mean = total * expected_rate
    std = math.sqrt(total * expected_rate * (1 - expected_rate))
    if std < 1e-9:
        return 1.0
    z = (wins - 0.5 - mean) / std
    return _norm_sf(z)

