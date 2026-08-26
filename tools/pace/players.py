"""Player-level pace impact."""

import logging
from typing import Optional

from tools.pace.constants import LEAGUE_DEFAULTS, Sport
from tools.pace.models import PlayerPaceImpact

logger = logging.getLogger("callisto.pace_model")


# ---------------------------------------------------------------------------
# 4. Player-level pace impact
# ---------------------------------------------------------------------------


def player_pace_adjustment(
    player_pace_on: float,
    player_pace_off: float,
    projected_minutes: float,
    team_total_minutes: float,
    sport: str,
    team_off_eff: Optional[float] = None,
    team_def_eff: Optional[float] = None,
    league_avg_eff: Optional[float] = None,
) -> PlayerPaceImpact:
    """
    Calculate how a player's presence/absence affects team pace and game total.

    When a fast-paced player is injured:
    - Team pace drops by (player_pace_on - player_pace_off) * minutes_fraction
    - Fewer possessions = lower total
    - The total delta depends on both the pace change AND the scoring efficiency

    When a slow-paced player is injured:
    - Team pace RISES (replacement plays faster)
    - More possessions = higher total

    This is critical for injury-adjusted totals. Books are slow to adjust totals
    for role player injuries that affect pace more than scoring directly.

    Args:
        player_pace_on: team pace (possessions/48 min) when player is on court
        player_pace_off: team pace when player is off court
        projected_minutes: minutes the player is expected to play (or 0 if out)
        team_total_minutes: total player-minutes per game (NBA=240, NFL=varies)
        sport: sport identifier
        team_off_eff: optional team offensive efficiency (for total delta calc)
        team_def_eff: optional team defensive efficiency (for total delta calc)
        league_avg_eff: optional league average efficiency

    Returns:
        PlayerPaceImpact with pace_delta and projected_total_delta
    """
    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
    defaults = LEAGUE_DEFAULTS.get(sport_key, LEAGUE_DEFAULTS[Sport.NBA])

    if team_total_minutes <= 0:
        team_total_minutes = defaults.get("total_minutes", 240.0)

    # What fraction of total minutes does this player account for?
    minutes_fraction = projected_minutes / team_total_minutes
    minutes_fraction = max(0.0, min(1.0, minutes_fraction))

    # Pace differential: how much faster/slower is the team with this player?
    on_off_diff = player_pace_on - player_pace_off

    # Team pace change when player is OUT = they lose the minutes where pace was
    # player_pace_on and replace with minutes at player_pace_off
    # Net pace delta = on_off_diff * minutes_fraction
    pace_delta = on_off_diff * minutes_fraction

    # Convert pace delta to total delta
    # Each extra possession is worth ~(off_eff + opp_off_eff) / 100 points
    # Approximate: each possession is worth ~2.1-2.3 total points in NBA
    if team_off_eff is not None and team_def_eff is not None:
        # Points per possession for BOTH teams
        if league_avg_eff is None:
            league_avg_eff = defaults.get("off_eff", 112.0)
        # Rough: each extra possession = (team_eff + opponent_eff) / 100 points
        # Opponent efficiency approximated by league average
        pts_per_possession = (team_off_eff + league_avg_eff) / 100.0
    else:
        if sport_key == Sport.NBA:
            pts_per_possession = 2.24  # ~112 eff * 2 / 100
        elif sport_key == Sport.NFL:
            pts_per_possession = 0.8   # rough approximation
        elif sport_key == Sport.NHL:
            pts_per_possession = 0.19  # goals per "possession"
        elif sport_key == Sport.SOCCER:
            pts_per_possession = 0.11
        else:
            pts_per_possession = 1.0

    # If the player is OUT (projected_minutes = 0), the total drops by pace_delta * pts_per_poss
    # If the player is IN, the total is at baseline (no delta)
    # For comparison purposes, we report what happens when the player is REMOVED
    total_delta = pace_delta * pts_per_possession

    result = PlayerPaceImpact(
        player_pace_on=player_pace_on,
        player_pace_off=player_pace_off,
        projected_minutes=projected_minutes,
        team_total_minutes=team_total_minutes,
        pace_delta=round(pace_delta, 2),
        projected_total_delta=round(total_delta, 2),
        minutes_fraction=round(minutes_fraction, 4),
    )

    logger.info(
        f"Player pace impact ({sport_key.value}): "
        f"on={player_pace_on}, off={player_pace_off}, "
        f"mins={projected_minutes}/{team_total_minutes} -> "
        f"pace_delta={result.pace_delta}, total_delta={result.projected_total_delta}"
    )

    return result
