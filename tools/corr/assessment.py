"""Human-readable assessment helpers for SGP mispricing analysis.

Extracted verbatim from tools/correlation.py.
"""

import math
from itertools import combinations

from tools.corr.lookup import _normalize_market, get_correlation
from tools.corr.odds import _american_to_implied, _implied_to_american


def _assess_mispricing(
    edge: float,
    mispricing_pct: float,
    has_anti: bool,
    avg_rho: float,
) -> str:
    """Generate a human-readable assessment of an SGP mispricing."""
    if has_anti:
        return (
            f"CAUTION — This parlay contains negatively correlated legs. "
            f"The true hit rate is LOWER than independent pricing suggests. "
            f"Mispricing: {mispricing_pct:+.1f}%. "
            f"{'Avoid this parlay.' if edge < 0 else 'Edge exists despite anti-correlation, but proceed with caution.'}"
        )

    if edge <= 0:
        return (
            f"NO EDGE — Book pricing is fair or favors the book. "
            f"Mispricing: {mispricing_pct:+.1f}%. Avg correlation: {avg_rho:.2f}. "
            f"The book may be applying a sufficient correlation penalty."
        )

    if mispricing_pct > 15:
        severity = "EXCEPTIONAL"
    elif mispricing_pct > 8:
        severity = "STRONG"
    elif mispricing_pct > 3:
        severity = "GOOD"
    else:
        severity = "MARGINAL"

    return (
        f"{severity} EDGE — Correlation mispricing of {mispricing_pct:+.1f}%. "
        f"Avg pairwise correlation: {avg_rho:.2f}. "
        f"Book is underpricing this parlay by treating correlated legs as independent. "
        f"True hit probability is {edge:.4f} higher than book implies."
    )


def _rate_correlation_edge(edge_pct: float, avg_rho: float) -> str:
    """Rate a parlay suggestion based on its correlation edge."""
    if avg_rho >= 0.5 and edge_pct >= 10:
        return "ELITE"
    elif avg_rho >= 0.4 and edge_pct >= 6:
        return "STRONG"
    elif avg_rho >= 0.3 and edge_pct >= 3:
        return "GOOD"
    elif edge_pct >= 1:
        return "MARGINAL"
    else:
        return "WEAK"


def detect_anti_correlation(legs: list[dict], sport: str) -> list[dict]:
    """
    Scan a set of parlay legs for anti-correlated pairs that make the
    parlay harder to hit than independently priced.

    This is the inverse of the edge — if you build a parlay with legs
    that fight each other (e.g., both QBs throwing 300+ in a game with
    a total of 38), the book is OVERPRICING the parlay in your favor...
    in the wrong direction. You'll hit it LESS often than the odds suggest.

    Args:
        legs: List of dicts with "market" and optionally "description".
        sport: Sport key.

    Returns:
        List of anti-correlated pairs with their correlations and warnings.
    """
    warnings = []

    for (i, j) in combinations(range(len(legs)), 2):
        market_a = legs[i].get("market", "")
        market_b = legs[j].get("market", "")
        rho = get_correlation(market_a, market_b, sport)

        if rho < -0.05:  # Meaningful negative correlation
            warnings.append({
                "leg_a": legs[i].get("description", market_a),
                "leg_b": legs[j].get("description", market_b),
                "market_a": market_a,
                "market_b": market_b,
                "correlation": rho,
                "severity": "HIGH" if rho < -0.25 else ("MODERATE" if rho < -0.15 else "LOW"),
                "warning": (
                    f"These legs are negatively correlated (rho={rho:.2f}). "
                    f"When one hits, the other is LESS likely to hit. "
                    f"The parlay is harder to win than the independent odds suggest."
                ),
            })

    return warnings


def estimate_sgp_vig(
    legs: list[dict],
    book_parlay_odds: int,
    sport: str,
) -> dict:
    """
    Estimate how much extra vig (juice) the book is charging on an SGP
    beyond standard parlay vig.

    Books apply a "correlation tax" to SGPs — they know some legs are
    correlated and mark up the price. The question is: are they charging
    MORE or LESS than the actual correlation warrants?

    If the book's SGP vig exceeds the true correlation adjustment, the
    parlay is overpriced (bad for us). If it's less, the parlay is
    underpriced (edge for us).

    Args:
        legs: Parlay legs with "american_odds" and "market".
        book_parlay_odds: The SGP odds the book is offering.
        sport: Sport key.

    Returns:
        Breakdown of vig components.
    """
    marginals = [_american_to_implied(leg.get("american_odds", -110)) for leg in legs]

    # Independent joint
    independent_joint = 1.0
    for p in marginals:
        independent_joint *= p

    # Book's implied
    book_implied = _american_to_implied(book_parlay_odds)

    # Standard parlay vig (what a normal uncorrelated parlay would be juiced to)
    # Books typically charge ~10-20% vig on parlays via individual leg juice
    standard_vig_joint = independent_joint  # already includes per-leg vig

    # The SGP-specific adjustment the book made
    sgp_adjustment = book_implied - independent_joint

    # Our true correlation adjustment
    true_adjustment = 0.0
    for (i, j) in combinations(range(len(legs)), 2):
        market_a = _normalize_market(legs[i].get("market", ""))
        market_b = _normalize_market(legs[j].get("market", ""))
        rho = get_correlation(market_a, market_b, sport)
        if rho == 0:
            continue
        sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
        sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
        other_product = 1.0
        for k in range(len(marginals)):
            if k != i and k != j:
                other_product *= marginals[k]
        true_adjustment += rho * sigma_a * sigma_b * other_product

    # The extra vig beyond correlation
    extra_vig = sgp_adjustment - true_adjustment

    return {
        "independent_prob": round(independent_joint, 6),
        "book_implied_prob": round(book_implied, 6),
        "true_correlated_prob": round(independent_joint + true_adjustment, 6),
        "sgp_adjustment_book": round(sgp_adjustment, 6),
        "sgp_adjustment_true": round(true_adjustment, 6),
        "extra_sgp_vig": round(extra_vig, 6),
        "extra_sgp_vig_pct": round((extra_vig / independent_joint * 100) if independent_joint > 0 else 0, 2),
        "independent_odds": _implied_to_american(independent_joint),
        "book_odds": book_parlay_odds,
        "fair_odds": _implied_to_american(independent_joint + true_adjustment),
        "assessment": (
            f"Book charges {abs(sgp_adjustment):.4f} SGP adjustment vs "
            f"true correlation of {true_adjustment:+.4f}. "
            + (
                f"Book is UNDERCHARGING by {abs(extra_vig):.4f} — edge exists."
                if extra_vig < -0.001
                else (
                    f"Book is OVERCHARGING by {extra_vig:.4f} — no edge, avoid."
                    if extra_vig > 0.001
                    else "Book pricing is approximately fair."
                )
            )
        ),
    }
