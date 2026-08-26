"""
tools.kellypkg — split implementation of tools.kelly.

The canonical Kelly module remains ``tools.kelly`` (a thin facade that
re-exports every public name from this package).  Internal structure:

- constants: AGP tier multipliers, line-movement profiles, CLV decay
- odds:      American->decimal conversion, confidence tier mapping
- core:      kelly_core / kelly_full / kelly_fractional
- dynamic:   kelly_dynamic (confidence + variance adjusted)
- portfolio: kelly_portfolio (correlated multi-bet sizing)
- ruin:      ruin_probability + Monte Carlo ruin simulation
- timing:    timing_value (bet-now vs wait EV)
- units:     calculate_units (unit sizing from bankroll)
"""

from tools.kellypkg.constants import (
    AGP_TIER_MULTIPLIERS,
    LINE_MOVEMENT_PROFILES,
    MARKET_CLV_DECAY,
    _DEFAULT_MOVEMENT_PROFILE,
)
from tools.kellypkg.odds import (
    _american_to_decimal,
    _confidence_tier_from_score,
)
from tools.kellypkg.core import (
    kelly_core,
    kelly_full,
    kelly_fractional,
)
from tools.kellypkg.dynamic import kelly_dynamic
from tools.kellypkg.portfolio import kelly_portfolio
from tools.kellypkg.ruin import (
    ruin_probability,
    _expected_bets_to_ruin_neg_ev,
    _simulate_ruin,
)
from tools.kellypkg.timing import timing_value
from tools.kellypkg.units import calculate_units

__all__ = [
    "AGP_TIER_MULTIPLIERS",
    "LINE_MOVEMENT_PROFILES",
    "MARKET_CLV_DECAY",
    "kelly_core",
    "kelly_full",
    "kelly_fractional",
    "kelly_dynamic",
    "kelly_portfolio",
    "ruin_probability",
    "timing_value",
    "calculate_units",
]
