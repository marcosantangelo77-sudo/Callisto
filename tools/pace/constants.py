"""Constants and sport definitions for the pace modeling engine."""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums & constants
# ---------------------------------------------------------------------------

class Sport(str, Enum):
    NBA = "nba"
    NFL = "nfl"
    MLB = "mlb"
    NHL = "nhl"
    SOCCER = "soccer"


# League-average reference values (updated each season; sensible 2025-26 defaults)
LEAGUE_DEFAULTS: dict[str, dict] = {
    Sport.NBA: {
        "pace": 100.0,              # possessions per 48 min
        "off_eff": 112.0,           # points per 100 possessions
        "def_eff": 112.0,           # same (league avg offense == defense)
        "total_minutes": 240.0,     # 5 players x 48 min
        "game_minutes": 48.0,
        "score_std": 11.0,          # typical game-to-game std dev
    },
    Sport.NFL: {
        "plays_per_game": 64.0,     # offensive plays per team per game
        "yards_per_play": 5.5,
        "yards_per_point": 14.0,    # ~total yards / points scored
        "avg_total": 46.5,
        "top_factor": 1.0,          # time of possession multiplier
        "score_std": 10.0,
    },
    Sport.MLB: {
        "runs_per_game": 4.5,       # per team
        "league_era": 4.10,
        "league_ops": 0.720,
        "league_fip": 4.00,
        "score_std": 3.0,
    },
    Sport.NHL: {
        "goals_per_game": 3.10,     # per team
        "shots_per_game": 30.0,
        "save_pct": 0.905,
        "shooting_pct": 0.095,
        "score_std": 1.6,
    },
    Sport.SOCCER: {
        "xg_per_game": 1.35,       # per team, typical top-5 league
        "shots_per_game": 12.0,
        "shot_conversion": 0.112,
        "score_std": 1.1,
    },
}

# Non-linearity coefficients for pace interaction
# When both teams are fast (above avg), the compounding effect amplifies pace.
# Derived from empirical NBA data: the interaction term adds ~3-5% extra possessions
# when both teams are 5+ possessions above league average.
PACE_INTERACTION_COEFF: dict[str, float] = {
    Sport.NBA: 0.0015,     # per (delta_a * delta_b) possessions
    Sport.NFL: 0.0010,
    Sport.NHL: 0.0008,
    Sport.SOCCER: 0.0005,
}
