"""Over/under edge detection, full pipeline analysis, Monte Carlo simulation."""

import logging
import math
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools.pace.constants import LEAGUE_DEFAULTS, Sport
from tools.pace.distributions import (
    _normal_cdf,
    poisson_total_distribution,
)
from tools.pace.models import TotalEdge
from tools.pace.players import player_pace_adjustment
from tools.pace.projection import matchup_efficiency, project_pace
from tools.pace.totals import project_game_total

logger = logging.getLogger("callisto.pace_model")


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
