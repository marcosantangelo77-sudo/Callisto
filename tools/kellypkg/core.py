"""
Core Kelly primitives: kelly_core plus the full/fractional Kelly functions
(split from tools/kelly.py).

``kelly_core`` delegates to the single unrounded implementation in
``tools.kellypkg._formula``; the facade ``tools.kelly`` wraps the very same
implementation so there is exactly ONE Kelly formula in the codebase.
"""

import logging

from tools.odds_api import calculate_implied_probability
from tools.kellypkg._formula import kelly_core_unrounded
from tools.kellypkg.odds import _american_to_decimal

logger = logging.getLogger("callisto.kelly")


def kelly_core(p: float, b: float) -> float:
    """
    Unrounded binary Kelly fraction — THE canonical formula.

        f* = (b*p - q) / b

    where b = net payout per unit risked (decimal_odds - 1),
          p = true win probability,
          q = 1 - p.

    Returns 0.0 when b <= 0 or the bet is not +EV (f* <= 0).  UNROUNDED.
    Both ``kelly_full`` here and ``tools.sizing.kelly_binary`` delegate
    to this primitive so there is exactly one Kelly implementation.
    """
    return kelly_core_unrounded(p, b)


def kelly_full(edge: float, odds) -> float:
    """
    Classic Kelly criterion: optimal fraction of bankroll to wager.

    f* = (b*p - q) / b

    where:
        b = net decimal payout (decimal_odds - 1)
        p = true probability of winning
        q = 1 - p

    Args:
        edge: Your estimated edge as a decimal (e.g., 0.05 for 5% edge).
              This is true_probability - implied_probability.
        odds: American odds being offered.

    Returns:
        Optimal fraction of bankroll (0.0 if no edge).  Never negative.
        ROUNDED TO 6 DECIMAL PLACES.
    """
    implied = calculate_implied_probability(int(odds))
    p = implied + edge  # true probability
    p = max(0.0, min(1.0, p))  # clamp

    b = _american_to_decimal(odds) - 1.0  # net payout per unit risked

    return round(kelly_core(p, b), 6)


def kelly_fractional(
    edge: float,
    odds,
    fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly: reduce full Kelly by a fixed factor.

    Most sharps use quarter-Kelly (fraction=0.25).  This sacrifices ~6%
    of geometric growth rate but cuts drawdown variance by ~75%.  The
    growth-rate curve is flat near the Kelly peak, so you give up
    almost nothing by sizing down.

    Args:
        edge:     Edge as decimal (true_prob - implied_prob).
        odds:     American odds offered.
        fraction: Kelly fraction (0.25 = quarter-Kelly, 0.5 = half-Kelly).

    Returns:
        Reduced fraction of bankroll to wager.
    """
    full = kelly_full(edge, odds)
    return round(full * fraction, 6)
