"""
Callisto simulation engine — generate our own probability distributions.

This is the CORE of Callisto. Everything downstream depends on accuracy here.

Four simulation types:
  1. NBA: Possession-based Monte Carlo (100K sims)
  2. NFL: Negative Binomial (discrete, fat-tailed, natural ≥ 0)
  3. Poisson: MLB, NHL, Soccer (analytical — no simulation needed)
  4. Player Props: Per-minute rate × context-adjusted minutes

Uses numpy for vectorized Monte Carlo. Pure Python for Poisson PMF.
No scipy dependency (blocked by DLL policy on this system).

NOTE: This package shadows the historical flat module at tools/sim.py.
The legacy flat-module API (nba_game_sim, nfl_game_sim, poisson_game,
player_prop_sim, compare_sim_to_book, sim_from_odds) is still importable
from tools.sim via lazy re-export below.
"""

import importlib.util
import logging
import pathlib
import types

logger = logging.getLogger("callisto.sim")

__all__ = [
    # constants
    "DEFAULT_ITERATIONS", "HIGH_SCORING_SPORTS", "LOW_SCORING_SPORTS",
    "SPORT_DEFAULTS", "classify_sport",
    # models
    "TeamProfile", "SimulationResult", "PropSimResult", "EdgeResult",
    # game
    "simulate_game", "_poisson_pmf", "_std_dev",
    # markets
    "simulate_spread", "simulate_total",
    # props
    "simulate_prop",
    # edge
    "compare_to_book", "compare_to_market", "compare_poisson_to_market",
    "_make_edge_result", "make_edge_result",
    # pace/env
    "simulate_game_with_pace_env",
    # legacy
    "simulate_basketball", "simulate_poisson",
]

from tools.sim.constants import (  # noqa: E402,F401
    DEFAULT_ITERATIONS,
    HIGH_SCORING_SPORTS,
    LOW_SCORING_SPORTS,
    SPORT_DEFAULTS,
    classify_sport,
)
from tools.sim.models import (  # noqa: E402,F401
    EdgeResult,
    PropSimResult,
    SimulationResult,
    TeamProfile,
)
from tools.sim.game import simulate_game, _poisson_pmf, _std_dev  # noqa: E402,F401
from tools.sim.markets import simulate_spread, simulate_total  # noqa: E402,F401
from tools.sim.props import simulate_prop  # noqa: E402,F401
from tools.sim.edge import (  # noqa: E402,F401
    _make_edge_result,
    compare_to_book,
    compare_to_market,
    compare_poisson_to_market,
    make_edge_result,
)
from tools.sim.pace_env import simulate_game_with_pace_env  # noqa: E402,F401
from tools.sim.legacy import simulate_basketball, simulate_poisson  # noqa: E402,F401


def __getattr__(name: str):
    """Lazy fallback to the legacy flat module (tools/sim.py) for names that
    were part of its public API before this directory existed."""
    if name in {
        "nba_game_sim", "nfl_game_sim", "poisson_game", "player_prop_sim",
        "compare_sim_to_book", "sim_from_odds",
        "american_to_decimal", "american_to_implied",
    }:
        path = pathlib.Path(__file__).resolve().parent.parent / "sim.py"
        spec = importlib.util.spec_from_file_location("_tools_sim_flat", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Cache on this package so subsequent lookups are direct attribute hits.
        globals()[name] = getattr(module, name)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
