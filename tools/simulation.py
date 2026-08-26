"""
Monte Carlo simulation engine — generate our own probability distributions.

Without our own model, we're just comparing books to each other.
With a simulation engine, we know which SIDE of the divergence is correct.

Sport-specific models:
  Basketball/Football (high-scoring): Normal distribution with pace x efficiency.
  Soccer/Hockey/Baseball (low-scoring): Poisson distribution for goal/run processes.

Player props: Usage/pace/minutes-based simulation with matchup and pace factors.

The output is a "fair line" — our model's implied spread/total.
Compare fair line vs book line -> edge = the difference.

Implementation note:
    The implementation lives in the ``tools.sim`` package. This module is a
    compatibility facade: every public name (plus the private helpers other
    modules rely on) is re-exported below so that ``from tools.simulation
    import X`` keeps working unchanged.
"""

import logging

from tools.odds_api import calculate_implied_probability, calculate_ev  # noqa: F401
from tools.edge_confidence import score_edge  # noqa: F401

from tools.sim.constants import (  # noqa: F401
    DEFAULT_ITERATIONS,
    HIGH_SCORING_SPORTS,
    LOW_SCORING_SPORTS,
    SPORT_DEFAULTS,
    classify_sport,
)
from tools.sim.constants import classify_sport as _classify_sport  # noqa: F401
from tools.sim.models import (  # noqa: F401
    EdgeResult,
    PropSimResult,
    SimulationResult,
    TeamProfile,
)
from tools.sim.game import (  # noqa: F401
    _build_result,
    _poisson_pmf,
    _simulate_high_scoring,
    _simulate_low_scoring,
    _std_dev,
    simulate_game,
)
from tools.sim.markets import simulate_spread, simulate_total  # noqa: F401
from tools.sim.props import simulate_prop  # noqa: F401
from tools.sim.edge import compare_to_book, compare_to_market, compare_poisson_to_market  # noqa: F401
from tools.sim.edge import compare_to_market as compare_to_market_legacy  # noqa: F401
from tools.sim.edge import _make_edge_result  # noqa: F401
from tools.sim.pace_env import _PACE_SPORT_MAP, simulate_game_with_pace_env  # noqa: F401
from tools.sim.legacy import simulate_basketball, simulate_poisson  # noqa: F401

logger = logging.getLogger("callisto.simulation")  # noqa: F821
