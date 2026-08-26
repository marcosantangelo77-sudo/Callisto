"""
Kelly Criterion and bankroll optimization for Callisto.  FACADE MODULE.

Full-spectrum bankroll management:
- Classic full Kelly and fractional Kelly (quarter-Kelly default for sharps)
- Dynamic Kelly that integrates AGP confidence tiers and variance
- Simultaneous Kelly for correlated multi-bet portfolios
- Bankroll ruin probability modeling
- Optimal bet timing via line movement EV estimation
- Unit sizing from bankroll with confidence-weighted scaling

The central insight: bet sizing matters as much as bet selection.
A 3% edge with tight variance and VERIFIED confidence deserves
more capital than a 5% edge with wide variance and SPECULATIVE data.
Kelly maximizes long-run geometric growth rate — but full Kelly is
too aggressive for real-world variance. Quarter Kelly is the sweet spot
for sharps who want growth without ruin.

This module is now a thin facade: the implementation lives in
``tools.kellypkg`` (NOT ``tools.kelly/``, which would shadow this module).
Every public name is re-exported below so all existing
``from tools.kelly import ...`` call sites keep working unchanged.

INVARIANT: kelly_full rounds to 6 decimal places; kelly_core stays
unrounded via tools.kellypkg._formula.  The two Kelly paths are NOT merged.
"""

from tools.kellypkg._formula import kelly_core_unrounded


def kelly_core(p: float, b: float) -> float:
    """
    Unrounded binary Kelly fraction — THE canonical formula.

        f* = (b*p - q) / b

    where b = net payout per unit risked (decimal_odds - 1),
          p = true win probability,
          q = 1 - p.

    Returns 0.0 when b <= 0 or the bet is not +EV (f* <= 0).  UNROUNDED.
    Delegates to the single implementation shared with tools.kellypkg and
    tools.sizing.kelly_binary.
    """
    return kelly_core_unrounded(p, b)


# Re-export everything else from the split package.
from tools.kellypkg import *          # noqa: E402,F401,F403
from tools.kellypkg import (           # noqa: E402,F401  (private helpers too)
    _DEFAULT_MOVEMENT_PROFILE,
    _american_to_decimal,
    _confidence_tier_from_score,
    _expected_bets_to_ruin_neg_ev,
    _simulate_ruin,
)
from tools.kellypkg.core import logger  # noqa: E402,F401
