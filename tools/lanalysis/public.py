"""Public side estimation and contrarian value."""

import numpy as np

from tools.lanalysis.constants import (
    CONTRARIAN_ROI_TABLE,
    NFL_KEY_NUMBERS,
    TEAM_BRAND_TIERS,
    _DEFAULT_ROI_TABLE,
)


def estimate_public_side(
    line_open: float,
    line_current: float,
    sport: str = "americanfootball_nfl",
    is_primetime: bool = False,
    is_rivalry: bool = False,
    team_a: str = "",
    team_b: str = "",
    team_a_recent_wins: int = 0,
    team_b_recent_wins: int = 0,
) -> dict:
    """
    Estimate which side the public is on without actual ticket data.

    Books don't publish real ticket/money percentages (except in limited
    contexts). But we can infer public lean from observable signals:

    1. Line movement: if the line moves toward the favorite, public money
       is likely on the favorite (books shade to balance exposure).
    2. Team brand value: big-name teams attract public money.
    3. Primetime/national TV: these games get disproportionate public handle.
    4. Recency bias: teams on 3+ game win streaks attract "hot team" public bets.
    5. Rivalry games: public gravitates to the historically dominant program.

    Args:
        line_open: Opening line (spread). Negative = team A favored.
        line_current: Current line.
        sport: Sport key.
        is_primetime: True if nationally televised / primetime slot.
        is_rivalry: True if known rivalry matchup.
        team_a: Name of team A (favorite side at open).
        team_b: Name of team B.
        team_a_recent_wins: Team A wins in last 3 games (0-3).
        team_b_recent_wins: Team B wins in last 3 games (0-3).

    Returns:
        Dict with estimated public percentage on each side and fade value.
    """
    # Base: start at 50/50 and adjust
    public_lean_a = 50.0  # Percentage estimated on team A

    # --- Factor 1: Line movement direction ---
    # If line moved to make A more favored (more negative), public is likely on A
    line_move = line_current - line_open
    # For spreads: line_move < 0 means A got more points (more favored)
    if line_move < -1.0:
        public_lean_a += 8.0  # Strong move toward A
    elif line_move < -0.5:
        public_lean_a += 4.0
    elif line_move > 1.0:
        public_lean_a -= 8.0  # Line moved away from A
    elif line_move > 0.5:
        public_lean_a -= 4.0

    # --- Factor 2: Favorite bias ---
    # Public likes favorites, especially big favorites
    if line_open < -7:
        public_lean_a += 10.0  # Big favorite attracts heavy public action
    elif line_open < -3:
        public_lean_a += 6.0
    elif line_open < 0:
        public_lean_a += 3.0
    elif line_open > 7:
        public_lean_a -= 10.0  # A is a big underdog
    elif line_open > 3:
        public_lean_a -= 6.0
    elif line_open > 0:
        public_lean_a -= 3.0

    # --- Factor 3: Team brand value ---
    brand_a = TEAM_BRAND_TIERS.get(team_a, 1)
    brand_b = TEAM_BRAND_TIERS.get(team_b, 1)
    brand_diff = brand_a - brand_b  # Positive = A is bigger brand
    public_lean_a += brand_diff * 5.0  # Each tier difference = ~5%

    # --- Factor 4: Primetime/national TV multiplier ---
    if is_primetime:
        # Primetime amplifies all public biases by ~30%
        excess = public_lean_a - 50.0
        public_lean_a = 50.0 + excess * 1.3

    # --- Factor 5: Rivalry boost ---
    if is_rivalry:
        # Rivalries attract casual bets — amplify brand and favorite bias
        excess = public_lean_a - 50.0
        public_lean_a = 50.0 + excess * 1.15

    # --- Factor 6: Recency bias (hot team effect) ---
    recency_a = min(team_a_recent_wins, 3)
    recency_b = min(team_b_recent_wins, 3)
    if recency_a == 3:
        public_lean_a += 6.0  # 3-game win streak = "hot" team
    elif recency_a >= 2:
        public_lean_a += 3.0
    if recency_b == 3:
        public_lean_a -= 6.0
    elif recency_b >= 2:
        public_lean_a -= 3.0

    # Clamp to reasonable range
    public_lean_a = float(np.clip(public_lean_a, 15.0, 85.0))
    public_lean_b = 100.0 - public_lean_a

    # Fade value: how much contrarian edge exists from fading the public side
    fade_side = "B" if public_lean_a > 55 else "A" if public_lean_b > 55 else "neither"
    public_pct_on_popular = max(public_lean_a, public_lean_b)

    # Look up historical contrarian ROI for this sport and public pct
    roi_table = CONTRARIAN_ROI_TABLE.get(sport, _DEFAULT_ROI_TABLE)
    fade_roi = 0.0
    for (lo, hi), roi in roi_table.items():
        if lo <= public_pct_on_popular < hi:
            fade_roi = roi
            break

    return {
        "estimated_public_pct_a": round(public_lean_a, 1),
        "estimated_public_pct_b": round(public_lean_b, 1),
        "public_favorite": "A" if public_lean_a > 55 else "B" if public_lean_b > 55 else "split",
        "fade_side": fade_side,
        "fade_value": round(fade_roi, 2),
        "confidence": _public_estimation_confidence(public_lean_a, is_primetime, sport),
        "factors": {
            "line_movement_impact": round(line_move, 2),
            "favorite_bias": round(line_open, 1),
            "brand_a_tier": brand_a,
            "brand_b_tier": brand_b,
            "primetime": is_primetime,
            "rivalry": is_rivalry,
            "recency_a": recency_a,
            "recency_b": recency_b,
        },
        "interpretation": (
            f"Estimated public split: {public_lean_a:.0f}% on {team_a or 'A'} / "
            f"{public_lean_b:.0f}% on {team_b or 'B'}. "
            f"{'Fade ' + (team_a or 'A') + ' (contrarian on ' + (team_b or 'B') + ')' if fade_side == 'B' else 'Fade ' + (team_b or 'B') + ' (contrarian on ' + (team_a or 'A') + ')' if fade_side == 'A' else 'No strong fade signal'}. "
            f"Historical contrarian ROI at this public %: {fade_roi:+.1f}%."
        ),
    }


def _public_estimation_confidence(public_lean_a: float, is_primetime: bool, sport: str) -> str:
    """Rate confidence in the public estimate."""
    # More extreme estimates are higher confidence (strong signals)
    extremity = abs(public_lean_a - 50)
    if extremity > 20 and is_primetime:
        return "high"
    elif extremity > 15:
        return "medium-high"
    elif extremity > 8:
        return "medium"
    else:
        return "low"


def contrarian_value(
    estimated_public_pct: float,
    sport: str = "americanfootball_nfl",
    spread: float = 0.0,
) -> dict:
    """
    Calculate the expected contrarian edge from fading the public.

    Historical data shows that when 75%+ of the public is on one side in
    NFL/NCAAF, the other side has been marginally +EV. This effect is:
    - Stronger at non-key numbers (not 3, 7 in NFL)
    - Stronger in larger spreads
    - Strongest in NCAAF (least efficient market)
    - Weaker in NBA (more efficient, lower scoring variance)

    This is NOT a primary signal — it's a tiebreaker and overlay. Use it to
    add confidence to positions already supported by sharp indicators.

    Args:
        estimated_public_pct: Estimated percentage of public on the popular side (50-100).
        sport: Sport key.
        spread: The point spread (absolute value used for key number check).

    Returns:
        Dict with contrarian edge, historical ROI, and confidence.
    """
    pct = float(np.clip(estimated_public_pct, 50, 100))
    abs_spread = abs(spread)

    # Look up base ROI
    roi_table = CONTRARIAN_ROI_TABLE.get(sport, _DEFAULT_ROI_TABLE)
    base_roi = 0.0
    for (lo, hi), roi in roi_table.items():
        if lo <= pct < hi:
            base_roi = roi
            break

    # Key number adjustment (NFL/NCAAF only)
    is_football = "football" in sport
    on_key_number = False
    key_number_adjustment = 0.0
    if is_football:
        # Check if spread is on a key number
        on_key_number = abs_spread in NFL_KEY_NUMBERS or (abs_spread % 1 == 0 and int(abs_spread) in NFL_KEY_NUMBERS)
        if on_key_number:
            # Key numbers are more efficiently priced — less contrarian value
            key_number_adjustment = -0.8
        else:
            # Off key numbers: contrarian value is amplified
            key_number_adjustment = 0.5

    adjusted_roi = base_roi + key_number_adjustment

    # Large spread adjustment: big favorites attract more uninformed public action
    if abs_spread > 10:
        adjusted_roi += 0.5
    elif abs_spread > 7:
        adjusted_roi += 0.3

    # Confidence based on public percentage and sample strength
    if pct >= 75:
        confidence = "high" if adjusted_roi > 2.0 else "medium"
    elif pct >= 65:
        confidence = "medium"
    else:
        confidence = "low"

    # Calculate implied edge as a probability bump
    # 2% ROI at standard -110 vig implies roughly a 1% probability edge
    contrarian_edge_pct = adjusted_roi / 2.0  # Rough conversion: ROI/2 ≈ prob edge

    return {
        "estimated_public_pct": round(pct, 1),
        "sport": sport,
        "spread": spread,
        "base_historical_roi": round(base_roi, 2),
        "key_number_adjustment": round(key_number_adjustment, 2),
        "on_key_number": on_key_number,
        "adjusted_roi": round(adjusted_roi, 2),
        "contrarian_edge": round(contrarian_edge_pct, 2),
        "confidence": confidence,
        "historical_roi": round(adjusted_roi, 2),
        "interpretation": (
            f"With {pct:.0f}% public on the popular side in {sport}, "
            f"historical contrarian ROI is {adjusted_roi:+.1f}% "
            f"({'on' if on_key_number else 'off'} key number{', reduced edge' if on_key_number else ', amplified edge'}). "
            f"Confidence: {confidence}. "
            f"{'Use as confirming signal alongside sharp indicators.' if confidence != 'low' else 'Weak signal alone — need sharps to confirm.'}"
        ),
    }
