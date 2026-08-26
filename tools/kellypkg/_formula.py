"""
THE single unrounded Kelly formula.  Nothing else in this package (or in
tools.sizing / tools.kelly) may reimplement it — they must delegate here.
"""


def kelly_core_unrounded(p: float, b: float) -> float:
    """
    Unrounded binary Kelly fraction — THE canonical formula.

        f* = (b*p - q) / b

    where b = net payout per unit risked (decimal_odds - 1),
          p = true win probability,
          q = 1 - p.

    Returns 0.0 when b <= 0 or the bet is not +EV (f* <= 0).
    Never rounded: ``tools.sizing.kelly_binary`` depends on full precision,
    while ``kelly_full`` rounds its own return value to 6 decimals.
    """
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)
