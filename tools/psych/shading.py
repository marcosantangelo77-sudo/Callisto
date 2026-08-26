"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""1. Public number shading detection — where books exploit public clustering."""

from typing import Optional

from tools.odds_api import calculate_implied_probability
from tools.psych.constants import (
    NFL_MARGIN_FREQ,
    NBA_TOTAL_SHADE,
    NFL_SPREAD_SHADE,
    NBA_SPREAD_SHADE,
)

# ---------------------------------------------------------------------------
# 1. Public Number Shading Detection
# ---------------------------------------------------------------------------

def detect_number_shading(
    spread: float,
    sport: str,
    market: str = "spreads",
    book_price: Optional[int] = None,
) -> dict:
    """
    Detect if a line is shaded toward a public-magnet number.

    Books shade lines toward numbers the public gravitates to, because
    public money flows to those numbers regardless of fair value. This
    means the OTHER side of a shaded number often has slight value.

    NFL example: -2.5 has less public appeal than -3. If the true line
    is -2.7, books post -3 at -115 rather than -2.5 at -110, because
    more public money lands on -3. The value is on +3 (or +2.5 if
    available elsewhere).

    Args:
        spread: The current spread or total (absolute value used internally)
        sport: Sport key (e.g., 'americanfootball_nfl')
        market: 'spreads' or 'totals'
        book_price: Optional American odds price — if provided, we can
                    estimate juice premium for sitting on a key number

    Returns:
        Dict with shading analysis.
    """
    abs_spread = abs(spread)

    # Select the appropriate shade map
    if market == "totals" and "nba" in sport:
        shade_map = NBA_TOTAL_SHADE
    elif market == "spreads" and "nfl" in sport:
        shade_map = NFL_SPREAD_SHADE
    elif market == "spreads" and "nba" in sport:
        shade_map = NBA_SPREAD_SHADE
    elif market == "spreads" and "ncaaf" in sport:
        # College football uses same key numbers as NFL
        shade_map = NFL_SPREAD_SHADE
    else:
        # Generic: check if on a round number
        shade_map = {}

    # Check if the line sits on a shaded number
    shade_cents = shade_map.get(abs_spread, 0)
    is_shaded = shade_cents > 0

    # Check adjacent numbers for context
    half_up = abs_spread + 0.5
    half_down = abs_spread - 0.5
    shade_up = shade_map.get(half_up, 0)
    shade_down = shade_map.get(half_down, 0)

    # Determine which direction the line is shaded toward
    if is_shaded:
        # The line IS the public magnet. Public money clusters here.
        shaded_toward = "this_number"
        # True line estimate: shade pushes the posted number away from true value.
        # If -3 is shaded and public bets the favorite, true line is closer to -2.7
        # (books post -3 because it attracts more action).
        shade_direction = 1 if spread < 0 else -1
        true_line_estimate = spread + shade_direction * (shade_cents / 20.0)
        # Each cent of shade corresponds to roughly 0.05 points of line distortion
        value_side = "opposite"  # Value is on the other side of the shaded number
    elif shade_down > 0 and shade_down > shade_up:
        # The line is half a point above a shaded number (e.g., 3.5 when 3 is shaded)
        shaded_toward = half_down
        true_line_estimate = spread  # Line is likely close to fair here
        shade_cents = int(shade_down * 0.3)  # Residual shade from nearby magnet
        value_side = "this_side"  # Off the key number = less public money = value
    elif shade_up > 0:
        # Half a point below a shaded number (e.g., 2.5 when 3 is shaded)
        shaded_toward = half_up
        true_line_estimate = spread
        shade_cents = int(shade_up * 0.3)
        value_side = "this_side"
    else:
        shaded_toward = None
        true_line_estimate = spread
        value_side = "neutral"

    # If we have the book price, calculate the juice premium for this number
    juice_premium_cents = 0
    if book_price is not None and is_shaded:
        implied = calculate_implied_probability(book_price)
        standard_implied = calculate_implied_probability(-110)
        if implied > standard_implied:
            juice_premium_cents = int((implied - standard_implied) * 2000)
            # 2000 converts implied-prob difference to approximate American cents

    # NFL-specific: quantify the margin frequency impact
    margin_frequency_note = ""
    if "nfl" in sport and market == "spreads":
        freq = NFL_MARGIN_FREQ.get(int(abs_spread), 0)
        if freq > 0:
            margin_frequency_note = (
                f"Games land on margin {int(abs_spread)} approximately "
                f"{freq:.1%} of the time. "
            )
            if abs_spread == 3:
                margin_frequency_note += (
                    "This is the single most common NFL margin. "
                    "The difference between -2.5 and -3 is worth ~3% in cover probability."
                )
            elif abs_spread == 7:
                margin_frequency_note += (
                    "Second most common NFL margin (TD without extra point drama). "
                    "The -6.5 to -7 jump is worth ~2% in cover probability."
                )

    return {
        "spread": spread,
        "sport": sport,
        "market": market,
        "is_shaded": is_shaded,
        "shaded_toward": shaded_toward,
        "shade_magnitude_cents": shade_cents,
        "true_line_estimate": round(true_line_estimate, 2),
        "value_side": value_side,
        "juice_premium_cents": juice_premium_cents,
        "margin_frequency_note": margin_frequency_note,
        "explanation": _shading_explanation(spread, is_shaded, shaded_toward, value_side, sport, market),
    }


def _shading_explanation(
    spread: float,
    is_shaded: bool,
    shaded_toward,
    value_side: str,
    sport: str,
    market: str,
) -> str:
    """Build a human-readable explanation of the shading analysis."""
    if not is_shaded and shaded_toward is None:
        return (
            f"Line {spread} is not near a public-magnet number for {sport} {market}. "
            f"No significant shading detected."
        )

    if is_shaded and shaded_toward == "this_number":
        return (
            f"Line {spread} sits on a key public number. Books shade toward this "
            f"number because public money clusters here. The OTHER side likely has "
            f"slight value — public overrepresentation on one side means the book "
            f"can offer slightly worse odds and still attract action."
        )

    if value_side == "this_side":
        return (
            f"Line {spread} is half a point off the key number {shaded_toward}. "
            f"This is often a value spot — less public money lands here, so the "
            f"book doesn't need to shade as aggressively. If available, this line "
            f"may offer better value than the adjacent key number."
        )

    return f"Line {spread} — moderate shading analysis for {sport} {market}."


