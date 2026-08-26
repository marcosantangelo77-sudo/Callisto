"""Core Monte Carlo game simulation (high-scoring + low-scoring models)."""

import logging
import math
from collections import Counter

import numpy as np

from tools.sim.constants import DEFAULT_ITERATIONS, SPORT_DEFAULTS, classify_sport
from tools.sim.models import SimulationResult
from tools.sim.edge import make_edge_result  # noqa: F401  (re-exported)

logger = logging.getLogger("callisto.simulation")


def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (lam^k * e^-lam) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def simulate_game(
    home_power: float,
    away_power: float,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_advantage: float = None,
) -> SimulationResult:
    """
    Universal game simulator. Routes to the correct distribution model
    based on sport type.

    Args:
        home_power: Home team power rating. For high-scoring sports, this is
                    the expected team score. For low-scoring, expected goals/runs.
        away_power: Away team power rating (same units).
        sport: Sport key (e.g. 'basketball_nba', 'soccer_epl', 'icehockey_nhl').
        n_sims: Number of Monte Carlo iterations.
        home_advantage: Override for home advantage adjustment. If None, uses
                        sport defaults.

    Returns:
        SimulationResult with full probability distributions.
    """
    rng = np.random.default_rng()
    classification = classify_sport(sport)
    defaults = SPORT_DEFAULTS.get(sport, SPORT_DEFAULTS.get(classification.replace("_scoring", ""), {}))

    if classification == "low_scoring":
        return _simulate_low_scoring(
            home_lambda=home_power,
            away_lambda=away_power,
            sport=sport,
            n_sims=n_sims,
            rng=rng,
        )
    else:
        ha = home_advantage if home_advantage is not None else defaults.get("home_adv", 3.0)
        team_std = defaults.get("team_std", 12.0)
        return _simulate_high_scoring(
            home_mean=home_power,
            away_mean=away_power,
            sport=sport,
            n_sims=n_sims,
            home_adv=ha,
            team_std=team_std,
            rng=rng,
        )


def _simulate_high_scoring(
    home_mean: float,
    away_mean: float,
    sport: str,
    n_sims: int,
    home_adv: float,
    team_std: float,
    rng: np.random.Generator,
) -> SimulationResult:
    """Normal-distribution simulation for basketball and football."""
    # Apply home advantage
    adj_home_mean = home_mean + home_adv / 2.0
    adj_away_mean = away_mean - home_adv / 2.0

    # Generate scores using normal distribution, floor at sport minimum
    is_football = "football" in sport.lower()
    floor = 0 if is_football else 45

    home_raw = rng.normal(loc=adj_home_mean, scale=team_std, size=n_sims)
    away_raw = rng.normal(loc=adj_away_mean, scale=team_std, size=n_sims)

    # Round and floor
    home_scores = np.maximum(floor, np.round(home_raw)).astype(int)
    away_scores = np.maximum(floor, np.round(away_raw)).astype(int)

    return _build_result(home_scores, away_scores, "Home", "Away", n_sims, sport)


def _simulate_low_scoring(
    home_lambda: float,
    away_lambda: float,
    sport: str,
    n_sims: int,
    rng: np.random.Generator,
) -> SimulationResult:
    """Poisson-distribution simulation for soccer, hockey, baseball."""
    home_lambda = max(0.01, home_lambda)
    away_lambda = max(0.01, away_lambda)

    home_scores = rng.poisson(lam=home_lambda, size=n_sims)
    away_scores = rng.poisson(lam=away_lambda, size=n_sims)

    result = _build_result(home_scores, away_scores, "Home", "Away", n_sims, sport)

    # For low-scoring sports, also compute exact-score probabilities analytically
    max_goals = 10 if "soccer" in sport else 15
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            prob = _poisson_pmf(h, home_lambda) * _poisson_pmf(a, away_lambda)
            if prob >= 0.001:
                result.exact_score_probs[f"{h}-{a}"] = round(prob, 4)

    return result


def _build_result(
    home_scores: np.ndarray,
    away_scores: np.ndarray,
    home_name: str,
    away_name: str,
    n_sims: int,
    sport: str,
) -> SimulationResult:
    """Build a SimulationResult from raw score arrays."""
    margins = home_scores - away_scores
    totals = home_scores + away_scores

    home_wins = int(np.sum(margins > 0))
    away_wins = int(np.sum(margins < 0))
    draws = int(np.sum(margins == 0))

    result = SimulationResult(
        home_team=home_name,
        away_team=away_name,
        iterations=n_sims,
        sport=sport,
        home_avg_score=float(np.mean(home_scores)),
        away_avg_score=float(np.mean(away_scores)),
        home_score_std=float(np.std(home_scores, ddof=1)),
        away_score_std=float(np.std(away_scores, ddof=1)),
        fair_spread=float(np.mean(margins)),
        fair_total=float(np.mean(totals)),
        home_win_pct=round(home_wins / n_sims, 4),
        away_win_pct=round(away_wins / n_sims, 4),
        draw_pct=round(draws / n_sims, 4),
        home_scores=home_scores.tolist(),
        away_scores=away_scores.tolist(),
    )

    # Spread cover probabilities
    is_low = classify_sport(sport) == "low_scoring"
    if is_low:
        spread_range = [x * 0.5 for x in range(-10, 11)]
    else:
        spread_range = [x * 0.5 for x in range(-40, 41)]

    for spread in spread_range:
        covers = int(np.sum(margins > spread))
        result.spread_cover_probs[spread] = round(covers / n_sims, 4)

    # Over probabilities
    fair_total_int = round(float(np.mean(totals)))
    if is_low:
        total_range_vals = [x * 0.5 for x in range(max(0, int(fair_total_int * 2) - 20), int(fair_total_int * 2) + 21)]
    else:
        total_range_vals = range(fair_total_int - 20, fair_total_int + 21)

    for total_line in total_range_vals:
        overs = int(np.sum(totals > total_line))
        result.over_probs[total_line] = round(overs / n_sims, 4)

    # Distribution histograms
    margin_counter = Counter(int(m) for m in margins)
    total_counter = Counter(int(t) for t in totals)
    result.spread_distribution = dict(sorted(margin_counter.items()))
    result.total_distribution = dict(sorted(total_counter.items()))

    return result


def _std_dev(values: list) -> float:
    """Calculate standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return variance ** 0.5
