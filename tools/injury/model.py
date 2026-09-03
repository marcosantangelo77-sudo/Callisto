"""Quantitative injury impact and usage projection model — computation layer.

Every injury is a market event. The question is never "is this team worse
without Player X?" — the question is "by HOW MUCH, and has the market
fully priced it?"

Five capabilities:
1. Player impact quantification — marginal value over replacement in points/cents
2. Usage redistribution — where do the absent player's touches/targets/shots go?
3. Matchup-dependent adjustment — missing a rim protector matters more vs Jokic
4. Position impact database — see tools.injury.data for the lookup tables
5. Market adjustment speed — how fast do books reprice after injury news?

No external API calls. Pure computation against hardcoded research baselines.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("callisto.injury_model")


# ---------------------------------------------------------------------------
# Dataclasses for structured results
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Section 5: Dataclasses for structured results
# ---------------------------------------------------------------------------

@dataclass
class PlayerImpactResult:
    """Quantified impact of a player being absent."""
    player_name: str
    team: str
    sport: str
    position: str
    tier: str
    marginal_value_over_replacement: float  # points (NBA/NFL) or cents (MLB)
    spread_impact: float                    # how much the spread should move
    total_impact: float                     # impact on game total (points)
    prop_redistribution: dict               # which teammates benefit for props
    confidence: float                       # 0-1, how certain we are in the estimate
    notes: list[str] = field(default_factory=list)


@dataclass
class UsageRedistribution:
    """How a single teammate's workload changes when a player is out."""
    player: str
    role: str
    usage_increase: float           # percentage points of usage gained
    projected_stat_change: dict     # e.g., {"points": +4.2, "rebounds": +1.1}


@dataclass
class MatchupAdjustedImpact:
    """Injury impact adjusted for the specific opponent."""
    base_impact: float
    matchup_multiplier: float
    adjusted_spread_impact: float
    reasoning: list[str]


@dataclass
class MarketAdjustmentEstimate:
    """How much of the injury impact has been priced in by markets."""
    pct_adjusted: float             # 0-1, how much of the move has happened
    window_remaining_minutes: float # estimated minutes until 95% adjusted
    edge_remaining: float           # estimated remaining mispricing
    significance_tier: str          # star / starter / role_player
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def player_impact(
    player_name: str,
    team: str,
    sport: str,
    role: Optional[str] = None,
    position: Optional[str] = None,
    ppg: float = 0.0,
    bpm: float = 0.0,
    is_starter: bool = True,
    era: Optional[float] = None,
    war: Optional[float] = None,
    backup_info: Optional[dict] = None,
    teammates: Optional[list[dict]] = None,
) -> PlayerImpactResult:
    """
    Quantify the impact of a player being OUT.

    This is the central function. Given a player's identity and stats,
    it returns:
    - marginal_value_over_replacement: how many points/cents worse the team is
    - spread_impact: how much the spread should move toward the opponent
    - total_impact: how much the game total should decrease (usually negative)
    - prop_redistribution: which teammates see increased usage/stats

    Args:
        player_name: Player's name.
        team: Team name/abbreviation.
        sport: One of "NBA", "NFL", "MLB".
        role: Optional role descriptor (e.g., "rim_protector", "floor_spacer").
        position: Position code (e.g., "PG", "QB", "SP").
        ppg: Points per game (NBA).
        bpm: Box Plus/Minus (NBA).
        is_starter: Whether the player is a starter.
        era: Earned Run Average (MLB pitchers).
        war: Wins Above Replacement (MLB position players).
        backup_info: Dict with "name" and "quality" keys for the replacement.
        teammates: List of teammate dicts for usage redistribution.

    Returns:
        PlayerImpactResult with all quantified impacts.
    """
    sport = sport.upper()
    notes = []

    from tools.injury.impact_nba import (
        _nba_player_impact,
    )
    from tools.injury.impact_nfl_mlb import (
        _mlb_player_impact,
        _nfl_player_impact,
    )

    if sport == "NBA":
        return _nba_player_impact(
            player_name, team, position, role, ppg, bpm,
            is_starter, teammates, notes,
        )
    elif sport == "NFL":
        return _nfl_player_impact(
            player_name, team, position, role, backup_info,
            teammates, notes,
        )
    elif sport == "MLB":
        return _mlb_player_impact(
            player_name, team, position, era, war,
            teammates, notes,
        )
    else:
        logger.warning(f"Unsupported sport: {sport}")
        return PlayerImpactResult(
            player_name=player_name, team=team, sport=sport,
            position=position or "UNK", tier="unknown",
            marginal_value_over_replacement=0.0, spread_impact=0.0,
            total_impact=0.0, prop_redistribution={}, confidence=0.1,
            notes=[f"Unsupported sport: {sport}"],
        )


# ---------------------------------------------------------------------------
# Usage Redistribution (cross-sport entry point)
# ---------------------------------------------------------------------------

def redistribute_usage(
    absent_player: str,
    team_roster: list[dict],
    sport: str,
    absent_player_stats: Optional[dict] = None,
) -> list[UsageRedistribution]:
    """
    Project how usage/touches/targets redistribute when a player is OUT.

    This is where prop betting edge lives. When the 30 PPG scorer sits,
    someone absorbs those shots. Whoever gets the biggest share is likely
    underpriced in their player props.

    Args:
        absent_player: Name of the player who is out.
        team_roster: List of teammate dicts. Expected keys vary by sport:
            NBA: {"name", "ppg", "rpg", "apg", "usage_rate", "position"}
            NFL: {"name", "role", "targets_per_game", "carries_per_game"}
            MLB: {"name", "position", "pa_per_game", "avg", "obp"}
        sport: One of "NBA", "NFL", "MLB".
        absent_player_stats: Stats of the absent player for calibration.

    Returns:
        List of UsageRedistribution objects, sorted by usage_increase descending.
    """
    sport = sport.upper()

    from tools.injury.impact_nba import (
        _nba_usage_redistribution,
    )
    from tools.injury.impact_nfl_mlb import (
        _mlb_usage_redistribution,
        _nfl_usage_redistribution,
    )

    if sport == "NBA":
        return _nba_usage_redistribution(absent_player, team_roster, absent_player_stats)
    elif sport == "NFL":
        return _nfl_usage_redistribution(absent_player, team_roster, absent_player_stats)
    elif sport == "MLB":
        return _mlb_usage_redistribution(absent_player, team_roster, absent_player_stats)
    else:
        logger.warning(f"Unsupported sport for redistribution: {sport}")
        return []


def _determine_nba_tier(
    ppg: float = 0.0,
    bpm: float = 0.0,
    is_starter: bool = True,
) -> tuple[int, str]:
    from tools.injury.impact_nba import _determine_nba_tier as _impl
    return _impl(ppg, bpm, is_starter)


def _nba_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    role: Optional[str],
    ppg: float,
    bpm: float,
    is_starter: bool,
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    from tools.injury.impact_nba import _nba_player_impact as _impl
    return _impl(
        player_name, team, position, role, ppg, bpm,
        is_starter, teammates, notes,
    )


def _nba_redistribute_usage(
    absent_ppg: float,
    teammates: list[dict],
) -> dict:
    from tools.injury.impact_nba import _nba_redistribute_usage as _impl
    return _impl(absent_ppg, teammates)


def _nba_usage_redistribution(
    absent_player: str,
    roster: list[dict],
    absent_stats: Optional[dict],
) -> list[UsageRedistribution]:
    from tools.injury.impact_nba import _nba_usage_redistribution as _impl
    return _impl(absent_player, roster, absent_stats)


def _determine_nfl_backup_quality(backup_info: Optional[dict] = None) -> int:
    from tools.injury.impact_nfl_mlb import _determine_nfl_backup_quality as _impl
    return _impl(backup_info)


def _determine_mlb_tier(
    position: str,
    era: Optional[float] = None,
    war: Optional[float] = None,
) -> int:
    from tools.injury.impact_nfl_mlb import _determine_mlb_tier as _impl
    return _impl(position, era, war)


def _nfl_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    role: Optional[str],
    backup_info: Optional[dict],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    from tools.injury.impact_nfl_mlb import _nfl_player_impact as _impl
    return _impl(
        player_name, team, position, role, backup_info, teammates, notes,
    )


def _mlb_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    era: Optional[float],
    war: Optional[float],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    from tools.injury.impact_nfl_mlb import _mlb_player_impact as _impl
    return _impl(player_name, team, position, era, war, teammates, notes)


# Re-export NBA impact helpers (defined in tools.injury.impact_nba).
from tools.injury.impact_nba import (  # noqa: F401
    _determine_nba_tier,
    _nba_player_impact,
    _nba_redistribute_usage,
    _nba_usage_redistribution,
)

# Re-export NFL/MLB impact helpers (defined in tools.injury.impact_nfl_mlb).
from tools.injury.impact_nfl_mlb import (  # noqa: F401
    _determine_mlb_tier,
    _determine_nfl_backup_quality,
    _mlb_player_impact,
    _mlb_usage_redistribution,
    _nfl_player_impact,
    _nfl_redistribute_targets,
    _nfl_usage_redistribution,
)

# Re-export high-level analysis helpers (defined in tools.injury.analysis).
from tools.injury.analysis import (  # noqa: F401
    estimate_market_adjustment,
    full_injury_analysis,
    lookup_position_impact,
    matchup_adjusted_impact,
)
