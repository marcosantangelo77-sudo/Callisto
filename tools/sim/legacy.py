"""Legacy backward-compatible simulation functions."""

import logging
from collections import Counter

import numpy as np

from tools.sim.constants import DEFAULT_ITERATIONS, SPORT_DEFAULTS
from tools.sim.game import simulate_game
from tools.sim.models import SimulationResult, TeamProfile
from tools.sim.game import _poisson_pmf

logger = logging.getLogger("callisto.simulation")


def simulate_basketball(
    home: TeamProfile,
    away: TeamProfile,
    is_home: bool = True,
    iterations: int = DEFAULT_ITERATIONS,
) -> SimulationResult:
    """
    Monte Carlo simulation for basketball using pace x efficiency model.

    This is more accurate than averaging team totals because it accounts for
    the MATCHUP-SPECIFIC pace. A fast team vs fast team will have more possessions
    than either team's average suggests.

    Pace adjustment: game_pace = (home.pace + away.pace) / 2
    (Each team contributes equally to pace in basketball)

    Per-possession scoring:
    - Home offense vs away defense: home.off_eff vs away.def_eff
    - Away offense vs home defense: away.off_eff vs home.def_eff
    """
    rng = np.random.default_rng()

    result = SimulationResult(
        home_team=home.name,
        away_team=away.name,
        iterations=iterations,
        sport="basketball",
    )

    home_scores = np.empty(iterations, dtype=int)
    away_scores = np.empty(iterations, dtype=int)

    # Vectorised: generate all random components up front
    pace_noise = rng.normal(0, 2.0, size=iterations)
    poss_noise = rng.normal(0, 1.5, size=iterations)
    home_var_noise = rng.normal(0, 4.0 * home.variance, size=iterations)
    away_var_noise = rng.normal(0, 4.0 * away.variance, size=iterations)

    base_pace = (home.pace + away.pace) / 2.0

    # Matchup-specific efficiencies
    home_eff = (home.offensive_efficiency + (200 - away.defensive_efficiency)) / 2.0
    away_eff = (away.offensive_efficiency + (200 - home.defensive_efficiency)) / 2.0

    # Apply adjustments
    if is_home:
        home_eff += home.home_advantage
    home_eff += home.injuries_impact
    away_eff += away.injuries_impact
    if home.back_to_back:
        home_eff -= 1.5
    if away.back_to_back:
        away_eff -= 1.5

    for i in range(iterations):
        game_pace = max(55, base_pace + pace_noise[i])
        possessions = game_pace + poss_noise[i]

        h_score = possessions * (home_eff / 100.0) + home_var_noise[i]
        a_score = possessions * (away_eff / 100.0) + away_var_noise[i]

        home_scores[i] = max(45, round(h_score))
        away_scores[i] = max(45, round(a_score))

    # Calculate results
    result.home_avg_score = float(np.mean(home_scores))
    result.away_avg_score = float(np.mean(away_scores))
    result.home_score_std = float(np.std(home_scores, ddof=1))
    result.away_score_std = float(np.std(away_scores, ddof=1))
    result.home_scores = home_scores.tolist()
    result.away_scores = away_scores.tolist()

    margins = home_scores.astype(int) - away_scores.astype(int)
    result.fair_spread = float(np.mean(margins))

    home_wins = int(np.sum(margins > 0))
    away_wins = int(np.sum(margins < 0))
    result.home_win_pct = round(home_wins / iterations, 4)
    result.away_win_pct = round(away_wins / iterations, 4)

    totals = home_scores.astype(int) + away_scores.astype(int)
    result.fair_total = float(np.mean(totals))

    # Spread cover probabilities at common lines
    for spread in [x * 0.5 for x in range(-20, 21)]:
        covers = int(np.sum(margins > spread))
        result.spread_cover_probs[spread] = round(covers / iterations, 4)

    # Over probabilities at common totals
    fair_total_int = round(result.fair_total)
    for total in range(fair_total_int - 15, fair_total_int + 16):
        overs = int(np.sum(totals > total))
        result.over_probs[total] = round(overs / iterations, 4)

    # Distribution buckets
    margin_counter = Counter(int(m) for m in margins)
    total_counter = Counter(int(t) for t in totals)
    result.spread_distribution = dict(sorted(margin_counter.items()))
    result.total_distribution = dict(sorted(total_counter.items()))

    logger.info(
        f"Simulation: {away.name} @ {home.name} | "
        f"fair spread={result.fair_spread:.1f}, fair total={result.fair_total:.1f}, "
        f"home win={result.home_win_pct:.1%}"
    )

    return result


def simulate_poisson(
    home_expected_goals: float,
    away_expected_goals: float,
    max_goals: int = 10,
) -> dict:
    """
    Poisson simulation for low-scoring sports (soccer, hockey, baseball).

    Goals/runs follow a Poisson distribution. This gives exact probabilities
    for every possible scoreline, which we can aggregate into:
    - Win/draw/loss probabilities
    - Over/under at any line
    - Exact score probabilities (for correct score props)
    - Asian handicap probabilities

    Much more precise than Monte Carlo for low-scoring sports because
    the Poisson distribution is the correct statistical model.
    """
    scorelines = {}
    home_win_prob = 0.0
    away_win_prob = 0.0
    draw_prob = 0.0

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            prob = _poisson_pmf(h, home_expected_goals) * _poisson_pmf(a, away_expected_goals)
            scorelines[(h, a)] = prob

            if h > a:
                home_win_prob += prob
            elif a > h:
                away_win_prob += prob
            else:
                draw_prob += prob

    # Over/under probabilities
    over_probs = {}
    total_range = home_expected_goals + away_expected_goals
    for total_line in [x * 0.5 for x in range(max(0, int(total_range * 2) - 10), int(total_range * 2) + 11)]:
        over_prob = sum(
            prob for (h, a), prob in scorelines.items()
            if h + a > total_line
        )
        over_probs[total_line] = round(over_prob, 4)

    # Spread probabilities (Asian handicap)
    spread_probs = {}
    for spread in [x * 0.5 for x in range(-6, 7)]:
        cover_prob = sum(
            prob for (h, a), prob in scorelines.items()
            if (h - a) > spread
        )
        spread_probs[spread] = round(cover_prob, 4)

    # Most likely scorelines
    sorted_scores = sorted(scorelines.items(), key=lambda x: x[1], reverse=True)
    top_scores = [
        {"score": f"{h}-{a}", "probability": round(p, 4)}
        for (h, a), p in sorted_scores[:10]
    ]

    return {
        "home_expected": home_expected_goals,
        "away_expected": away_expected_goals,
        "home_win": round(home_win_prob, 4),
        "draw": round(draw_prob, 4),
        "away_win": round(away_win_prob, 4),
        "over_probs": over_probs,
        "spread_probs": spread_probs,
        "top_scorelines": top_scores,
        "fair_total": round(home_expected_goals + away_expected_goals, 2),
    }
