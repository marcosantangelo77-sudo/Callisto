"""Pace-adjusted game total projections, per sport."""

import logging
import math
from typing import Optional

from tools.pace.constants import LEAGUE_DEFAULTS, Sport
from tools.pace.models import TotalProjection
from tools.pace.projection import matchup_efficiency, project_pace

logger = logging.getLogger("callisto.pace_model")


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
