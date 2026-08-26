"""Parlay pricing: independent and correlation-adjusted odds.

Extracted verbatim from tools/correlation.py.
"""

import logging
import math
from itertools import combinations
from typing import Optional

from tools.corr.assessment import _assess_mispricing, _rate_correlation_edge
from tools.corr.lookup import _normalize_market, get_correlation
from tools.corr.odds import _american_to_implied, _implied_to_american

logger = logging.getLogger("callisto.correlation")


def independent_parlay_odds(legs: list[dict]) -> int:
    """
    Calculate parlay odds assuming all legs are independent.

    This is how books price parlays — multiply the individual implied
    probabilities together. The result is the "naive" parlay price.

    Args:
        legs: List of dicts, each with "american_odds" (int).

    Returns:
        American odds for the parlay assuming full independence.
    """
    if not legs:
        return 0

    joint_prob = 1.0
    for leg in legs:
        odds = leg.get("american_odds", -110)
        prob = _american_to_implied(odds)
        joint_prob *= prob

    if joint_prob <= 0 or joint_prob >= 1:
        return 0
    return _implied_to_american(joint_prob)


def correlated_parlay_odds(legs: list[dict], correlations: Optional[dict] = None, sport: str = "nfl") -> int:
    """
    Calculate parlay odds adjusted for correlations between legs.

    Processes legs pairwise: for each pair of legs with a non-zero correlation,
    the joint probability is adjusted upward (positive correlation) or downward
    (negative correlation) relative to the independent product.

    For N legs, we:
    1. Start with the independent joint probability (product of all marginals).
    2. For each correlated pair, compute the adjustment delta:
       delta = rho * sigma_A * sigma_B
    3. Sum all pairwise deltas and apply to the independent product.

    This is an approximation — the full multivariate copula is intractable
    for arbitrary N, but the pairwise adjustment captures the dominant effect
    and is the standard approach in quantitative sports betting.

    Args:
        legs: List of dicts with "american_odds" and "market" keys.
        correlations: Optional dict mapping (market_a, market_b) -> rho.
                      If None, uses the sport's default correlation matrix.
        sport: Sport for default correlation lookup.

    Returns:
        American odds for the correlation-adjusted parlay.
    """
    if not legs:
        return 0

    # Get marginal probabilities
    probs = []
    for leg in legs:
        odds = leg.get("american_odds", -110)
        probs.append(_american_to_implied(odds))

    # Independent product
    independent_joint = 1.0
    for p in probs:
        independent_joint *= p

    # Sum pairwise correlation adjustments
    total_adjustment = 0.0
    for i, j in combinations(range(len(legs)), 2):
        market_a = _normalize_market(legs[i].get("market", ""))
        market_b = _normalize_market(legs[j].get("market", ""))

        if correlations:
            rho = correlations.get((market_a, market_b), 0.0)
            if rho == 0.0:
                rho = correlations.get((market_b, market_a), 0.0)
        else:
            rho = get_correlation(market_a, market_b, sport)

        if rho == 0.0:
            continue

        sigma_a = math.sqrt(probs[i] * (1 - probs[i])) if 0 < probs[i] < 1 else 0.0
        sigma_b = math.sqrt(probs[j] * (1 - probs[j])) if 0 < probs[j] < 1 else 0.0

        # Scale adjustment by the product of all OTHER legs' probabilities
        # so the pairwise adjustment propagates correctly through the full parlay.
        other_product = 1.0
        for k in range(len(probs)):
            if k != i and k != j:
                other_product *= probs[k]

        total_adjustment += rho * sigma_a * sigma_b * other_product

    adjusted_joint = independent_joint + total_adjustment

    # Clamp to valid range
    adjusted_joint = max(1e-9, min(1.0 - 1e-9, adjusted_joint))

    return _implied_to_american(adjusted_joint)


def detect_mispriced_correlation(
    legs: list[dict],
    book_parlay_odds: int,
    sport: str,
) -> dict:
    """
    Detect whether a book is mispricing an SGP by ignoring correlations.

    Compares the book's offered parlay odds (which assume independence or
    apply a crude correlation penalty) against our correlation-adjusted
    fair odds. If the book offers better odds than our adjusted fair value,
    the SGP has positive expected value.

    Args:
        legs: List of dicts with:
            - "american_odds" (int): individual leg odds
            - "market" (str): market type (e.g., "qb_passing_yards", "team_total")
            - "description" (str, optional): human-readable leg description
        book_parlay_odds: The American odds the book is offering for the parlay
        sport: Sport key (e.g., "nfl", "nba")

    Returns:
        Dict with mispricing analysis:
        - true_correlation: weighted average correlation across all leg pairs
        - book_assumed_correlation: implied correlation from book's pricing
        - edge_from_correlation: probability edge from correlation mispricing
        - mispricing_pct: edge as a percentage
        - is_positive_ev: whether the SGP is +EV
        - anti_correlation_warning: flags if legs fight each other
        - leg_pair_correlations: detailed per-pair correlation breakdown
    """
    if not legs or len(legs) < 2:
        return {"error": "Need at least 2 legs for correlation analysis"}

    # Individual marginal probabilities
    marginals = []
    for leg in legs:
        odds = leg.get("american_odds", -110)
        marginals.append(_american_to_implied(odds))

    # Independent joint probability (what a naive book would assume)
    independent_joint = 1.0
    for p in marginals:
        independent_joint *= p

    # Book's implied joint probability from their offered parlay odds
    book_implied_joint = _american_to_implied(book_parlay_odds)

    # Calculate correlation-adjusted joint probability (our fair estimate)
    total_adjustment = 0.0
    pair_details = []
    has_anti_correlation = False

    for (i, j) in combinations(range(len(legs)), 2):
        market_a = legs[i].get("market", "unknown")
        market_b = legs[j].get("market", "unknown")
        rho = get_correlation(market_a, market_b, sport)

        sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
        sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0

        other_product = 1.0
        for k in range(len(marginals)):
            if k != i and k != j:
                other_product *= marginals[k]

        pair_adjustment = rho * sigma_a * sigma_b * other_product

        if rho < -0.05:
            has_anti_correlation = True

        pair_details.append({
            "leg_a": legs[i].get("description", market_a),
            "leg_b": legs[j].get("description", market_b),
            "market_a": market_a,
            "market_b": market_b,
            "correlation": rho,
            "adjustment": round(pair_adjustment, 6),
            "direction": "positive" if rho > 0 else ("negative" if rho < 0 else "independent"),
        })

        total_adjustment += pair_adjustment

    # Our true joint probability with correlations
    true_joint = independent_joint + total_adjustment
    true_joint = max(1e-9, min(1.0 - 1e-9, true_joint))

    # Edge: difference between book's price and our fair price
    # If true_joint > book_implied_joint, the parlay hits more often than
    # the book thinks → the book is underpricing it → +EV for us
    edge = true_joint - book_implied_joint
    mispricing_pct = (edge / book_implied_joint * 100) if book_implied_joint > 0 else 0.0

    # Reverse-engineer what correlation the book is assuming
    # book_implied = independent + book_rho_adj
    # book_rho_adj = book_implied - independent
    book_rho_adj = book_implied_joint - independent_joint
    # Approximate book's assumed weighted-average correlation
    # Using the average sigma product as denominator
    if pair_details:
        avg_sigma_product = 0.0
        for (i, j) in combinations(range(len(marginals)), 2):
            sa = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
            sb = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
            other_prod = 1.0
            for k in range(len(marginals)):
                if k != i and k != j:
                    other_prod *= marginals[k]
            avg_sigma_product += sa * sb * other_prod
        book_assumed_rho = book_rho_adj / avg_sigma_product if avg_sigma_product > 0 else 0.0
    else:
        book_assumed_rho = 0.0

    # Weighted average of our correlations
    weighted_rho_sum = sum(abs(pd["correlation"]) for pd in pair_details)
    num_pairs = len(pair_details)
    avg_rho = weighted_rho_sum / num_pairs if num_pairs > 0 else 0.0

    # Fair odds
    fair_american = _implied_to_american(true_joint)
    independent_american = _implied_to_american(independent_joint)

    # Anti-correlation warnings
    anti_warnings = []
    if has_anti_correlation:
        for pd in pair_details:
            if pd["correlation"] < -0.05:
                anti_warnings.append(
                    f"WARNING: {pd['leg_a']} and {pd['leg_b']} are negatively correlated "
                    f"(rho={pd['correlation']:.2f}). This parlay is HARDER to hit than "
                    f"the independent price suggests."
                )

    return {
        "true_correlation": round(avg_rho, 4),
        "book_assumed_correlation": round(book_assumed_rho, 4),
        "edge_from_correlation": round(edge, 6),
        "mispricing_pct": round(mispricing_pct, 2),
        "is_positive_ev": edge > 0,
        "independent_joint_prob": round(independent_joint, 6),
        "book_implied_joint_prob": round(book_implied_joint, 6),
        "true_joint_prob": round(true_joint, 6),
        "independent_odds": independent_american,
        "book_offered_odds": book_parlay_odds,
        "fair_odds": fair_american,
        "leg_pair_correlations": pair_details,
        "anti_correlation_warning": anti_warnings if anti_warnings else None,
        "assessment": _assess_mispricing(edge, mispricing_pct, has_anti_correlation, avg_rho),
    }


def build_correlated_parlay(
    available_props: list[dict],
    game_data: dict,
    sport: str,
    min_correlation: float = 0.3,
    max_legs: int = 4,
    min_legs: int = 2,
) -> list[dict]:
    """
    Build optimally correlated SGP suggestions from available props and game lines.

    Scans all possible combinations of available props/markets within a game and
    ranks them by the degree of correlation mispricing — i.e., how much edge
    comes from the book treating correlated legs as independent.

    Args:
        available_props: List of available betting options, each a dict with:
            - "market" (str): market type
            - "american_odds" (int): offered odds
            - "description" (str): human-readable description
            - "player" (str, optional): player name
            - "side" (str, optional): "over" or "under"
            - "line" (float, optional): the prop line
        game_data: Game-level data dict with:
            - "home_team" (str)
            - "away_team" (str)
            - "game_total" (float, optional): posted game total
            - "spread" (float, optional): posted spread
        sport: Sport key
        min_correlation: Minimum average pairwise correlation to include a combo.
        max_legs: Maximum legs per parlay suggestion.
        min_legs: Minimum legs per parlay suggestion.

    Returns:
        List of parlay suggestions sorted by correlation edge (highest first).
        Each suggestion includes the legs, correlations, and estimated edge.
    """
    if not available_props:
        return []

    suggestions = []

    # Try all combinations from min_legs to max_legs
    for num_legs in range(min_legs, min(max_legs + 1, len(available_props) + 1)):
        for combo in combinations(range(len(available_props)), num_legs):
            legs = [available_props[idx] for idx in combo]
            markets = [_normalize_market(leg.get("market", "")) for leg in legs]

            # Calculate pairwise correlations
            pair_rhos = []
            total_rho = 0.0
            all_positive = True

            for (i, j) in combinations(range(len(legs)), 2):
                rho = get_correlation(markets[i], markets[j], sport)
                pair_rhos.append({
                    "leg_a": legs[i].get("description", markets[i]),
                    "leg_b": legs[j].get("description", markets[j]),
                    "correlation": rho,
                })
                total_rho += rho
                if rho < 0:
                    all_positive = False

            num_pairs = len(pair_rhos)
            avg_correlation = total_rho / num_pairs if num_pairs > 0 else 0.0

            # Filter: only suggest parlays above the min correlation threshold
            if avg_correlation < min_correlation:
                continue

            # Calculate pricing
            marginals = [_american_to_implied(leg.get("american_odds", -110)) for leg in legs]

            independent_joint = 1.0
            for p in marginals:
                independent_joint *= p

            # Correlation-adjusted probability
            adjustment = 0.0
            for (i, j) in combinations(range(len(legs)), 2):
                rho = get_correlation(markets[i], markets[j], sport)
                sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
                sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
                other_product = 1.0
                for k in range(len(marginals)):
                    if k != i and k != j:
                        other_product *= marginals[k]
                adjustment += rho * sigma_a * sigma_b * other_product

            true_joint = max(1e-9, min(1.0 - 1e-9, independent_joint + adjustment))

            # Edge from correlation = true probability - book's independent price
            correlation_edge = true_joint - independent_joint
            edge_pct = (correlation_edge / independent_joint * 100) if independent_joint > 0 else 0.0

            # Convert to odds
            independent_odds = _implied_to_american(independent_joint)
            fair_odds = _implied_to_american(true_joint)

            home = game_data.get("home_team", "")
            away = game_data.get("away_team", "")

            suggestions.append({
                "game": f"{away} @ {home}" if away and home else "Unknown",
                "num_legs": num_legs,
                "legs": [
                    {
                        "description": leg.get("description", ""),
                        "market": leg.get("market", ""),
                        "american_odds": leg.get("american_odds", 0),
                        "implied_prob": round(_american_to_implied(leg.get("american_odds", -110)), 4),
                        "player": leg.get("player", ""),
                        "side": leg.get("side", ""),
                        "line": leg.get("line"),
                    }
                    for leg in legs
                ],
                "pair_correlations": pair_rhos,
                "avg_correlation": round(avg_correlation, 4),
                "all_positive_correlations": all_positive,
                "independent_joint_prob": round(independent_joint, 6),
                "true_joint_prob": round(true_joint, 6),
                "correlation_edge": round(correlation_edge, 6),
                "correlation_edge_pct": round(edge_pct, 2),
                "independent_parlay_odds": independent_odds,
                "fair_parlay_odds": fair_odds,
                "rating": _rate_correlation_edge(edge_pct, avg_correlation),
            })

    # Sort by correlation edge percentage (highest mispricing first)
    suggestions.sort(key=lambda x: x["correlation_edge_pct"], reverse=True)

    import logging

    logger = logging.getLogger("callisto.correlation")
    logger.info(
        f"Built {len(suggestions)} correlated parlay suggestions for {sport} "
        f"(min_corr={min_correlation}, max_legs={max_legs})"
    )

    return suggestions
