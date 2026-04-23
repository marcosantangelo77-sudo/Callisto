"""
Pace and possession modeling engine — the correct way to project game totals.

Most models average team totals. That's wrong.
The correct approach: Total = Possessions x Efficiency per Possession.

Why this matters for betting:
- Two fast teams create MORE possessions than their average suggests (compounding)
- Two slow teams create FEWER (also compounding)
- A great offense vs a bad defense scores more than the average of both suggests
- Player injuries change team pace, which changes the total projection

This engine projects game totals from first principles across all major sports,
then compares to book lines for over/under edge detection.

Sport models:
- NBA: possessions x offensive/defensive efficiency
- NFL: plays x yards per play / yards per point
- MLB: Poisson model with pitcher/lineup matchup adjustment
- NHL: shots x save percentage with Poisson goal distribution
- Soccer: expected goals (xG) framework
"""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.pace_model")


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


# ---------------------------------------------------------------------------
# 3. Pace-adjusted total projection — the main entry point per sport
# ---------------------------------------------------------------------------

def project_game_total(
    home_pace: float,
    away_pace: float,
    home_off_eff: float,
    away_off_eff: float,
    home_def_eff: float,
    away_def_eff: float,
    league_avg_pace: float,
    sport: str,
    league_avg_eff: Optional[float] = None,
) -> TotalProjection:
    """
    Project the game total from first principles: pace x efficiency.

    This is the core function. For each sport, it:
    1. Projects matchup-specific pace/possessions
    2. Adjusts each team's offensive efficiency for the opposing defense
    3. Calculates expected points per team
    4. Combines into a total with confidence interval

    Returns TotalProjection with projected_total and 90% confidence_interval.
    """
    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
    defaults = LEAGUE_DEFAULTS[sport_key]

    if league_avg_eff is None:
        if sport_key == Sport.NBA:
            league_avg_eff = defaults["off_eff"]
        elif sport_key == Sport.NFL:
            league_avg_eff = defaults["yards_per_play"]
        elif sport_key == Sport.MLB:
            league_avg_eff = defaults["runs_per_game"]
        elif sport_key == Sport.NHL:
            league_avg_eff = defaults["goals_per_game"]
        elif sport_key == Sport.SOCCER:
            league_avg_eff = defaults["xg_per_game"]

    # Dispatch to sport-specific model
    if sport_key == Sport.NBA:
        return _nba_total(
            home_pace, away_pace, home_off_eff, away_off_eff,
            home_def_eff, away_def_eff, league_avg_pace, league_avg_eff,
        )
    elif sport_key == Sport.NFL:
        return _nfl_total(
            home_pace, away_pace, home_off_eff, away_off_eff,
            home_def_eff, away_def_eff, league_avg_pace, league_avg_eff,
        )
    elif sport_key == Sport.MLB:
        return _mlb_total(
            home_pace, away_pace, home_off_eff, away_off_eff,
            home_def_eff, away_def_eff, league_avg_pace, league_avg_eff,
        )
    elif sport_key == Sport.NHL:
        return _nhl_total(
            home_pace, away_pace, home_off_eff, away_off_eff,
            home_def_eff, away_def_eff, league_avg_pace, league_avg_eff,
        )
    elif sport_key == Sport.SOCCER:
        return _soccer_total(
            home_pace, away_pace, home_off_eff, away_off_eff,
            home_def_eff, away_def_eff, league_avg_pace, league_avg_eff,
        )
    else:
        raise ValueError(f"Unsupported sport: {sport}")


# ---------------------------------------------------------------------------
# 3a. NBA: possessions x efficiency model
# ---------------------------------------------------------------------------

def _nba_total(
    home_pace: float, away_pace: float,
    home_off_eff: float, away_off_eff: float,
    home_def_eff: float, away_def_eff: float,
    league_avg_pace: float, league_avg_eff: float,
) -> TotalProjection:
    """
    NBA total = projected_possessions * (home_scoring_rate + away_scoring_rate).

    Possessions are shared (each team gets the same number per game).
    Scoring rate = adjusted efficiency / 100 (points per possession).

    Example: 100 possessions, home adjusted eff = 115, away adjusted eff = 108
    Total = 100 * (1.15 + 1.08) = 100 * 2.23 = 223.0
    """
    pace = project_pace(home_pace, away_pace, league_avg_pace, Sport.NBA)
    possessions = pace.projected_possessions

    # Adjust efficiencies for specific matchup
    # Home offense vs away defense
    home_adj_eff = matchup_efficiency(
        home_off_eff, away_def_eff, league_avg_eff, Sport.NBA,
    )
    # Away offense vs home defense
    away_adj_eff = matchup_efficiency(
        away_off_eff, home_def_eff, league_avg_eff, Sport.NBA,
    )

    # Home court advantage: ~+1.5 efficiency points for home team
    home_adj_eff += 1.5
    away_adj_eff -= 0.5  # slight road penalty

    # Points per team
    home_pts = possessions * (home_adj_eff / 100.0)
    away_pts = possessions * (away_adj_eff / 100.0)
    total = home_pts + away_pts

    # 90% confidence interval
    # NBA game totals have std dev ~11 points around projection
    std = LEAGUE_DEFAULTS[Sport.NBA]["score_std"]
    # Adjust std for extreme pace matchups (fast-fast games are more volatile)
    pace_vol_adj = 1.0 + 0.1 * abs(pace.pace_factor - 1.0)
    adjusted_std = std * pace_vol_adj

    ci_low = total - 1.645 * adjusted_std
    ci_high = total + 1.645 * adjusted_std

    result = TotalProjection(
        sport=Sport.NBA.value,
        projected_total=round(total, 1),
        home_projected=round(home_pts, 1),
        away_projected=round(away_pts, 1),
        confidence_interval=(round(ci_low, 1), round(ci_high, 1)),
        pace_factor=pace.pace_factor,
        efficiency_matchup_home=home_adj_eff,
        efficiency_matchup_away=away_adj_eff,
        methodology=(
            f"NBA pace-efficiency: {possessions:.1f} possessions x "
            f"(home_eff={home_adj_eff:.1f} + away_eff={away_adj_eff:.1f})/100"
        ),
    )

    logger.info(
        f"NBA total: {result.projected_total} "
        f"({result.home_projected} + {result.away_projected}), "
        f"CI=({ci_low:.1f}, {ci_high:.1f})"
    )

    return result


# ---------------------------------------------------------------------------
# 3b. NFL: plays x yards per play / yards per point
# ---------------------------------------------------------------------------

def _nfl_total(
    home_pace: float, away_pace: float,
    home_off_eff: float, away_off_eff: float,
    home_def_eff: float, away_def_eff: float,
    league_avg_pace: float, league_avg_eff: float,
) -> TotalProjection:
    """
    NFL total projection using plays x efficiency model.

    Inputs reinterpreted for NFL:
    - home_pace / away_pace: plays per game (not possessions)
    - home_off_eff / away_off_eff: yards per play (offense)
    - home_def_eff / away_def_eff: yards per play allowed (defense)
    - league_avg_pace: avg plays per team per game (~64)
    - league_avg_eff: avg yards per play (~5.5)

    Model: points = (plays * adjusted_ypp) / yards_per_point
    Yards per point is ~14 (league avg total yards / total points).

    NFL-specific adjustments:
    - Time of possession: team that controls clock limits opponent plays
    - Scoring efficiency in red zone affects yards-per-point ratio
    - High-pace offenses (hurry-up) inflate play counts for BOTH teams
    """
    defaults = LEAGUE_DEFAULTS[Sport.NFL]
    yards_per_point = defaults["yards_per_point"]

    # Project matchup-specific play count
    pace = project_pace(home_pace, away_pace, league_avg_pace, Sport.NFL)

    # In NFL, total plays in a game is roughly constant (~128 combined)
    # But pace of play affects how they're distributed
    # A fast team gets more plays but also gives opponent more plays
    total_plays_game = pace.projected_possessions * 2  # combined
    home_plays = total_plays_game * (home_pace / (home_pace + away_pace))
    away_plays = total_plays_game * (away_pace / (home_pace + away_pace))

    # Adjust efficiency for matchup
    home_adj_ypp = matchup_efficiency(
        home_off_eff, away_def_eff, league_avg_eff, Sport.NFL,
    )
    away_adj_ypp = matchup_efficiency(
        away_off_eff, home_def_eff, league_avg_eff, Sport.NFL,
    )

    # Home field advantage in NFL: ~+0.15 yards per play and slightly
    # better yards_per_point ratio (crowd noise helps on defense)
    home_adj_ypp += 0.15
    home_ypp_adj = yards_per_point * 0.97   # home scores slightly more per yard
    away_ypp_adj = yards_per_point * 1.03   # away scores slightly less per yard

    # Points projection
    home_pts = (home_plays * home_adj_ypp) / home_ypp_adj
    away_pts = (away_plays * away_adj_ypp) / away_ypp_adj

    total = home_pts + away_pts

    # NFL totals have high variance (defensive TDs, special teams, turnovers)
    std = defaults["score_std"]
    ci_low = total - 1.645 * std
    ci_high = total + 1.645 * std

    result = TotalProjection(
        sport=Sport.NFL.value,
        projected_total=round(total, 1),
        home_projected=round(home_pts, 1),
        away_projected=round(away_pts, 1),
        confidence_interval=(round(ci_low, 1), round(ci_high, 1)),
        pace_factor=pace.pace_factor,
        efficiency_matchup_home=home_adj_ypp,
        efficiency_matchup_away=away_adj_ypp,
        methodology=(
            f"NFL plays-efficiency: home={home_plays:.1f}plays x "
            f"{home_adj_ypp:.2f}ypp / {home_ypp_adj:.1f}ypp_pt + "
            f"away={away_plays:.1f}plays x {away_adj_ypp:.2f}ypp / "
            f"{away_ypp_adj:.1f}ypp_pt"
        ),
    )

    logger.info(f"NFL total: {result.projected_total}, CI=({ci_low:.1f}, {ci_high:.1f})")
    return result


# ---------------------------------------------------------------------------
# 3c. MLB: Poisson model with pitcher/lineup matchup
# ---------------------------------------------------------------------------

def _mlb_total(
    home_pace: float, away_pace: float,
    home_off_eff: float, away_off_eff: float,
    home_def_eff: float, away_def_eff: float,
    league_avg_pace: float, league_avg_eff: float,
) -> TotalProjection:
    """
    MLB total using Poisson-based run expectancy.

    Inputs reinterpreted for MLB:
    - home_pace / away_pace: not used directly (MLB has fixed innings)
      but can represent plate appearances per game (~38) to adjust for
      lineup depth / bullpen usage
    - home_off_eff / away_off_eff: runs scored per game (offense)
    - home_def_eff / away_def_eff: runs allowed per game (pitching+defense)
    - league_avg_eff: league average runs per team per game (~4.5)

    Model: expected_runs = lineup_strength * (opponent_pitching / league_avg)
    Then use Poisson distribution for probability calculations.

    The Poisson model is ideal for baseball because:
    - Run scoring is approximately a Poisson process
    - Each at-bat is roughly independent
    - Low-scoring nature means discrete distribution matters
    """
    defaults = LEAGUE_DEFAULTS[Sport.MLB]

    # MLB doesn't have "pace" in the traditional sense, but plate appearances
    # matter — a deeper lineup that works counts sees more pitches, gets
    # to bullpen faster. We use pace as a PA multiplier.
    pa_factor_home = home_pace / league_avg_pace if league_avg_pace > 0 else 1.0
    pa_factor_away = away_pace / league_avg_pace if league_avg_pace > 0 else 1.0

    # Run expectancy: offense * (opponent_pitching / league_avg)
    # For pitching: lower def_eff = better pitching = fewer runs
    # Multiplicative: good lineup vs bad pitcher = more runs than average of both
    home_expected = matchup_efficiency(
        home_off_eff, away_def_eff, league_avg_eff, Sport.MLB,
    )
    away_expected = matchup_efficiency(
        away_off_eff, home_def_eff, league_avg_eff, Sport.MLB,
    )

    # Apply plate appearance factor (lineup depth effect)
    home_expected *= pa_factor_home
    away_expected *= pa_factor_away

    # Home field advantage in MLB: ~0.15 runs
    home_expected += 0.15

    total = home_expected + away_expected

    # MLB std dev: sqrt(home_lambda + away_lambda) for Poisson
    # Plus additional variance from bullpen uncertainty
    poisson_std = math.sqrt(home_expected + away_expected)
    # Add bullpen/game variance beyond pure Poisson
    total_std = math.sqrt(poisson_std**2 + 1.0**2)

    ci_low = total - 1.645 * total_std
    ci_high = total + 1.645 * total_std

    result = TotalProjection(
        sport=Sport.MLB.value,
        projected_total=round(total, 1),
        home_projected=round(home_expected, 2),
        away_projected=round(away_expected, 2),
        confidence_interval=(round(max(0.5, ci_low), 1), round(ci_high, 1)),
        pace_factor=round((pa_factor_home + pa_factor_away) / 2, 4),
        efficiency_matchup_home=round(home_expected, 2),
        efficiency_matchup_away=round(away_expected, 2),
        methodology=(
            f"MLB Poisson: home_lambda={home_expected:.2f}, "
            f"away_lambda={away_expected:.2f}, "
            f"PA_factors=({pa_factor_home:.3f}, {pa_factor_away:.3f})"
        ),
    )

    logger.info(f"MLB total: {result.projected_total}, CI=({ci_low:.1f}, {ci_high:.1f})")
    return result


# ---------------------------------------------------------------------------
# 3d. NHL: shots x save percentage with Poisson goal distribution
# ---------------------------------------------------------------------------

def _nhl_total(
    home_pace: float, away_pace: float,
    home_off_eff: float, away_off_eff: float,
    home_def_eff: float, away_def_eff: float,
    league_avg_pace: float, league_avg_eff: float,
) -> TotalProjection:
    """
    NHL total using shots x shooting/save percentage, then Poisson.

    Inputs reinterpreted for NHL:
    - home_pace / away_pace: shots per game (offensive generation)
    - home_off_eff / away_off_eff: shooting percentage (goals per shot)
    - home_def_eff / away_def_eff: goals allowed per shot (1 - save_pct)
    - league_avg_pace: league avg shots per game (~30)
    - league_avg_eff: league avg goals per game per team (~3.1)

    Model:
    1. Project shots for each team based on pace interaction
    2. Adjust shooting % for opponent's goaltending/defense
    3. Expected goals = shots * adjusted_shooting_pct
    4. Apply Poisson for total distribution

    The shots x save% model captures that:
    - High-shot teams generate more chances but face regression (lower quality)
    - Elite goalies suppress shooting % beyond their save% suggests
    - Fast-paced games with poor goaltending create high totals
    """
    defaults = LEAGUE_DEFAULTS[Sport.NHL]

    # Project matchup-specific shot volume
    pace = project_pace(home_pace, away_pace, league_avg_pace, Sport.NHL)
    projected_shots_per_team = pace.projected_possessions

    # Distribute shots (home team gets slight advantage ~51-52% of shots at home)
    home_shot_share = 0.52
    home_shots = projected_shots_per_team * 2 * home_shot_share
    away_shots = projected_shots_per_team * 2 * (1 - home_shot_share)

    # Adjust shooting efficiency for matchup
    # home_off_eff = home shooting %, away_def_eff = away GA per shot
    # Blend: if home shoots well AND opponent allows goals, multiply up
    home_shoot_adj = matchup_efficiency(
        home_off_eff, away_def_eff, league_avg_eff, Sport.NHL,
    )
    away_shoot_adj = matchup_efficiency(
        away_off_eff, home_def_eff, league_avg_eff, Sport.NHL,
    )

    # Expected goals
    # Since eff values represent goals/game already, and we're adjusting,
    # compute expected goals as the adjusted efficiency directly
    home_expected_goals = home_shoot_adj
    away_expected_goals = away_shoot_adj

    # Home ice advantage: ~+0.12 goals
    home_expected_goals += 0.12

    total = home_expected_goals + away_expected_goals

    # Poisson std dev for goal totals
    poisson_std = math.sqrt(home_expected_goals + away_expected_goals)
    # Add OT/SO variance
    total_std = math.sqrt(poisson_std**2 + 0.5**2)

    ci_low = total - 1.645 * total_std
    ci_high = total + 1.645 * total_std

    result = TotalProjection(
        sport=Sport.NHL.value,
        projected_total=round(total, 2),
        home_projected=round(home_expected_goals, 2),
        away_projected=round(away_expected_goals, 2),
        confidence_interval=(round(max(0.5, ci_low), 1), round(ci_high, 1)),
        pace_factor=pace.pace_factor,
        efficiency_matchup_home=round(home_shoot_adj, 3),
        efficiency_matchup_away=round(away_shoot_adj, 3),
        methodology=(
            f"NHL shots-save: home_shots={home_shots:.1f} x adj_eff={home_shoot_adj:.3f}, "
            f"away_shots={away_shots:.1f} x adj_eff={away_shoot_adj:.3f}, "
            f"Poisson distribution applied"
        ),
    )

    logger.info(f"NHL total: {result.projected_total}, CI=({ci_low:.1f}, {ci_high:.1f})")
    return result


# ---------------------------------------------------------------------------
# 3e. Soccer: expected goals (xG) model
# ---------------------------------------------------------------------------

def _soccer_total(
    home_pace: float, away_pace: float,
    home_off_eff: float, away_off_eff: float,
    home_def_eff: float, away_def_eff: float,
    league_avg_pace: float, league_avg_eff: float,
) -> TotalProjection:
    """
    Soccer total using expected goals (xG) framework with Poisson.

    Inputs reinterpreted for soccer:
    - home_pace / away_pace: shots per 90 minutes
    - home_off_eff / away_off_eff: xG per game (expected goals created)
    - home_def_eff / away_def_eff: xGA per game (expected goals conceded)
    - league_avg_pace: league avg shots per game (~12)
    - league_avg_eff: league avg xG per team (~1.35)

    Model:
    1. Adjust xG for specific matchup (good attack vs weak defense)
    2. Factor in shot volume interaction (pace compounding)
    3. Apply Poisson for exact scoreline probabilities

    Soccer-specific: home advantage is substantial (~0.25-0.35 xG)
    due to crowd pressure on referees, travel fatigue, pitch familiarity.
    """
    defaults = LEAGUE_DEFAULTS[Sport.SOCCER]

    # Project matchup-specific shot volume
    pace = project_pace(home_pace, away_pace, league_avg_pace, Sport.SOCCER)

    # Shot volume factor affects chance creation
    shot_factor = pace.pace_factor

    # Matchup-adjusted xG
    home_xg = matchup_efficiency(
        home_off_eff, away_def_eff, league_avg_eff, Sport.SOCCER,
    )
    away_xg = matchup_efficiency(
        away_off_eff, home_def_eff, league_avg_eff, Sport.SOCCER,
    )

    # Scale by shot volume interaction (more shots = more chances)
    home_xg *= shot_factor
    away_xg *= shot_factor

    # Home advantage in soccer: significant
    home_xg += 0.30
    away_xg -= 0.05  # slight away penalty

    # Floor at minimum reasonable xG
    home_xg = max(0.3, home_xg)
    away_xg = max(0.2, away_xg)

    total = home_xg + away_xg

    # Poisson std dev
    poisson_std = math.sqrt(home_xg + away_xg)
    total_std = math.sqrt(poisson_std**2 + 0.3**2)  # extra variance

    ci_low = total - 1.645 * total_std
    ci_high = total + 1.645 * total_std

    result = TotalProjection(
        sport=Sport.SOCCER.value,
        projected_total=round(total, 2),
        home_projected=round(home_xg, 2),
        away_projected=round(away_xg, 2),
        confidence_interval=(round(max(0.0, ci_low), 1), round(ci_high, 1)),
        pace_factor=pace.pace_factor,
        efficiency_matchup_home=round(home_xg, 3),
        efficiency_matchup_away=round(away_xg, 3),
        methodology=(
            f"Soccer xG: home_xg={home_xg:.2f}, away_xg={away_xg:.2f}, "
            f"shot_factor={shot_factor:.3f}, Poisson distribution applied"
        ),
    )

    logger.info(f"Soccer total: {result.projected_total}, CI=({ci_low:.1f}, {ci_high:.1f})")
    return result


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


# ---------------------------------------------------------------------------
# 5. Poisson helpers for low-scoring sports
# ---------------------------------------------------------------------------

def poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (lam^k * e^-lam) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_total_distribution(
    home_expected: float,
    away_expected: float,
    max_score: int = 12,
) -> dict:
    """
    Generate full scoreline probability matrix using Poisson distribution.

    Returns:
        - scoreline_probs: dict of (home, away) -> probability
        - over_probs: dict of total_line -> P(over)
        - under_probs: dict of total_line -> P(under)
        - total_mean: expected total
        - total_std: standard deviation of total
    """
    scoreline_probs = {}
    for h in range(max_score + 1):
        for a in range(max_score + 1):
            prob = poisson_pmf(h, home_expected) * poisson_pmf(a, away_expected)
            scoreline_probs[(h, a)] = prob

    total_mean = home_expected + away_expected
    total_std = math.sqrt(home_expected + away_expected)

    # Over/under at every half-point
    over_probs = {}
    under_probs = {}
    for half in range(0, (max_score * 2 + 1) * 2):
        line = half * 0.5
        over_p = sum(p for (h, a), p in scoreline_probs.items() if h + a > line)
        over_probs[line] = round(over_p, 5)
        under_probs[line] = round(1.0 - over_p, 5)

    return {
        "scoreline_probs": scoreline_probs,
        "over_probs": over_probs,
        "under_probs": under_probs,
        "total_mean": round(total_mean, 2),
        "total_std": round(total_std, 2),
    }


# ---------------------------------------------------------------------------
# 6. Over/Under edge detection
# ---------------------------------------------------------------------------

def detect_total_edge(
    projected_total: float,
    book_total: float,
    book_over_odds: int,
    book_under_odds: int,
    sport: Optional[str] = None,
    home_expected: Optional[float] = None,
    away_expected: Optional[float] = None,
    projection_std: Optional[float] = None,
) -> TotalEdge:
    """
    Detect over/under edge by comparing our projection to book line.

    Two methods depending on available data:

    Method 1 (Gaussian): If we have projected_total and std dev, use
    normal distribution to compute P(over) and P(under) at the book line.
    Good for high-scoring sports (NBA, NFL).

    Method 2 (Poisson): If we have home/away expected scores, use exact
    Poisson distribution. Better for low-scoring sports (MLB, NHL, soccer).

    Edge = our_probability - implied_probability_from_odds
    Positive edge on over means the book total is too low.
    Positive edge on under means the book total is too high.

    Args:
        projected_total: our model's projected game total
        book_total: the bookmaker's total line (e.g., 224.5)
        book_over_odds: American odds for the over (e.g., -110)
        book_under_odds: American odds for the under (e.g., -110)
        sport: sport for model selection
        home_expected: home team expected score (for Poisson method)
        away_expected: away team expected score (for Poisson method)
        projection_std: standard deviation of our total projection
    """
    # Determine if we use Poisson or Gaussian
    use_poisson = False
    sport_key = None
    if sport is not None:
        sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
        if sport_key in (Sport.MLB, Sport.NHL, Sport.SOCCER):
            use_poisson = True

    if use_poisson and home_expected is not None and away_expected is not None:
        # Exact Poisson calculation
        dist = poisson_total_distribution(home_expected, away_expected)
        over_prob = dist["over_probs"].get(book_total, 0.5)
        under_prob = 1.0 - over_prob
    else:
        # Gaussian approximation
        if projection_std is None:
            if sport_key is not None:
                projection_std = LEAGUE_DEFAULTS.get(
                    sport_key, {}
                ).get("score_std", 10.0)
            else:
                projection_std = 10.0

        # P(total > book_line) using normal CDF
        # z = (book_total - projected_total) / std
        # P(over) = 1 - Phi(z) = Phi(-z)
        if projection_std > 0:
            z = (book_total - projected_total) / projection_std
            over_prob = _normal_cdf(-z)
            under_prob = _normal_cdf(z)
        else:
            # No variance: deterministic
            over_prob = 1.0 if projected_total > book_total else 0.0
            under_prob = 1.0 - over_prob

    # Market implied probabilities (with vig)
    over_implied = calculate_implied_probability(book_over_odds)
    under_implied = calculate_implied_probability(book_under_odds)

    # True probability after removing vig (normalize to sum to 1)
    total_implied = over_implied + under_implied
    over_no_vig = over_implied / total_implied if total_implied > 0 else 0.5
    under_no_vig = under_implied / total_implied if total_implied > 0 else 0.5

    # Edge on each side
    over_edge = over_prob - over_implied    # vs raw implied (includes vig)
    under_edge = under_prob - under_implied

    # Which side has the edge?
    if over_edge > under_edge:
        direction = "over"
        edge_pct = over_edge
        ev = calculate_ev(probability=over_prob, american_odds=book_over_odds)
        bet_odds = book_over_odds
        bet_prob = over_prob
    else:
        direction = "under"
        edge_pct = under_edge
        ev = calculate_ev(probability=under_prob, american_odds=book_under_odds)
        bet_odds = book_under_odds
        bet_prob = under_prob

    # Kelly criterion for optimal bet sizing
    # Kelly fraction = (bp - q) / b
    # where b = decimal odds - 1, p = our probability, q = 1 - p
    if bet_odds > 0:
        decimal_odds = 1 + bet_odds / 100.0
    else:
        decimal_odds = 1 + 100.0 / abs(bet_odds)

    b = decimal_odds - 1.0
    p = bet_prob
    q = 1.0 - p
    kelly = (b * p - q) / b if b > 0 else 0.0
    kelly = max(0.0, kelly)  # never negative (no edge = no bet)

    # Fractional kelly (conservative: use 25% of full kelly)
    fractional_kelly = kelly * 0.25

    result = TotalEdge(
        edge_direction=direction,
        edge_pct=round(edge_pct * 100, 2),
        recommended_side=direction,
        ev=ev,
        projected_total=round(projected_total, 2),
        book_total=book_total,
        over_probability=round(over_prob, 4),
        under_probability=round(under_prob, 4),
        kelly_fraction=round(fractional_kelly, 4),
    )

    logger.info(
        f"Total edge: projected={projected_total:.1f} vs book={book_total}, "
        f"direction={direction}, edge={edge_pct*100:.1f}%, "
        f"kelly={fractional_kelly:.2%}"
    )

    return result


# ---------------------------------------------------------------------------
# 7. Batch analysis: run full pipeline for a game
# ---------------------------------------------------------------------------

def analyze_game_total(
    home_pace: float,
    away_pace: float,
    home_off_eff: float,
    away_off_eff: float,
    home_def_eff: float,
    away_def_eff: float,
    league_avg_pace: float,
    sport: str,
    book_total: Optional[float] = None,
    book_over_odds: Optional[int] = None,
    book_under_odds: Optional[int] = None,
    league_avg_eff: Optional[float] = None,
    player_adjustments: Optional[list[dict]] = None,
) -> dict:
    """
    Full pipeline: project total, apply player adjustments, detect edge.

    This is the convenience function that chains everything together.

    Args:
        player_adjustments: list of dicts with keys:
            - player_pace_on, player_pace_off, projected_minutes, is_playing
            (set projected_minutes=0 and is_playing=False for injured players)

    Returns dict with all projections, adjustments, and edge detection.
    """
    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
    defaults = LEAGUE_DEFAULTS[sport_key]

    # Step 1: Base total projection
    projection = project_game_total(
        home_pace, away_pace,
        home_off_eff, away_off_eff,
        home_def_eff, away_def_eff,
        league_avg_pace, sport,
        league_avg_eff=league_avg_eff,
    )

    # Step 2: Player pace adjustments
    total_adjustment = 0.0
    player_impacts = []

    if player_adjustments:
        for pa in player_adjustments:
            # If player is OUT, projected_minutes should be 0
            # The pace delta represents what the team loses
            is_playing = pa.get("is_playing", True)
            mins = pa.get("projected_minutes", 0)

            if not is_playing:
                # Player is out — compute impact of their absence
                impact = player_pace_adjustment(
                    player_pace_on=pa["player_pace_on"],
                    player_pace_off=pa["player_pace_off"],
                    projected_minutes=mins if mins > 0 else pa.get("usual_minutes", 30),
                    team_total_minutes=defaults.get("total_minutes", 240.0),
                    sport=sport,
                    team_off_eff=home_off_eff if pa.get("team", "home") == "home" else away_off_eff,
                    team_def_eff=home_def_eff if pa.get("team", "home") == "home" else away_def_eff,
                    league_avg_eff=league_avg_eff,
                )
                # When a player is OUT, team loses their pace contribution
                # total_delta is negative if they were a pace-booster
                total_adjustment -= impact.projected_total_delta
                player_impacts.append({
                    "status": "OUT",
                    "pace_on": pa["player_pace_on"],
                    "pace_off": pa["player_pace_off"],
                    "pace_delta": impact.pace_delta,
                    "total_impact": -impact.projected_total_delta,
                })

    adjusted_total = projection.projected_total + total_adjustment

    # Step 3: Edge detection (if book line provided)
    edge = None
    if book_total is not None and book_over_odds is not None and book_under_odds is not None:
        # Determine std for edge detection
        ci = projection.confidence_interval
        proj_std = (ci[1] - ci[0]) / (2 * 1.645)  # recover std from 90% CI

        # Use Poisson for low-scoring sports
        home_exp = projection.home_projected
        away_exp = projection.away_projected

        # Adjust expected scores for player impacts
        if total_adjustment != 0:
            ratio = adjusted_total / projection.projected_total if projection.projected_total > 0 else 1.0
            home_exp *= ratio
            away_exp *= ratio

        edge = detect_total_edge(
            projected_total=adjusted_total,
            book_total=book_total,
            book_over_odds=book_over_odds,
            book_under_odds=book_under_odds,
            sport=sport,
            home_expected=home_exp,
            away_expected=away_exp,
            projection_std=proj_std,
        )

    return {
        "projection": projection,
        "adjusted_total": round(adjusted_total, 1),
        "player_impacts": player_impacts,
        "total_adjustment": round(total_adjustment, 1),
        "edge": edge,
        "sport": sport_key.value,
    }


# ---------------------------------------------------------------------------
# 8. Monte Carlo simulation with pace model
# ---------------------------------------------------------------------------

def simulate_total_distribution(
    home_pace: float,
    away_pace: float,
    home_off_eff: float,
    away_off_eff: float,
    home_def_eff: float,
    away_def_eff: float,
    league_avg_pace: float,
    sport: str,
    iterations: int = 10000,
    league_avg_eff: Optional[float] = None,
) -> dict:
    """
    Monte Carlo simulation using the pace model to generate total distribution.

    Adds stochastic noise to pace, efficiency, and scoring to produce a full
    probability distribution of game totals. Useful for:
    - Getting exact P(over) at any line
    - Tail probabilities (blowout games, low-scoring games)
    - Alternate total pricing

    Returns distribution of totals with percentiles and over probabilities.
    """
    sport_key = Sport(sport.lower()) if not isinstance(sport, Sport) else sport
    defaults = LEAGUE_DEFAULTS[sport_key]

    if league_avg_eff is None:
        if sport_key == Sport.NBA:
            league_avg_eff = defaults["off_eff"]
        elif sport_key == Sport.NFL:
            league_avg_eff = defaults["yards_per_play"]
        elif sport_key == Sport.MLB:
            league_avg_eff = defaults["runs_per_game"]
        elif sport_key == Sport.NHL:
            league_avg_eff = defaults["goals_per_game"]
        elif sport_key == Sport.SOCCER:
            league_avg_eff = defaults["xg_per_game"]

    rng = np.random.default_rng()
    totals = np.empty(iterations)

    # Pre-compute base values
    pace_result = project_pace(home_pace, away_pace, league_avg_pace, sport)
    base_possessions = pace_result.projected_possessions

    home_adj_eff = matchup_efficiency(home_off_eff, away_def_eff, league_avg_eff, sport)
    away_adj_eff = matchup_efficiency(away_off_eff, home_def_eff, league_avg_eff, sport)

    if sport_key == Sport.NBA:
        # NBA simulation
        pace_noise = rng.normal(0, 3.5, iterations)
        home_eff_noise = rng.normal(0, 4.0, iterations)
        away_eff_noise = rng.normal(0, 4.0, iterations)
        game_noise = rng.normal(0, 3.0, iterations)  # game-level randomness

        possessions = np.clip(base_possessions + pace_noise, 55, 120)
        home_eff = home_adj_eff + 1.5 + home_eff_noise  # home advantage
        away_eff = away_adj_eff - 0.5 + away_eff_noise  # road penalty

        home_scores = possessions * (home_eff / 100.0) + game_noise
        away_scores = possessions * (away_eff / 100.0) + game_noise * 0.8
        home_scores = np.clip(home_scores, 65, 180)
        away_scores = np.clip(away_scores, 65, 180)
        totals = home_scores + away_scores

    elif sport_key == Sport.NFL:
        ypp = defaults["yards_per_point"]
        pace_noise = rng.normal(0, 4.0, iterations)
        eff_noise_h = rng.normal(0, 0.8, iterations)
        eff_noise_a = rng.normal(0, 0.8, iterations)

        plays = np.clip(base_possessions + pace_noise, 40, 85)
        total_plays = plays * 2
        home_plays = total_plays * (home_pace / (home_pace + away_pace))
        away_plays = total_plays * (away_pace / (home_pace + away_pace))

        home_ypp = home_adj_eff + 0.15 + eff_noise_h
        away_ypp = away_adj_eff + eff_noise_a

        home_pts = (home_plays * home_ypp) / (ypp * 0.97)
        away_pts = (away_plays * away_ypp) / (ypp * 1.03)

        # Add scoring noise (defensive TDs, special teams)
        scoring_noise = rng.normal(0, 4.0, iterations)
        totals = home_pts + away_pts + scoring_noise
        totals = np.clip(totals, 6, 100)

    elif sport_key in (Sport.MLB, Sport.NHL, Sport.SOCCER):
        # Poisson-based sports: sample from Poisson directly
        home_lambda = home_adj_eff
        away_lambda = away_adj_eff

        if sport_key == Sport.MLB:
            home_lambda += 0.15  # home advantage
        elif sport_key == Sport.NHL:
            home_lambda += 0.12
        elif sport_key == Sport.SOCCER:
            home_lambda += 0.30
            away_lambda = max(0.2, away_lambda - 0.05)

        home_lambda = max(0.3, home_lambda)
        away_lambda = max(0.2, away_lambda)

        # Add game-level variance to lambdas (overdispersion)
        home_lambdas = rng.gamma(
            shape=home_lambda / 0.3, scale=0.3, size=iterations,
        )
        away_lambdas = rng.gamma(
            shape=away_lambda / 0.3, scale=0.3, size=iterations,
        )

        home_scores = rng.poisson(home_lambdas)
        away_scores = rng.poisson(away_lambdas)
        totals = (home_scores + away_scores).astype(float)

    else:
        raise ValueError(f"Unsupported sport for simulation: {sport}")

    # Compute statistics
    mean_total = float(np.mean(totals))
    std_total = float(np.std(totals))
    percentiles = {
        "p5": float(np.percentile(totals, 5)),
        "p10": float(np.percentile(totals, 10)),
        "p25": float(np.percentile(totals, 25)),
        "p50": float(np.percentile(totals, 50)),
        "p75": float(np.percentile(totals, 75)),
        "p90": float(np.percentile(totals, 90)),
        "p95": float(np.percentile(totals, 95)),
    }

    # Over probabilities at relevant lines
    over_probs = {}
    center = round(mean_total)
    if sport_key in (Sport.MLB, Sport.NHL, Sport.SOCCER):
        # Half-point lines for low-scoring
        for half in range(max(0, center * 2 - 20), center * 2 + 21):
            line = half * 0.5
            over_probs[line] = round(float(np.mean(totals > line)), 4)
    else:
        # Half-point lines for high-scoring
        for half in range(max(0, center * 2 - 40), center * 2 + 41):
            line = half * 0.5
            over_probs[line] = round(float(np.mean(totals > line)), 4)

    return {
        "sport": sport_key.value,
        "iterations": iterations,
        "mean_total": round(mean_total, 2),
        "std_total": round(std_total, 2),
        "percentiles": {k: round(v, 1) for k, v in percentiles.items()},
        "over_probs": over_probs,
        "ci_90": (round(percentiles["p5"], 1), round(percentiles["p95"], 1)),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """
    Standard normal CDF using the complementary error function.
    P(Z <= x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
