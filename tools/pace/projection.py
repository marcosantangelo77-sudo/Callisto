"""Matchup pace projection and efficiency adjustment."""

import logging
import math
from typing import Optional

from tools.pace.constants import PACE_INTERACTION_COEFF, Sport
from tools.pace.models import PaceProjection

logger = logging.getLogger("callisto.pace_model")


# ---------------------------------------------------------------------------
# 1. Matchup pace projection (sport-agnostic core)
# ---------------------------------------------------------------------------


def project_pace(
    home_pace: float,
    away_pace: float,
    league_avg: float,
    sport: str,
) -> PaceProjection:
    """
    Project matchup-specific possessions/pace accounting for non-linear interaction.

    The key insight: pace is not simply the average of two teams' paces.
    When two fast teams meet, they push each other faster (compounding).
    When two slow teams meet, they drag each other slower.
    The interaction is non-linear — modeled as a quadratic term on the
    product of each team's deviation from league average.

    For NBA: possessions = (pace_a + pace_b) / 2 + interaction_term
    where interaction_term = coeff * (pace_a - avg) * (pace_b - avg)

    This means:
    - Two teams both +5 above avg: interaction adds ~0.0375 extra possessions
      (small per game, but enough to shift a total by 0.5-1 point)
    - One fast (+5) and one slow (-5): interaction SUBTRACTS ~0.0375
      (the slow team's style partially cancels the fast team's desire to run)
    - Two slow teams both -5: interaction adds ~0.0375 FEWER possessions
      (they compound each other's slowness)
    """
    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
    coeff = PACE_INTERACTION_COEFF.get(sport_key, 0.001)

    home_delta = home_pace - league_avg
    away_delta = away_pace - league_avg

    # Base pace: weighted average (home team gets slight weight for home court)
    if sport_key == Sport.NBA:
        # In NBA, pace is roughly equal contribution — slight home advantage
        base_pace = (home_pace * 0.52 + away_pace * 0.48)
    elif sport_key == Sport.NFL:
        # In NFL, home team controls pace more (play calling, crowd noise)
        base_pace = (home_pace * 0.55 + away_pace * 0.45)
    else:
        base_pace = (home_pace + away_pace) / 2.0

    # Non-linear interaction term
    # When both deltas have the same sign (both fast or both slow), this amplifies
    # When opposite signs, this dampens
    interaction = coeff * home_delta * away_delta

    projected = base_pace + interaction

    # Pace factor relative to league average
    pace_factor = projected / league_avg if league_avg > 0 else 1.0

    # Confidence interval: pace has ~3-4 possession std dev game-to-game in NBA
    if sport_key == Sport.NBA:
        pace_std = 3.5
    elif sport_key == Sport.NFL:
        pace_std = 4.0
    else:
        pace_std = 2.5

    ci_low = projected - 1.645 * pace_std   # 90% CI
    ci_high = projected + 1.645 * pace_std

    result = PaceProjection(
        sport=sport_key.value,
        projected_possessions=round(projected, 2),
        pace_factor=round(pace_factor, 4),
        home_pace_contribution=round(home_delta, 2),
        away_pace_contribution=round(away_delta, 2),
        interaction_term=round(interaction, 4),
        confidence_interval=(round(ci_low, 2), round(ci_high, 2)),
    )

    logger.info(
        f"Pace projection ({sport_key.value}): "
        f"home={home_pace}, away={away_pace}, avg={league_avg} -> "
        f"projected={result.projected_possessions} (factor={result.pace_factor}), "
        f"interaction={result.interaction_term}"
    )

    return result


# ---------------------------------------------------------------------------
# 2. Matchup efficiency adjustment
# ---------------------------------------------------------------------------

def matchup_efficiency(
    team_off_eff: float,
    opponent_def_eff: float,
    league_avg_eff: float,
    sport: str,
) -> float:
    """
    Adjust offensive efficiency for a specific defensive matchup.

    Why not just average? Because the relationship is multiplicative, not additive.
    A 115 offense vs a 105 defense (both above avg) is NOT the same as
    a 110 offense vs a 110 defense, even though both "average" to 110.

    The formula uses the "log5" style adjustment:
      adjusted = team_off * (opponent_def / league_avg)
    This properly accounts for the defensive resistance.

    A 115 offense vs a 120 defense (bad defense) in a 112 league:
      115 * (120/112) = 123.2 — the offense feasts on the bad defense
    A 115 offense vs a 105 defense (great defense) in a 112 league:
      115 * (105/112) = 107.8 — the defense clamps down significantly

    For NFL, the adjustment uses different denominators (yards/play, etc.)
    """
    if league_avg_eff <= 0:
        return team_off_eff

    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport

    if sport_key in (Sport.NBA, Sport.NHL, Sport.SOCCER):
        # Multiplicative adjustment: offense * (opponent_defense / league_avg)
        # For defense: higher number = worse defense (more points allowed)
        # So dividing by league_avg gives a scaling factor:
        #   > 1.0 means defense is worse than average (offense does better)
        #   < 1.0 means defense is better than average (offense does worse)
        defensive_factor = opponent_def_eff / league_avg_eff
        adjusted = team_off_eff * defensive_factor

    elif sport_key == Sport.NFL:
        # NFL: similar principle but with diminishing returns at extremes
        # A truly elite defense doesn't completely shut down a great offense
        defensive_factor = opponent_def_eff / league_avg_eff

        # Apply diminishing returns via square root compression at extremes
        if defensive_factor > 1.0:
            # Bad defense: offense benefits, but with slight compression
            excess = defensive_factor - 1.0
            compressed = 1.0 + excess * 0.85
            adjusted = team_off_eff * compressed
        else:
            # Good defense: offense suffers, but with slight compression
            deficit = 1.0 - defensive_factor
            compressed = 1.0 - deficit * 0.85
            adjusted = team_off_eff * compressed

    elif sport_key == Sport.MLB:
        # MLB: pitcher quality vs lineup quality
        # Use geometric mean for a balanced blending
        adjusted = math.sqrt(team_off_eff * opponent_def_eff)

    else:
        # Fallback: simple weighted average favoring offense slightly
        adjusted = team_off_eff * 0.6 + opponent_def_eff * 0.4

    logger.debug(
        f"Matchup efficiency ({sport_key.value}): "
        f"off={team_off_eff}, def={opponent_def_eff}, avg={league_avg_eff} -> "
        f"adjusted={adjusted:.2f}"
    )

    return round(adjusted, 2)
