"""Reverse line movement detection — the strongest sharp money indicator."""

import numpy as np


def detect_rlm(
    line_movement_direction: float,
    public_ticket_pct: float,
    public_money_pct: float,
) -> dict:
    """
    Detect reverse line movement — the strongest sharp money indicator.

    RLM occurs when the line moves AGAINST where the majority of bets (tickets)
    are placed. This means fewer but LARGER (sharper) bets on the other side
    are moving the line despite being outnumbered in ticket count.

    The money percentage vs ticket percentage divergence is the key signal.
    If 70% of tickets are on Team A but only 45% of money is on Team A,
    sharp money is clearly on Team B.

    Args:
        line_movement_direction: Positive = line moved toward side A being
            more expensive (i.e., A got shorter / more favored).
            Negative = line moved away from side A.
        public_ticket_pct: Percentage of tickets on side A (0-100).
        public_money_pct: Percentage of total money on side A (0-100).

    Returns:
        Dict with is_rlm flag, confidence score, and interpretation.
    """
    # Normalize inputs
    ticket_pct = float(np.clip(public_ticket_pct, 0, 100))
    money_pct = float(np.clip(public_money_pct, 0, 100))

    # Ticket/money divergence: positive means more tickets than money on side A
    # → sharp money is on the OTHER side (side B)
    ticket_money_divergence = ticket_pct - money_pct

    # Determine if RLM exists
    # RLM case 1: majority of tickets on A, but line moves to make B cheaper
    #   → ticket_pct > 50, line_movement_direction < 0 (moved away from A)
    # RLM case 2: majority of tickets on B, but line moves to make A cheaper
    #   → ticket_pct < 50, line_movement_direction > 0 (moved toward A)
    public_side_is_a = ticket_pct > 50
    line_favors_a = line_movement_direction > 0

    # RLM = public on one side, line moves the other way
    is_rlm = (public_side_is_a and not line_favors_a) or (not public_side_is_a and line_favors_a)

    # Confidence scoring (0-1 scale)
    # Factors: ticket imbalance, ticket/money divergence, movement magnitude
    ticket_imbalance = abs(ticket_pct - 50) / 50.0  # 0 at 50%, 1 at 0/100%
    divergence_score = abs(ticket_money_divergence) / 40.0  # Normalize to ~0-1 range
    movement_magnitude = min(abs(line_movement_direction) / 3.0, 1.0)  # 3+ pts = max

    if is_rlm:
        # Weight the components — ticket/money divergence is the strongest signal
        confidence = float(np.clip(
            0.30 * ticket_imbalance + 0.45 * divergence_score + 0.25 * movement_magnitude,
            0.0, 1.0,
        ))
    else:
        confidence = 0.0

    # Determine the sharp side
    if is_rlm:
        sharp_side = "B (opposite of public)" if public_side_is_a else "A (opposite of public)"
    else:
        sharp_side = "aligned with public (no RLM)"

    # Build detailed interpretation
    if is_rlm and confidence > 0.6:
        strength = "STRONG"
        action = "High-confidence sharp signal — strongly consider the contrarian side"
    elif is_rlm and confidence > 0.35:
        strength = "MODERATE"
        action = "Meaningful RLM detected — worth including in analysis"
    elif is_rlm:
        strength = "WEAK"
        action = "Marginal RLM — may be noise, seek confirming signals"
    else:
        strength = "NONE"
        action = "No reverse line movement — line moving with public consensus"

    return {
        "is_rlm": is_rlm,
        "confidence": round(confidence, 4),
        "strength": strength,
        "sharp_side": sharp_side,
        "ticket_pct_side_a": round(ticket_pct, 1),
        "money_pct_side_a": round(money_pct, 1),
        "ticket_money_divergence": round(ticket_money_divergence, 1),
        "line_movement": round(line_movement_direction, 2),
        "interpretation": (
            f"Public: {ticket_pct:.0f}% tickets / {money_pct:.0f}% money on side A. "
            f"Line moved {'toward' if line_favors_a else 'away from'} A by "
            f"{abs(line_movement_direction):.1f} pts. "
            f"{'RLM DETECTED (' + strength + '): ' if is_rlm else 'No RLM: '}"
            f"{action}."
        ),
    }
