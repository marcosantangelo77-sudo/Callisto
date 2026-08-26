"""
Unit sizing from bankroll with confidence-weighted scaling
(split from tools/kelly.py).
"""

from typing import Optional

from tools.kellypkg.constants import AGP_TIER_MULTIPLIERS
from tools.kellypkg.odds import _confidence_tier_from_score


def calculate_units(
    bankroll: float,
    edge: float,
    confidence: float,
    kelly_fraction: float = 0.25,
    unit_size: Optional[float] = None,
) -> dict:
    """
    Convert Kelly output into practical unit sizing.

    Most bettors think in "units" (1 unit = 1% of bankroll by convention).
    This function bridges the gap between Kelly math and the unit system.

    If unit_size is not provided, 1 unit = 1% of bankroll (standard).

    Args:
        bankroll:       Current bankroll in dollars.
        edge:           Edge as a decimal (e.g., 0.03 for 3%).
        confidence:     AGP confidence score (0.0 - 1.0).
        kelly_fraction: Fractional Kelly factor (default 0.25).
        unit_size:      Dollar value of 1 unit (default: bankroll * 0.01).

    Returns:
        Dict with units, dollar_amount, pct_of_bankroll, and breakdown.
    """
    if unit_size is None:
        unit_size = bankroll * 0.01

    if unit_size <= 0 or bankroll <= 0:
        return {
            "units": 0.0,
            "dollar_amount": 0.0,
            "pct_of_bankroll": 0.0,
            "unit_size": unit_size,
            "error": "Invalid bankroll or unit size",
        }

    # Use the Kelly fraction from dynamic Kelly (without variance — that requires
    # separate variance_estimate).  Here we apply confidence directly.
    # For a quick sizing call, use the tier multiplier on fractional Kelly.
    tier = _confidence_tier_from_score(confidence)
    tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)

    # Compute base Kelly (needs odds — estimate from edge)
    # To avoid requiring odds as a separate param, we back-calculate
    # approximate odds from the edge magnitude.  For a more precise result,
    # call kelly_dynamic() directly with actual odds.
    #
    # However, edge alone is ambiguous without odds.  We use a heuristic:
    # assume standard -110 odds (most common) unless edge is large enough
    # to suggest plus-money.
    #
    # For unit sizing, the practical formula is:
    #   fraction = edge * kelly_fraction * tier_mult
    # This is a linearized approximation of Kelly that works well for
    # small edges (which is what sharps typically bet on).
    fraction = edge * kelly_fraction * tier_mult

    # Safety: cap at 5% of bankroll
    fraction = max(0.0, min(fraction, 0.05))

    dollar_amount = round(bankroll * fraction, 2)
    units = round(dollar_amount / unit_size, 2) if unit_size > 0 else 0.0
    pct = round(fraction * 100, 3)

    # Unit rating for readability
    if units >= 3.0:
        unit_label = "MAX"
    elif units >= 2.0:
        unit_label = "STRONG"
    elif units >= 1.0:
        unit_label = "STANDARD"
    elif units >= 0.5:
        unit_label = "HALF"
    elif units > 0:
        unit_label = "LEAN"
    else:
        unit_label = "NO_BET"

    return {
        "units": units,
        "unit_label": unit_label,
        "dollar_amount": dollar_amount,
        "pct_of_bankroll": pct,
        "unit_size": round(unit_size, 2),
        "bankroll": bankroll,
        "breakdown": {
            "edge": round(edge, 5),
            "confidence": round(confidence, 3),
            "tier": tier,
            "tier_multiplier": round(tier_mult, 3),
            "kelly_fraction": kelly_fraction,
            "raw_fraction": round(edge * kelly_fraction * tier_mult, 7),
            "capped_fraction": round(fraction, 7),
        },
    }
