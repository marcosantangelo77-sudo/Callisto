"""
SGP (Same Game Parlay) correlation exploitation engine.

Books price SGP legs as independent (multiply probabilities).
When legs are positively correlated, the TRUE joint probability is
HIGHER than the book's calculation -> book underprices -> +EV for us.

Uses Gaussian copula to adjust for correlation.
No scipy dependency — implements the normal CDF/PPF via pure Python.
"""

import logging
import math
from typing import Optional

from tools.math_utils import american_to_decimal, american_to_implied, fair_prob_to_american

logger = logging.getLogger("callisto.sgp")


# ──────────────────────────────────────────────────
# CORRELATION PRIORS (from sports analytics research)
# Each entry is (low_estimate, high_estimate)
# For go/no-go: use LOWER BOUND (conservative)
# For sizing: use midpoint
# ──────────────────────────────────────────────────

CORRELATION_PRIORS = {
    "nba": {
        ("team_total_over", "player_pts_over_same_team"): (0.35, 0.55),
        ("game_total_over", "player_pts_over"): (0.25, 0.45),
        ("team_spread_cover", "team_total_over"): (0.10, 0.30),
        ("player_assists_over", "game_total_over"): (0.15, 0.35),
        ("team_win", "opp_player_pts_under"): (0.05, 0.25),
        ("player_pts_over", "player_reb_over"): (0.10, 0.25),
        ("player_pts_over", "player_ast_over"): (0.05, 0.20),
        ("game_total_over", "player_reb_over"): (0.15, 0.30),
    },
    "nfl": {
        ("qb_pass_yds_over", "team_total_over"): (0.35, 0.55),
        ("team_win", "team_total_over"): (0.20, 0.40),
        ("rb_rush_yds_over", "team_win_favorite"): (0.15, 0.35),
        ("qb_pass_tds_over", "qb_pass_yds_over"): (0.25, 0.45),
        ("wr_rec_yds_over", "qb_pass_yds_over"): (0.30, 0.50),
        ("game_total_over", "qb_pass_yds_over"): (0.25, 0.45),
    },
    "mlb": {
        ("team_total_over", "player_hits_over"): (0.25, 0.45),
        ("game_total_over", "player_rbi_over"): (0.20, 0.40),
        ("pitcher_ks_over", "opp_total_under"): (0.15, 0.35),
    },
}


# ──────────────────────────────────────────────────
# PURE PYTHON NORMAL CDF/PPF (no scipy needed)
# ──────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via Abramowitz & Stegun approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    """
    Standard normal inverse CDF (PPF) via rational approximation.
    Accurate to ~4.5e-4 absolute error.
    """
    if p <= 0:
        return -10.0
    if p >= 1:
        return 10.0
    if p == 0.5:
        return 0.0

    if p < 0.5:
        t = math.sqrt(-2 * math.log(p))
        sign = -1
    else:
        t = math.sqrt(-2 * math.log(1 - p))
        sign = 1

    # Rational approximation coefficients
    c0 = 2.515517
    c1 = 0.802853
    c2 = 0.010328
    d1 = 1.432788
    d2 = 0.189269
    d3 = 0.001308

    x = t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)
    return sign * x


def _bivariate_normal_cdf(x: float, y: float, rho: float) -> float:
    """
    Bivariate normal CDF approximation using Drezner & Wesolowsky (1990).
    P(X <= x, Y <= y) with correlation rho.
    """
    if abs(rho) < 0.001:
        return _norm_cdf(x) * _norm_cdf(y)

    if rho == 1.0:
        return _norm_cdf(min(x, y))
    if rho == -1.0:
        if x + y >= 0:
            return max(_norm_cdf(x) + _norm_cdf(y) - 1, 0)
        else:
            return 0.0

    # Drezner-Wesolowsky approximation
    if abs(rho) < 0.925:
        # Gauss-Legendre quadrature points and weights
        points = [0.04691008, 0.23076534, 0.50000000, 0.76923466, 0.95308992]
        weights = [0.01846567, 0.09713858, 0.16666667, 0.09713858, 0.01846567]

        # Scale to [0, arcsin(rho)]
        asr = math.asin(rho)
        result = 0.0
        for i, (p, w) in enumerate(zip(points, weights)):
            sin_val = math.sin(asr * p)
            cos_val = math.sqrt(1 - sin_val * sin_val)
            if cos_val > 0:
                result += w * math.exp(-(x * x + y * y - 2 * x * y * sin_val) / (2 * cos_val * cos_val))

        result *= asr / (2 * math.pi)
        result += _norm_cdf(x) * _norm_cdf(y)
        return max(0, min(1, result))

    # High correlation: use identity
    # P(X<=x, Y<=y; rho) = P(X<=x) - P(X<=x, Y>y; rho)
    # Decompose for numerical stability
    if rho > 0:
        return max(0, min(1, _norm_cdf(x) + _norm_cdf(y) - 1 +
                          _bivariate_normal_cdf(-x, -y, rho)))
    else:
        return max(0, min(1, max(_norm_cdf(x) - _norm_cdf(-y) +
                                 _bivariate_normal_cdf(-x, y, -rho), 0)))


# ──────────────────────────────────────────────────
# GAUSSIAN COPULA JOINT PROBABILITY
# ──────────────────────────────────────────────────

def correlated_parlay_prob(
    leg_probs: list[float],
    correlation_matrix: list[list[float]],
) -> float:
    """
    Gaussian copula joint probability for N legs.

    For 2 legs: uses bivariate normal CDF directly.
    For N > 2 legs: pairwise approximation (product of pairwise adjustments).

    Verified:
      [0.55, 0.60], rho=0.40 -> joint~0.356 vs naive=0.330 (+2.6 cents)
      [0.55, 0.60], rho=0.00 -> joint=0.330 = naive
      [0.55, 0.60], rho=-0.3 -> joint~0.304 < naive
    """
    n = len(leg_probs)
    clamped = [max(0.001, min(0.999, p)) for p in leg_probs]

    if n == 1:
        return clamped[0]

    if n == 2:
        z0 = _norm_ppf(clamped[0])
        z1 = _norm_ppf(clamped[1])
        rho = correlation_matrix[0][1] if len(correlation_matrix) > 1 else 0
        return _bivariate_normal_cdf(z0, z1, rho)

    # N > 2: pairwise approximation
    # Start with independent probability, then adjust for each pair's correlation
    naive = 1.0
    for p in clamped:
        naive *= p

    adjustment = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            rho = correlation_matrix[i][j] if i < len(correlation_matrix) and j < len(correlation_matrix[i]) else 0
            if abs(rho) < 0.001:
                continue
            # Pairwise adjustment
            pair_indep = clamped[i] * clamped[j]
            pair_corr = _bivariate_normal_cdf(
                _norm_ppf(clamped[i]), _norm_ppf(clamped[j]), rho
            )
            if pair_indep > 0:
                adjustment *= pair_corr / pair_indep

    return max(0, min(1, naive * adjustment))


def evaluate_sgp(
    legs: list[dict],
    sport: str,
    book_sgp_decimal: float,
) -> dict:
    """
    Evaluate a Same Game Parlay for edge.

    Each leg: {'type': str, 'fair_prob': float}

    For go/no-go: use LOWER BOUND of correlation range (conservative).
    For sizing: use midpoint.

    Returns edge under both conservative and midpoint assumptions.
    """
    sport_priors = CORRELATION_PRIORS.get(sport.lower(), {})
    n = len(legs)

    # Build correlation matrix
    corr_low = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    corr_mid = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    pair_details = []
    for i in range(n):
        for j in range(i + 1, n):
            key = (legs[i]["type"], legs[j]["type"])
            key_rev = (legs[j]["type"], legs[i]["type"])
            rho_range = sport_priors.get(key) or sport_priors.get(key_rev)

            if rho_range:
                low, high = rho_range
                mid = (low + high) / 2
                corr_low[i][j] = corr_low[j][i] = low
                corr_mid[i][j] = corr_mid[j][i] = mid
                pair_details.append({
                    "legs": f"{legs[i]['type']} + {legs[j]['type']}",
                    "rho_low": low,
                    "rho_high": high,
                    "rho_mid": mid,
                })

    probs = [leg["fair_prob"] for leg in legs]
    naive_prob = 1.0
    for p in probs:
        naive_prob *= p

    conservative_prob = correlated_parlay_prob(probs, corr_low)
    midpoint_prob = correlated_parlay_prob(probs, corr_mid)

    book_implied = 1 / book_sgp_decimal if book_sgp_decimal > 0 else 0

    return {
        "legs": len(legs),
        "naive_joint_prob": round(naive_prob, 4),
        "conservative_prob": round(conservative_prob, 4),
        "midpoint_prob": round(midpoint_prob, 4),
        "book_implied": round(book_implied, 4),
        "book_sgp_decimal": round(book_sgp_decimal, 4),
        "edge_conservative": round((conservative_prob - book_implied) * 100, 2),
        "edge_midpoint": round((midpoint_prob - book_implied) * 100, 2),
        "ev_conservative": round((conservative_prob * book_sgp_decimal - 1) * 100, 2),
        "ev_midpoint": round((midpoint_prob * book_sgp_decimal - 1) * 100, 2),
        "actionable_conservative": conservative_prob > book_implied,
        "actionable_midpoint": midpoint_prob > book_implied,
        "correlation_pairs": pair_details,
    }
