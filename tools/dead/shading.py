"""
Public shading detection: identify lines shaded toward key numbers.
"""

from .data import _normalize_sport
from .valuation import push_probability


# ---------------------------------------------------------------------------
# Public shading detection
# ---------------------------------------------------------------------------

# Typical shading behavior: books move lines toward key numbers to attract
# public money. The public loves betting on -3 and -7 in the NFL because
# those "feel right." Books exploit this by shading a -2.5 true line to
# -3, knowing the public will pile on at -3.
#
# The shade is usually 0.5 to 1.5 points and creates value on the OTHER side.

# Shading gravity: how much each key number "pulls" lines toward it.
# Higher gravity = books are more likely to shade toward this number.
NFL_SHADE_GRAVITY: dict[float, float] = {
    3.0: 1.0,    # Maximum gravity — books love posting -3
    7.0: 0.85,   # Second strongest pull
    6.0: 0.30,   # Moderate pull
    6.5: 0.25,
    10.0: 0.40,
    14.0: 0.30,
    17.0: 0.20,
    21.0: 0.15,
}

NBA_SHADE_GRAVITY: dict[float, float] = {
    # NBA shading is less pronounced because key numbers are less dramatic.
    # Books still shade toward round numbers for totals.
    1.0: 0.15,   2.0: 0.12,   3.0: 0.12,   5.0: 0.15,   7.0: 0.10,
    10.0: 0.12,
}

SHADE_GRAVITY: dict[str, dict[float, float]] = {
    "NFL": NFL_SHADE_GRAVITY,
    "NBA": NBA_SHADE_GRAVITY,
    "NCAAF": NFL_SHADE_GRAVITY,     # Same shading patterns as NFL
    "NCAAB": NBA_SHADE_GRAVITY,     # Same shading patterns as NBA
    "MLB": {},                       # Run lines don't really get shaded
    "NHL": {},                       # Puck lines don't really get shaded
}


def detect_public_shading(
    line: float,
    sport: str,
    market_type: str = "spread",
) -> dict:
    """
    Detect whether a line is likely being shaded toward a key number
    to attract public money.

    Public shading works like this:
    1. Book's true model says the line should be -2.5
    2. They know the public loves betting -3 (it "feels" like a field goal game)
    3. They post -3 to attract public money on the favorite
    4. This creates value on the underdog at +3 (the true line is +2.5)

    Signs of shading:
    - Line is ON a key number (3, 7, 10, 14 in NFL)
    - Key number is "close" to where the true line likely is (within 1 point)
    - Line opened at a non-key number and moved TO the key number without
      apparent sharp action

    Args:
        line: The current spread (e.g., -3.0).
        sport: Sport identifier.
        market_type: "spread" or "total" — different shading patterns.

    Returns:
        Dict with shading analysis.
    """
    normalized = _normalize_sport(sport)
    abs_line = abs(line)
    gravity_table = SHADE_GRAVITY.get(normalized, {})

    # Check if the line is on or very near a key number with high gravity
    nearest_key = None
    nearest_dist = float("inf")
    nearest_gravity = 0.0

    for key_num, gravity in gravity_table.items():
        dist = abs(abs_line - key_num)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest_key = key_num
            nearest_gravity = gravity

    # Is the line sitting on a gravitational key number?
    on_key = nearest_dist < 0.01
    near_key = nearest_dist <= 1.0

    if not near_key or nearest_key is None:
        return {
            "line": line,
            "sport": normalized,
            "shaded_toward": None,
            "estimated_shade_cents": 0,
            "true_line_estimate": line,
            "confidence": "LOW",
            "detail": "Line is not near a key number with public gravity.",
        }

    # Estimate shade magnitude based on gravity and proximity
    # On the key number: maximum shade estimate
    # Near the key number: proportional shade estimate
    if on_key:
        # Line is ON the key number — most likely shaded
        # Estimate the true line is 0.5-1.0 points away from the key
        # Direction: shade is TOWARD the key from the true line
        shade_estimate_pts = nearest_gravity * 0.5  # Max 0.5 points shade at gravity 1.0
        shade_direction = "toward_zero" if line < 0 else "away_from_zero"

        # True line is slightly less extreme (closer to pick'em)
        if line < 0:
            true_line_estimate = line + shade_estimate_pts
        else:
            true_line_estimate = line - shade_estimate_pts

        # Convert shade to cents
        # At -110 juice, half a point on a key number is worth roughly
        # push_prob/2 * 191 cents (the cents_per_pct factor)
        push_prob = push_probability(abs_line, sport)
        estimated_shade_cents = round(push_prob / 2 * 191 * nearest_gravity, 1)

        confidence = "HIGH" if nearest_gravity >= 0.7 else "MODERATE"
        detail = (
            f"Line is ON key number {nearest_key} (gravity {nearest_gravity:.2f}). "
            f"Public loves this number. Estimated true line: {true_line_estimate:+.1f}. "
            f"The OTHER side likely has {estimated_shade_cents:.1f} cents of value "
            f"from public shading."
        )
    else:
        # Line is near but not on a key number
        # This could mean: the true line IS here, and the book resisted shading
        # (less common), or the line is in transition
        shade_estimate_pts = 0.0
        true_line_estimate = line
        estimated_shade_cents = 0
        confidence = "LOW"
        detail = (
            f"Line {line:+.1f} is near key number {nearest_key} (distance: "
            f"{nearest_dist:.1f}) but not on it. This may indicate the book's "
            f"true model places the line here and they resisted shading, or "
            f"the line is moving toward {nearest_key}."
        )

    return {
        "line": line,
        "sport": normalized,
        "shaded_toward": nearest_key if on_key else None,
        "nearest_key_number": nearest_key,
        "distance_from_key": round(nearest_dist, 2),
        "key_gravity": nearest_gravity,
        "estimated_shade_cents": estimated_shade_cents,
        "true_line_estimate": round(true_line_estimate, 1),
        "shade_direction": shade_direction if on_key else None,
        "confidence": confidence,
        "detail": detail,
        "actionable": on_key and nearest_gravity >= 0.5,
        "contrarian_side": (
            f"Bet {'underdog' if line < 0 else 'favorite'} "
            f"({'+'if line < 0 else '-'}{abs_line}) — "
            f"public is on the other side"
        ) if on_key and nearest_gravity >= 0.5 else None,
    }
