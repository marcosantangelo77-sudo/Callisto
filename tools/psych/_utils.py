"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""Shared utility helpers."""

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _prob_to_american(prob: float) -> int:
    """Convert probability to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    else:
        return int(100 * (1 - prob) / prob)


