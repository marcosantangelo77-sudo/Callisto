"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""2. Trap line detection — lines that don't move despite one-sided action."""

from typing import Optional

# ---------------------------------------------------------------------------
# 2. Trap Line Detection
# ---------------------------------------------------------------------------

def detect_trap_line(
    opening_line: float,
    current_line: float,
    sharp_money_direction: Optional[str] = None,
    public_pct: Optional[float] = None,
    hours_since_open: float = 24.0,
) -> dict:
    """
    Detect if a line is a "trap" — set to attract public money to one side
    while the book (and sharps) are comfortable on the other.

    The key signal: a line that HASN'T MOVED despite heavy one-sided public
    action. Books move lines to manage risk. If 75% of tickets are on one
    side and the line stays put, the book is HAPPY taking that liability.
    That means sharp money (and the book's own models) disagree with the public.

    A second signal: the line moved OPPOSITE to public money. If the public
    hammers the favorite and the line gets cheaper (moves toward the underdog),
    the book is openly baiting more public money onto the losing side.

    Args:
        opening_line: The opening spread/total
        current_line: The current spread/total
        sharp_money_direction: 'favorite', 'underdog', 'over', 'under', or None
        public_pct: Percentage of public tickets on the popular side (0-100)
        hours_since_open: Hours since the line opened

    Returns:
        Dict with trap analysis.
    """
    line_movement = current_line - opening_line
    abs_movement = abs(line_movement)

    # Determine expected movement based on public action
    # If public is heavily one-sided, we'd expect the line to move that direction
    expected_movement_per_hour = 0.02  # baseline drift

    if public_pct is not None:
        public_imbalance = abs(public_pct - 50.0) / 50.0  # 0 to 1 scale
        # Heavy public action (>70%) should move a line ~0.5 to 1.5 points
        expected_total_movement = public_imbalance * 1.5
    else:
        public_imbalance = 0.0
        expected_total_movement = 0.0

    # Trap signal 1: No movement despite public imbalance
    no_move_trap = False
    if public_pct is not None and public_pct > 65 and abs_movement < 0.5:
        no_move_trap = True

    # Trap signal 2: Reverse movement (line moved opposite to public money)
    reverse_trap = False
    if public_pct is not None and public_pct > 55:
        # Public is on the "popular" side. If line moved to make that side
        # MORE attractive (cheaper), the book is baiting.
        # For spreads: public on favorite means public_pct > 55 on negative side.
        # If line moved more negative (bigger spread), that's the expected direction.
        # If it moved LESS negative (smaller spread), that's reverse = trap.
        if line_movement > 0 and public_pct > 60:
            reverse_trap = True  # Line moved toward underdog despite public on favorite

    # Trap signal 3: Sharp money agrees with book (opposite public)
    sharp_confirms = False
    if sharp_money_direction is not None and public_pct is not None:
        # If public is on favorite (pct > 55) but sharp money is on underdog
        if public_pct > 55 and sharp_money_direction in ("underdog", "under"):
            sharp_confirms = True
        elif public_pct < 45 and sharp_money_direction in ("favorite", "over"):
            sharp_confirms = True

    # Calculate composite trap confidence
    trap_signals = []
    confidence = 0.0

    if no_move_trap:
        trap_signals.append("NO_MOVEMENT")
        confidence += 0.35
        # Scale by how extreme the public imbalance is
        confidence += min(0.15, (public_pct - 65) / 100.0) if public_pct else 0

    if reverse_trap:
        trap_signals.append("REVERSE_MOVEMENT")
        confidence += 0.30

    if sharp_confirms:
        trap_signals.append("SHARP_CONFIRMS")
        confidence += 0.25

    # Time factor: trap lines are set early and held steady
    if hours_since_open > 48 and abs_movement < 0.5:
        trap_signals.append("STALE_LINE")
        confidence += 0.10

    is_trap = confidence >= 0.30
    confidence = min(1.0, confidence)

    # Build explanation
    explanations = []
    if no_move_trap:
        explanations.append(
            f"Line hasn't moved ({opening_line} -> {current_line}) despite "
            f"{public_pct:.0f}% of tickets on one side. The book is comfortable "
            f"with this liability."
        )
    if reverse_trap:
        explanations.append(
            f"Line moved OPPOSITE to public money direction. The book is "
            f"baiting more public action onto what they believe is the losing side."
        )
    if sharp_confirms:
        explanations.append(
            f"Sharp money is on the {sharp_money_direction} side, aligning with "
            f"the book and against the public."
        )
    if not explanations:
        explanations.append(
            "No strong trap signals detected. Line movement is consistent with "
            "public action patterns."
        )

    return {
        "is_trap": is_trap,
        "confidence": round(confidence, 3),
        "trap_signals": trap_signals,
        "opening_line": opening_line,
        "current_line": current_line,
        "line_movement": round(line_movement, 2),
        "public_pct": public_pct,
        "sharp_money_direction": sharp_money_direction,
        "expected_movement": round(expected_total_movement, 2),
        "actual_vs_expected": round(abs_movement - expected_total_movement, 2),
        "explanation": " ".join(explanations),
        "actionable_side": (
            "opposite_public" if is_trap
            else "insufficient_signal"
        ),
    }


