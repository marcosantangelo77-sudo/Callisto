"""Dataclasses for pace projections and edges."""

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PaceProjection:
    """Result of a matchup pace projection."""
    sport: str
    projected_possessions: float
    pace_factor: float           # >1 = faster than average, <1 = slower
    home_pace_contribution: float
    away_pace_contribution: float
    interaction_term: float      # non-linear compounding effect
    confidence_interval: tuple[float, float] = (0.0, 0.0)


@dataclass
class TotalProjection:
    """Result of a game total projection."""
    sport: str
    projected_total: float
    home_projected: float
    away_projected: float
    confidence_interval: tuple[float, float]  # 90% CI
    pace_factor: float
    efficiency_matchup_home: float  # adjusted off eff for home team
    efficiency_matchup_away: float  # adjusted off eff for away team
    methodology: str = ""


@dataclass
class PlayerPaceImpact:
    """Impact of a player's presence/absence on team pace."""
    player_pace_on: float
    player_pace_off: float
    projected_minutes: float
    team_total_minutes: float
    pace_delta: float            # how much team pace changes
    projected_total_delta: float # how much game total changes
    minutes_fraction: float


@dataclass
class TotalEdge:
    """Over/under edge detection result."""
    edge_direction: str          # "over" or "under"
    edge_pct: float              # percentage edge
    recommended_side: str        # "over" or "under"
    ev: dict                     # expected value calculation
    projected_total: float
    book_total: float
    over_probability: float
    under_probability: float
    kelly_fraction: float        # kelly criterion bet sizing
