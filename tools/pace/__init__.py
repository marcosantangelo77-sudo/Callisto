"""Pace and possession modeling engine.

Split from the former monolithic ``tools/pace_model.py``. This package is a
drop-in replacement: all public names are re-exported here, and
``tools/pace_model`` remains as a facade for backwards compatibility.

Modules:
- constants: Sport enum, LEAGUE_DEFAULTS, PACE_INTERACTION_COEFF
- models: dataclasses (PaceProjection, TotalProjection, PlayerPaceImpact, TotalEdge)
- projection: project_pace, matchup_efficiency
- totals: project_game_total + per-sport total models (NBA/NFL/MLB/NHL/soccer)
- players: player_pace_adjustment
- distributions: Poisson and normal helpers
- edge: detect_total_edge, analyze_game_total, simulate_total_distribution
"""

from tools.pace.constants import LEAGUE_DEFAULTS, PACE_INTERACTION_COEFF, Sport
from tools.pace.distributions import (
    _normal_cdf,
    _normal_pdf,
    poisson_pmf,
    poisson_total_distribution,
)
from tools.pace.edge import analyze_game_total, detect_total_edge, simulate_total_distribution
from tools.pace.models import PaceProjection, PlayerPaceImpact, TotalEdge, TotalProjection
from tools.pace.players import player_pace_adjustment
from tools.pace.projection import matchup_efficiency, project_pace
from tools.pace.totals import (
    _mlb_total,
    _nba_total,
    _nfl_total,
    _nhl_total,
    _soccer_total,
    project_game_total,
)

__all__ = [
    "Sport",
    "LEAGUE_DEFAULTS",
    "PACE_INTERACTION_COEFF",
    "PaceProjection",
    "TotalProjection",
    "PlayerPaceImpact",
    "TotalEdge",
    "project_pace",
    "matchup_efficiency",
    "project_game_total",
    "_nba_total",
    "_nfl_total",
    "_mlb_total",
    "_nhl_total",
    "_soccer_total",
    "player_pace_adjustment",
    "poisson_pmf",
    "poisson_total_distribution",
    "detect_total_edge",
    "analyze_game_total",
    "simulate_total_distribution",
]
