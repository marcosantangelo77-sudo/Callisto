"""
Simultaneous Kelly for correlated multi-bet portfolios
(split from tools/kelly.py).
"""

import math

import numpy as np

from tools.kellypkg.constants import AGP_TIER_MULTIPLIERS
from tools.kellypkg.core import kelly_fractional
from tools.kellypkg.odds import _confidence_tier_from_score


def kelly_portfolio(bets):
    """
    Optimal simultaneous Kelly sizing for a portfolio of open bets.

    Each bet has:
        - edge: float (decimal)
        - odds: int (American)
        - correlation_with_others: float (-1 to 1, average pairwise correlation)
        - Optional: confidence_score, variance_estimate, description

    Correlated bets reduce effective bankroll.  Two bets on the same game
    (e.g., spread and total) at correlation 0.3 should be sized as if the
    bankroll is smaller.  Perfectly correlated bets (same outcome, different
    books) should be treated as one position.

    The approach:
    1. Compute independent Kelly for each bet.
    2. Build a correlation-adjusted budget: total Kelly allocation is capped
       at a portfolio-level maximum.
    3. Scale each bet proportionally if the sum exceeds the cap.
    4. Apply correlation penalties: higher correlation -> more reduction.

    Args:
        bets: List of bet dicts, each with at minimum {edge, odds, correlation_with_others}.

    Returns:
        List of dicts with sizing info for each bet, plus portfolio summary.
    """
    if not bets:
        return []

    n = len(bets)

    # Step 1: Individual Kelly fractions
    individual_kellys = []
    for bet in bets:
        edge = bet.get("edge", 0.0)
        odds = bet.get("odds", -110)
        conf = bet.get("confidence_score", 0.75)  # default CORROBORATED
        var_est = bet.get("variance_estimate", abs(edge) * 0.5)

        # Use dynamic Kelly for each
        base_frac = kelly_fractional(edge, odds, fraction=0.25)

        # Confidence adjustment
        tier = _confidence_tier_from_score(conf)
        tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)
        adj_frac = base_frac * tier_mult

        individual_kellys.append({
            "raw_fraction": round(base_frac, 6),
            "confidence_adjusted": round(adj_frac, 6),
            "tier": tier,
        })

    # Step 2: Correlation-adjusted portfolio allocation
    # Build a simple correlation matrix from pairwise correlation estimates
    correlations = np.array([bet.get("correlation_with_others", 0.0) for bet in bets])

    # Portfolio variance scaling: for N bets with average pairwise correlation rho,
    # the portfolio variance scales as:
    #   var_portfolio = N * var_individual * (1 + (N-1) * rho) / N
    #                 = var_individual * (1 + (N-1) * rho)
    # We use this to compute a correlation penalty factor.
    avg_correlation = float(np.mean(np.clip(correlations, -1.0, 1.0)))
    # Effective diversification ratio: 1.0 = fully diversified, higher = concentrated
    diversification_ratio = 1.0 + max(0.0, (n - 1) * avg_correlation)
    # Penalty: scale down total allocation as correlation increases
    # At rho=0: penalty=1.0 (no penalty).  At rho=1: penalty = 1/sqrt(N).
    correlation_penalty = 1.0 / math.sqrt(max(1.0, diversification_ratio))

    # Step 3: Portfolio-level cap
    # Total simultaneous Kelly allocation should not exceed 20% of bankroll.
    # This is the "don't blow up" constraint.
    PORTFOLIO_CAP = 0.20
    raw_total = sum(ik["confidence_adjusted"] for ik in individual_kellys)
    penalized_total = raw_total * correlation_penalty

    if penalized_total > PORTFOLIO_CAP:
        scale_factor = PORTFOLIO_CAP / penalized_total
    else:
        scale_factor = correlation_penalty

    # Step 4: Per-bet correlation adjustment
    # Bets with higher individual correlation get penalized more.
    results = []
    for i, bet in enumerate(bets):
        ik = individual_kellys[i]
        rho_i = max(0.0, correlations[i])

        # Individual correlation penalty: additional reduction for highly correlated bets
        # A bet with rho=0.8 gets an extra 20% reduction on top of the portfolio scaling.
        individual_corr_penalty = 1.0 - (rho_i * 0.25)
        individual_corr_penalty = max(0.1, individual_corr_penalty)

        final_fraction = ik["confidence_adjusted"] * scale_factor * individual_corr_penalty
        # Per-bet hard cap at 5%
        final_fraction = min(final_fraction, 0.05)

        results.append({
            "description": bet.get("description", f"Bet {i+1}"),
            "edge": bet.get("edge", 0.0),
            "odds": bet.get("odds", -110),
            "independent_kelly": ik["raw_fraction"],
            "confidence_adjusted_kelly": ik["confidence_adjusted"],
            "correlation": round(correlations[i], 3),
            "individual_corr_penalty": round(individual_corr_penalty, 4),
            "final_fraction": round(final_fraction, 6),
            "final_pct": round(final_fraction * 100, 3),
            "tier": ik["tier"],
        })

    # Portfolio summary
    total_allocated = sum(r["final_fraction"] for r in results)
    portfolio_summary = {
        "bet_count": n,
        "avg_correlation": round(avg_correlation, 4),
        "diversification_ratio": round(diversification_ratio, 4),
        "correlation_penalty": round(correlation_penalty, 4),
        "raw_total_allocation": round(raw_total, 6),
        "final_total_allocation": round(total_allocated, 6),
        "final_total_pct": round(total_allocated * 100, 3),
        "portfolio_cap": PORTFOLIO_CAP,
        "cap_hit": penalized_total > PORTFOLIO_CAP,
    }

    # Attach summary to each result for easy access
    for r in results:
        r["portfolio_summary"] = portfolio_summary

    return results
