"""
Dynamic Kelly with confidence bands (split from tools/kelly.py).
"""

from tools.kellypkg.constants import AGP_TIER_MULTIPLIERS
from tools.kellypkg.core import kelly_full, kelly_fractional
from tools.kellypkg.odds import _confidence_tier_from_score


def kelly_dynamic(
    edge: float,
    odds,
    confidence_score: float,
    variance_estimate: float,
    bankroll: float,
    kelly_base_fraction: float = 0.25,
) -> dict:
    """
    Dynamic Kelly that factors in AGP confidence tier and edge variance.

    A 3% edge with tight variance and VERIFIED confidence gets more units
    than a 5% edge with wide variance and SPECULATIVE data.

    The formula:
        stake = bankroll * kelly_fractional * tier_multiplier * variance_dampener

    Variance dampener:
        dampener = 1 / (1 + k * variance_estimate)
        where k is a sensitivity constant.  High variance -> smaller bets.

    Args:
        edge:               Edge as decimal.
        odds:               American odds.
        confidence_score:   AGP confidence score (0.0 - 1.0).
        variance_estimate:  Standard deviation of the edge estimate (in probability
                            units, e.g., 0.03 means +/-3% uncertainty on the edge).
        bankroll:           Current bankroll in dollars.
        kelly_base_fraction: Base Kelly fraction before adjustments (default 0.25).

    Returns:
        Dict with stake, fraction, reasoning, and component breakdown.
    """
    # Step 1: Base fractional Kelly
    base_fraction = kelly_fractional(edge, odds, fraction=kelly_base_fraction)

    # Step 2: AGP tier multiplier
    tier = _confidence_tier_from_score(confidence_score)
    tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)

    # Smooth scaling within tier: interpolate between this tier's mult and
    # the next-higher tier's mult based on where the score falls.
    # This avoids cliff effects at tier boundaries.
    if tier == "VERIFIED":
        smooth_mult = tier_mult
    elif tier == "CORROBORATED":
        # 0.75-0.89 -> lerp between 0.80 and 1.00
        t = (confidence_score - 0.75) / 0.15
        smooth_mult = 0.80 + t * 0.20
    elif tier == "PROBABLE":
        # 0.55-0.74 -> lerp between 0.55 and 0.80
        t = (confidence_score - 0.55) / 0.20
        smooth_mult = 0.55 + t * 0.25
    elif tier == "SPECULATIVE":
        # 0.30-0.54 -> lerp between 0.30 and 0.55
        t = (confidence_score - 0.30) / 0.25
        smooth_mult = 0.30 + t * 0.25
    else:
        smooth_mult = 0.0

    smooth_mult = max(0.0, min(1.0, smooth_mult))

    # Step 3: Variance dampener
    # Higher variance -> smaller bet.  The sensitivity constant k controls
    # how aggressively we penalize uncertainty.
    # At variance_estimate = 0 (perfect info), dampener = 1.0.
    # At variance_estimate = edge (uncertainty equals the edge), dampener ~ 0.5.
    k = 1.0 / max(abs(edge), 0.001)  # normalize so dampener halves when var == edge
    variance_dampener = 1.0 / (1.0 + k * variance_estimate)
    variance_dampener = max(0.05, min(1.0, variance_dampener))

    # Step 4: Combine
    adjusted_fraction = base_fraction * smooth_mult * variance_dampener

    # Step 5: Safety caps
    # Never risk more than 5% of bankroll on a single bet regardless of Kelly
    hard_cap = 0.05
    final_fraction = min(adjusted_fraction, hard_cap)

    # Step 6: Dollar amount
    stake = round(bankroll * final_fraction, 2)

    # Build reasoning
    reasons = []
    reasons.append(f"Base quarter-Kelly: {base_fraction:.4f} ({base_fraction*100:.2f}% of bankroll)")
    reasons.append(f"AGP tier: {tier} (score={confidence_score:.2f}, multiplier={smooth_mult:.3f})")
    reasons.append(f"Variance dampener: {variance_dampener:.3f} (edge_uncertainty={variance_estimate:.4f})")
    if adjusted_fraction > hard_cap:
        reasons.append(f"Hard-capped from {adjusted_fraction*100:.2f}% to {hard_cap*100:.1f}%")
    reasons.append(f"Final: {final_fraction*100:.3f}% of ${bankroll:,.0f} = ${stake:,.2f}")

    return {
        "stake": stake,
        "fraction": round(final_fraction, 6),
        "kelly_full": round(kelly_full(edge, odds), 6),
        "kelly_base": round(base_fraction, 6),
        "tier": tier,
        "tier_multiplier": round(smooth_mult, 4),
        "variance_dampener": round(variance_dampener, 4),
        "hard_cap_applied": adjusted_fraction > hard_cap,
        "reasoning": " | ".join(reasons),
        "components": {
            "edge": round(edge, 5),
            "odds": odds,
            "confidence_score": round(confidence_score, 3),
            "variance_estimate": round(variance_estimate, 5),
            "bankroll": bankroll,
        },
    }
