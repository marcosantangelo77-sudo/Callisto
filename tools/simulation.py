"""
Monte Carlo simulation engine — generate our own probability distributions.

Without our own model, we're just comparing books to each other.
With a simulation engine, we know which SIDE of the divergence is correct.

Sport-specific models:
  Basketball/Football (high-scoring): Normal distribution with pace x efficiency.
  Soccer/Hockey/Baseball (low-scoring): Poisson distribution for goal/run processes.

Player props: Usage/pace/minutes-based simulation with matchup and pace factors.

The output is a "fair line" — our model's implied spread/total.
Compare fair line vs book line -> edge = the difference.
"""

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools.edge_confidence import score_edge

logger = logging.getLogger("callisto.simulation")

# Default iterations — balance accuracy vs speed
DEFAULT_ITERATIONS = 10000

# Sport classification
HIGH_SCORING_SPORTS = {
    "basketball", "football",
    "basketball_nba", "basketball_ncaab", "basketball_euroleague",
    "americanfootball_nfl", "americanfootball_ncaaf",
}
LOW_SCORING_SPORTS = {
    "soccer", "hockey", "baseball",
    "soccer_epl", "soccer_germany_bundesliga", "soccer_spain_la_liga",
    "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_usa_mls",
    "icehockey_nhl", "baseball_mlb",
}

# Sport-specific scoring parameters (mean total, std dev of team score)
SPORT_DEFAULTS = {
    "basketball":      {"mean_total": 220, "team_std": 12.0, "home_adv": 3.0},
    "basketball_nba":  {"mean_total": 224, "team_std": 12.0, "home_adv": 3.0},
    "basketball_ncaab":{"mean_total": 140, "team_std": 10.0, "home_adv": 3.5},
    "football":        {"mean_total": 44,  "team_std": 10.0, "home_adv": 2.5},
    "americanfootball_nfl":  {"mean_total": 44, "team_std": 10.0, "home_adv": 2.5},
    "americanfootball_ncaaf":{"mean_total": 50, "team_std": 12.0, "home_adv": 3.0},
    "soccer":          {"mean_total": 2.6, "home_lambda": 1.45, "away_lambda": 1.15},
    "soccer_epl":      {"mean_total": 2.7, "home_lambda": 1.50, "away_lambda": 1.20},
    "hockey":          {"mean_total": 5.8, "home_lambda": 3.05, "away_lambda": 2.75},
    "icehockey_nhl":   {"mean_total": 6.1, "home_lambda": 3.20, "away_lambda": 2.90},
    "baseball":        {"mean_total": 8.6, "home_lambda": 4.50, "away_lambda": 4.10},
    "baseball_mlb":    {"mean_total": 8.6, "home_lambda": 4.50, "away_lambda": 4.10},
}


def _classify_sport(sport: str) -> str:
    """Classify a sport key into 'high_scoring' or 'low_scoring'."""
    s = sport.lower().strip()
    if s in LOW_SCORING_SPORTS or any(s.startswith(p) for p in ("soccer", "ice", "baseball")):
        return "low_scoring"
    return "high_scoring"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TeamProfile:
    """Team statistical profile for simulation input."""
    name: str
    # Offensive/defensive efficiency (points per 100 possessions for basketball)
    offensive_efficiency: float = 100.0
    defensive_efficiency: float = 100.0
    # Pace (possessions per game)
    pace: float = 70.0
    # Variance factor (higher = more volatile outcomes)
    variance: float = 1.0
    # Home court advantage (points added when home)
    home_advantage: float = 3.0
    # Adjustments
    injuries_impact: float = 0.0  # negative = weakened
    rest_days: int = 1
    back_to_back: bool = False


@dataclass
class SimulationResult:
    """Results from a Monte Carlo simulation."""
    home_team: str
    away_team: str
    iterations: int
    sport: str = "basketball"
    # Score distributions
    home_avg_score: float = 0.0
    away_avg_score: float = 0.0
    home_score_std: float = 0.0
    away_score_std: float = 0.0
    # Spread analysis
    fair_spread: float = 0.0  # Positive = home favored
    spread_distribution: dict = field(default_factory=dict)
    # Total analysis
    fair_total: float = 0.0
    total_distribution: dict = field(default_factory=dict)
    # Win probabilities
    home_win_pct: float = 0.0
    away_win_pct: float = 0.0
    draw_pct: float = 0.0
    # Spread cover probabilities at various lines
    spread_cover_probs: dict = field(default_factory=dict)
    # Over/under probabilities at various totals
    over_probs: dict = field(default_factory=dict)
    # Raw score arrays for downstream analysis
    home_scores: list = field(default_factory=list)
    away_scores: list = field(default_factory=list)
    # Exact score probabilities (mainly useful for low-scoring)
    exact_score_probs: dict = field(default_factory=dict)


@dataclass
class PropSimResult:
    """Results from a player prop simulation."""
    player: str
    stat: str
    iterations: int
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0
    values: list = field(default_factory=list)
    percentiles: dict = field(default_factory=dict)
    over_probs: dict = field(default_factory=dict)  # line -> P(over)


@dataclass
class EdgeResult:
    """Comparison of simulated probability vs book line."""
    simulated_prob: float
    book_prob: float
    edge: float
    edge_pct: float
    confidence_interval: tuple  # (lower, upper) 95% CI
    kelly_fraction: float
    kelly_half: float  # Half-Kelly for conservative sizing
    ev_per_100: float
    is_positive_ev: bool
    rating: str  # "STRONG", "MODERATE", "THIN", "NO_EDGE"
    confidence: Optional[object] = None  # EdgeConfidence from score_edge


# ---------------------------------------------------------------------------
# Core: simulate_game (universal entry point)
# ---------------------------------------------------------------------------

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
    classification = _classify_sport(sport)
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
    is_low = _classify_sport(sport) == "low_scoring"
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


# ---------------------------------------------------------------------------
# simulate_spread: targeted spread analysis
# ---------------------------------------------------------------------------

def simulate_spread(
    game_odds: dict,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_power: float = None,
    away_power: float = None,
) -> dict:
    """
    Simulate thousands of outcomes and compare the spread probability
    against what the book is implying.

    Args:
        game_odds: Dict with 'bookmakers' list (Odds API format) plus optional
                   'home_power'/'away_power' overrides.
        sport: Sport key.
        n_sims: Number of simulations.
        home_power: Home team expected score / goals. If None, inferred from
                    the book total and spread.
        away_power: Away team expected score / goals. If None, inferred.

    Returns:
        Dict with simulated_prob, book_prob, edge, confidence_interval for
        every bookmaker spread found.
    """
    # Extract the consensus spread and total from bookmaker data to infer powers
    spreads_found = []
    totals_found = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] == "spreads":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        spreads_found.append(o["point"])
            if mkt["key"] == "totals":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        totals_found.append(o["point"])

    # Derive power ratings from consensus lines if not provided
    if home_power is None or away_power is None:
        consensus_spread = np.median(spreads_found) if spreads_found else 0.0
        consensus_total = np.median(totals_found) if totals_found else (
            SPORT_DEFAULTS.get(sport, {}).get("mean_total", 100)
        )
        # spread = home - away, total = home + away
        # => home = (total + spread) / 2, away = (total - spread) / 2
        inferred_home = (consensus_total + consensus_spread) / 2.0
        inferred_away = (consensus_total - consensus_spread) / 2.0
        home_power = home_power if home_power is not None else inferred_home
        away_power = away_power if away_power is not None else inferred_away

    sim = simulate_game(home_power, away_power, sport=sport, n_sims=n_sims,
                        home_advantage=0.0)

    results = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "spreads":
                continue
            for o in mkt.get("outcomes", []):
                point = o.get("point")
                price = o.get("price", -110)
                name = o.get("name", "")
                if point is None:
                    continue

                # Model: P(home margin > -point) for home side
                # For away side: P(home margin < point)
                is_home_side = (point < 0) or (name == sim.home_team)
                if is_home_side:
                    lookup = -point
                    sim_prob = sim.spread_cover_probs.get(lookup, None)
                    if sim_prob is None:
                        sim_prob = float(np.mean(
                            np.array(sim.home_scores) - np.array(sim.away_scores) > lookup
                        ))
                else:
                    lookup = point
                    cover_prob = sim.spread_cover_probs.get(lookup, None)
                    if cover_prob is None:
                        cover_prob = float(np.mean(
                            np.array(sim.home_scores) - np.array(sim.away_scores) > lookup
                        ))
                    sim_prob = 1.0 - cover_prob

                book_prob = calculate_implied_probability(price)
                edge = sim_prob - book_prob
                edge_result = _make_edge_result(sim_prob, book_prob, price, n_sims)

                results.append({
                    "bookmaker": bm.get("title", bm.get("key", "unknown")),
                    "team": name,
                    "line": point,
                    "price": price,
                    "simulated_prob": round(sim_prob, 4),
                    "book_prob": round(book_prob, 4),
                    "edge": round(edge, 4),
                    "edge_pct": round(edge * 100, 2),
                    "confidence_interval": edge_result.confidence_interval,
                    "kelly": round(edge_result.kelly_fraction, 4),
                    "ev_per_100": round(edge_result.ev_per_100, 2),
                    "rating": edge_result.rating,
                })

    return {
        "sport": sport,
        "n_sims": n_sims,
        "fair_spread": round(sim.fair_spread, 2),
        "fair_total": round(sim.fair_total, 2),
        "edges": sorted(results, key=lambda x: abs(x["edge"]), reverse=True),
    }


# ---------------------------------------------------------------------------
# simulate_total: targeted total analysis
# ---------------------------------------------------------------------------

def simulate_total(
    game_odds: dict,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_power: float = None,
    away_power: float = None,
) -> dict:
    """
    Simulate game totals and compare over/under probabilities to book lines.

    Args:
        game_odds: Dict with 'bookmakers' list (Odds API format).
        sport: Sport key.
        n_sims: Number of simulations.
        home_power: Home team expected score / goals.
        away_power: Away team expected score / goals.

    Returns:
        Dict with simulated over/under probabilities vs book for each line.
    """
    # Extract consensus to infer powers
    spreads_found = []
    totals_found = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] == "spreads":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        spreads_found.append(o["point"])
            if mkt["key"] == "totals":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        totals_found.append(o["point"])

    if home_power is None or away_power is None:
        consensus_spread = np.median(spreads_found) if spreads_found else 0.0
        consensus_total = np.median(totals_found) if totals_found else (
            SPORT_DEFAULTS.get(sport, {}).get("mean_total", 100)
        )
        inferred_home = (consensus_total + consensus_spread) / 2.0
        inferred_away = (consensus_total - consensus_spread) / 2.0
        home_power = home_power if home_power is not None else inferred_home
        away_power = away_power if away_power is not None else inferred_away

    sim = simulate_game(home_power, away_power, sport=sport, n_sims=n_sims,
                        home_advantage=0.0)

    sim_totals = np.array(sim.home_scores) + np.array(sim.away_scores)

    results = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "totals":
                continue
            for o in mkt.get("outcomes", []):
                point = o.get("point")
                price = o.get("price", -110)
                name = o.get("name", "")
                if point is None:
                    continue

                if name == "Over":
                    sim_prob = float(np.mean(sim_totals > point))
                else:
                    sim_prob = float(np.mean(sim_totals < point))

                book_prob = calculate_implied_probability(price)
                edge = sim_prob - book_prob
                edge_result = _make_edge_result(sim_prob, book_prob, price, n_sims)

                results.append({
                    "bookmaker": bm.get("title", bm.get("key", "unknown")),
                    "side": name,
                    "line": point,
                    "price": price,
                    "simulated_prob": round(sim_prob, 4),
                    "book_prob": round(book_prob, 4),
                    "edge": round(edge, 4),
                    "edge_pct": round(edge * 100, 2),
                    "confidence_interval": edge_result.confidence_interval,
                    "kelly": round(edge_result.kelly_fraction, 4),
                    "ev_per_100": round(edge_result.ev_per_100, 2),
                    "rating": edge_result.rating,
                })

    return {
        "sport": sport,
        "n_sims": n_sims,
        "fair_total": round(sim.fair_total, 2),
        "total_std": round(float(np.std(sim_totals, ddof=1)), 2),
        "edges": sorted(results, key=lambda x: abs(x["edge"]), reverse=True),
    }


# ---------------------------------------------------------------------------
# simulate_prop: player prop simulation
# ---------------------------------------------------------------------------

def simulate_prop(
    player_avg: float,
    matchup_factor: float = 1.0,
    pace_factor: float = 1.0,
    minutes: float = 32.0,
    n_sims: int = DEFAULT_ITERATIONS,
    player_name: str = "Unknown",
    stat: str = "points",
    std_ratio: float = 0.35,
) -> PropSimResult:
    """
    Simulate a player prop stat line using usage/pace context.

    The model:
      adjusted_avg = player_avg * matchup_factor * pace_factor * (minutes / baseline_minutes)
      per-game variance modeled as normal with std = adjusted_avg * std_ratio

    For counting stats (rebounds, assists, threes), we use a modified Poisson
    because these are discrete low-count events. For points, normal is fine
    because the central limit theorem holds at NBA scoring rates.

    Args:
        player_avg: Player's season average for this stat.
        matchup_factor: Multiplier for matchup difficulty (>1 = favorable, <1 = tough).
                        Example: 1.15 if opponent allows 15% more of this stat vs league avg.
        pace_factor: Multiplier for game pace context (>1 = faster pace expected).
        minutes: Expected minutes for this game.
        n_sims: Number of simulations.
        player_name: Player name for labeling.
        stat: Stat type (points, rebounds, assists, threes, etc.).
        std_ratio: Standard deviation as a fraction of the mean. Defaults to 0.35.
                   Higher for more volatile stats (threes ~0.50, assists ~0.40).

    Returns:
        PropSimResult with full distribution.
    """
    rng = np.random.default_rng()

    # Baseline minutes assumption (NBA default)
    baseline_minutes = 32.0
    minutes_factor = minutes / baseline_minutes if baseline_minutes > 0 else 1.0

    # Adjusted expected value
    adjusted_avg = player_avg * matchup_factor * pace_factor * minutes_factor
    adjusted_avg = max(0.1, adjusted_avg)

    # Choose distribution based on stat type and magnitude
    is_counting = stat.lower() in {"rebounds", "assists", "threes", "steals", "blocks",
                                    "turnovers", "fouls", "three_pointers"}
    is_low_count = adjusted_avg < 5.0

    if is_counting and is_low_count:
        # Modified Poisson for low-count discrete stats
        # Add overdispersion: sample lambda from gamma, then draw Poisson
        # This is a negative binomial, which handles the extra variance
        # in real player stat distributions
        shape = adjusted_avg / max(std_ratio, 0.1)  # controls overdispersion
        scale = std_ratio
        lambdas = rng.gamma(shape=shape, scale=scale, size=n_sims)
        values = rng.poisson(lam=np.maximum(0.01, lambdas))
    else:
        # Normal distribution for higher-count stats
        std = adjusted_avg * std_ratio
        values = rng.normal(loc=adjusted_avg, scale=max(std, 0.5), size=n_sims)
        values = np.maximum(0, np.round(values)).astype(int)

    values_list = values.tolist()

    # Percentiles
    pcts = {
        5: float(np.percentile(values, 5)),
        10: float(np.percentile(values, 10)),
        25: float(np.percentile(values, 25)),
        50: float(np.percentile(values, 50)),
        75: float(np.percentile(values, 75)),
        90: float(np.percentile(values, 90)),
        95: float(np.percentile(values, 95)),
    }

    # Over probabilities at common lines around the adjusted average
    over_probs = {}
    center = round(adjusted_avg * 2) / 2  # nearest 0.5
    for offset in np.arange(-8, 8.5, 0.5):
        line = center + offset
        if line < 0:
            continue
        over_count = int(np.sum(values > line))
        over_probs[float(line)] = round(over_count / n_sims, 4)

    result = PropSimResult(
        player=player_name,
        stat=stat,
        iterations=n_sims,
        mean=round(float(np.mean(values)), 2),
        median=round(float(np.median(values)), 2),
        std=round(float(np.std(values, ddof=1)), 2),
        values=values_list,
        percentiles=pcts,
        over_probs=over_probs,
    )

    logger.info(
        f"Prop sim: {player_name} {stat} | avg={player_avg}, adj={adjusted_avg:.1f}, "
        f"matchup={matchup_factor:.2f}, pace={pace_factor:.2f}, mins={minutes} | "
        f"sim mean={result.mean}, median={result.median}"
    )

    return result


# ---------------------------------------------------------------------------
# compare_to_book: universal edge comparison
# ---------------------------------------------------------------------------

def compare_to_book(
    simulated_dist: Union[np.ndarray, list],
    book_line: float,
    book_odds: int,
    side: str = "over",
    book_names: list[str] = None,
    market: str = "totals",
) -> EdgeResult:
    """
    Compare a simulated distribution against a book's line and odds to
    quantify the edge.

    Args:
        simulated_dist: Array of simulated values (scores, margins, stat totals).
        book_line: The book's line (e.g., 224.5 for total, -3.5 for spread).
        book_odds: American odds offered by the book (e.g., -110).
        side: 'over' or 'under' for totals, 'cover' or 'fade' for spreads.
        book_names: List of book names for confidence scoring.
        market: Market type for confidence scoring.

    Returns:
        EdgeResult with edge, confidence interval, Kelly sizing.
    """
    values = np.asarray(simulated_dist, dtype=float)
    n = len(values)

    # Calculate simulated probability
    if side.lower() in ("over", "cover"):
        sim_prob = float(np.mean(values > book_line))
    else:
        sim_prob = float(np.mean(values < book_line))

    book_prob = calculate_implied_probability(book_odds)

    return _make_edge_result(
        sim_prob, book_prob, book_odds, n,
        book_names=book_names,
        market=market,
    )


def _make_edge_result(
    sim_prob: float,
    book_prob: float,
    book_odds: int,
    n_sims: int,
    book_names: list[str] = None,
    market: str = "totals",
) -> EdgeResult:
    """Build an EdgeResult with confidence interval and Kelly sizing."""
    edge = sim_prob - book_prob

    # 95% confidence interval using Wilson score interval for proportions
    z = 1.96
    denom = 1 + z * z / n_sims
    center = (sim_prob + z * z / (2 * n_sims)) / denom
    spread = z * math.sqrt((sim_prob * (1 - sim_prob) + z * z / (4 * n_sims)) / n_sims) / denom
    ci_low = max(0.0, round(center - spread, 4))
    ci_high = min(1.0, round(center + spread, 4))
    confidence_interval = (ci_low, ci_high)

    # Edge confidence interval (CI of sim_prob minus book_prob)
    edge_ci_low = ci_low - book_prob
    edge_ci_high = ci_high - book_prob

    # Kelly criterion
    ev_calc = calculate_ev(probability=max(0.001, min(0.999, sim_prob)), american_odds=book_odds)
    kelly = ev_calc["kelly_fraction"]
    kelly_half = round(kelly / 2, 4)

    # EV per $100
    ev_per_100 = ev_calc["expected_value"]

    # Rating
    if edge >= 0.05 and edge_ci_low > 0:
        rating = "STRONG"
    elif edge >= 0.03 and edge_ci_low > -0.01:
        rating = "MODERATE"
    elif edge >= 0.01:
        rating = "THIN"
    else:
        rating = "NO_EDGE"

    # Optional AGP confidence scoring
    confidence = None
    if book_names:
        try:
            confidence = score_edge(
                edge_pct=abs(edge * 100),
                books_compared=len(book_names),
                book_names=book_names,
                market=market,
                cross_method_confirmed=False,
            )
        except Exception as e:
            logger.warning(f"Could not score edge confidence: {e}")

    return EdgeResult(
        simulated_prob=round(sim_prob, 4),
        book_prob=round(book_prob, 4),
        edge=round(edge, 4),
        edge_pct=round(edge * 100, 2),
        confidence_interval=confidence_interval,
        kelly_fraction=round(kelly, 4),
        kelly_half=kelly_half,
        ev_per_100=round(ev_per_100, 2),
        is_positive_ev=ev_per_100 > 0,
        rating=rating,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Legacy: simulate_basketball (backward-compatible with existing API + tests)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Legacy: simulate_poisson (backward-compatible)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Pace-model & environment enhanced simulation
# ---------------------------------------------------------------------------

# Sport key -> pace_model.Sport mapping
_PACE_SPORT_MAP = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "soccer",
    "soccer_germany_bundesliga": "soccer",
    "soccer_spain_la_liga": "soccer",
    "soccer_italy_serie_a": "soccer",
    "soccer_france_ligue_one": "soccer",
    "soccer_usa_mls": "soccer",
}


def simulate_game_with_pace_env(
    home_power: float,
    away_power: float,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_advantage: float = None,
    venue_team: str = None,
    weather_data: dict = None,
    refs: list[str] = None,
    home_pace: float = None,
    away_pace: float = None,
    home_off_eff: float = None,
    away_off_eff: float = None,
    home_def_eff: float = None,
    away_def_eff: float = None,
) -> SimulationResult:
    """
    Enhanced game simulation that integrates pace model projections and
    environment adjustments into the Monte Carlo engine.

    When pace model data is available, it uses pace x efficiency to derive
    more accurate power ratings. When environment data is available (venue,
    weather, refs), it adjusts the simulation parameters accordingly.

    Falls back to the standard simulate_game() when pace/env data is absent.

    Args:
        home_power: Base home team power rating.
        away_power: Base away team power rating.
        sport: Sport key.
        n_sims: Monte Carlo iterations.
        home_advantage: Override home advantage.
        venue_team: Home team abbreviation for venue/environment lookup.
        weather_data: Weather conditions dict.
        refs: Referee names for tendency adjustments.
        home_pace / away_pace: Team pace values for pace model.
        home_off_eff / away_off_eff: Offensive efficiency values.
        home_def_eff / away_def_eff: Defensive efficiency values.

    Returns:
        SimulationResult with environment metadata attached.
    """
    env_adjustment = 0.0
    env_detail = None
    pace_projection = None

    # --- Environment adjustment ---
    if venue_team:
        pace_sport = _PACE_SPORT_MAP.get(sport.lower())
        env_sport_code = (pace_sport or "").upper()
        if env_sport_code:
            try:
                from tools.environment import total_environment_adjustment
                env_result = total_environment_adjustment(
                    venue=venue_team,
                    sport=env_sport_code,
                    weather=weather_data,
                    refs=refs,
                )
                env_adjustment = env_result.get("total_adj", 0.0)
                env_detail = env_result
                logger.info(
                    f"Simulation env adjustment for {venue_team} ({env_sport_code}): "
                    f"{env_adjustment:+.1f} pts"
                )
            except Exception as e:
                logger.debug(f"Environment adjustment failed in simulation: {e}")

    # --- Pace model power rating override ---
    pace_sport = _PACE_SPORT_MAP.get(sport.lower())
    if (pace_sport and home_pace is not None and away_pace is not None
            and home_off_eff is not None and away_off_eff is not None
            and home_def_eff is not None and away_def_eff is not None):
        try:
            from tools.pace_model import project_game_total, LEAGUE_DEFAULTS, Sport
            sport_enum = Sport(pace_sport)
            defaults = LEAGUE_DEFAULTS.get(sport_enum, {})

            if sport_enum == Sport.NBA:
                league_avg_pace = defaults.get("pace", 100.0)
            elif sport_enum == Sport.NFL:
                league_avg_pace = defaults.get("plays_per_game", 64.0)
            elif sport_enum == Sport.MLB:
                league_avg_pace = defaults.get("runs_per_game", 4.5)
            elif sport_enum == Sport.NHL:
                league_avg_pace = defaults.get("shots_per_game", 30.0)
            elif sport_enum == Sport.SOCCER:
                league_avg_pace = defaults.get("shots_per_game", 12.0)
            else:
                league_avg_pace = home_pace  # fallback

            projection = project_game_total(
                home_pace=home_pace,
                away_pace=away_pace,
                home_off_eff=home_off_eff,
                away_off_eff=away_off_eff,
                home_def_eff=home_def_eff,
                away_def_eff=away_def_eff,
                league_avg_pace=league_avg_pace,
                sport=pace_sport,
            )
            pace_projection = projection

            # Override power ratings with pace model projections
            home_power = projection.home_projected
            away_power = projection.away_projected
            # Set home_advantage to 0 since pace model already includes it
            home_advantage = 0.0

            logger.info(
                f"Pace model overriding sim powers: home={home_power:.1f}, "
                f"away={away_power:.1f}, total={projection.projected_total:.1f}"
            )
        except Exception as e:
            logger.debug(f"Pace model projection failed in simulation: {e}")

    # Apply environment adjustment to power ratings (split evenly)
    if env_adjustment != 0:
        classification = _classify_sport(sport)
        if classification == "low_scoring":
            # For low-scoring: split proportionally
            total_base = home_power + away_power
            if total_base > 0:
                home_power += env_adjustment * (home_power / total_base)
                away_power += env_adjustment * (away_power / total_base)
        else:
            # For high-scoring: split evenly
            home_power += env_adjustment / 2.0
            away_power += env_adjustment / 2.0

    # Run the base simulation with adjusted parameters
    sim = simulate_game(
        home_power=home_power,
        away_power=away_power,
        sport=sport,
        n_sims=n_sims,
        home_advantage=home_advantage,
    )

    # Attach pace/env metadata to the result for downstream consumers
    # (Using a simple dict attribute since SimulationResult is a dataclass)
    if not hasattr(sim, '_pace_env_meta'):
        object.__setattr__(sim, '_pace_env_meta', {})
    sim._pace_env_meta = {
        "environment_adjustment": round(env_adjustment, 2),
        "environment_detail": env_detail,
        "pace_projection": {
            "projected_total": pace_projection.projected_total,
            "pace_factor": pace_projection.pace_factor,
            "methodology": pace_projection.methodology,
        } if pace_projection else None,
    }

    return sim


# ---------------------------------------------------------------------------
# Legacy: compare_to_market (backward-compatible)
# ---------------------------------------------------------------------------

def compare_to_market(
    sim_result: SimulationResult,
    market_odds: dict,
) -> list[dict]:
    """
    Compare simulation fair lines against actual market odds.

    This is where the edge lives -- if our model says the fair spread is -5.2
    and the book has -3.5, there's a 1.7-point edge on the favorite.

    Returns a list of identified edges with EV calculations.
    """
    edges = []
    home = sim_result.home_team
    away = sim_result.away_team

    for bm in market_odds.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "unknown"))
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                price = o.get("price", 0)
                point = o.get("point")
                market_implied = calculate_implied_probability(price)

                # Spread comparison
                if mkt["key"] == "spreads" and point is not None:
                    if name == home:
                        model_prob = sim_result.spread_cover_probs.get(-point, 0.5)
                    else:
                        model_prob = 1.0 - sim_result.spread_cover_probs.get(point, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="spreads")
                        edges.append({
                            "market": "spreads",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "fair_spread": round(sim_result.fair_spread, 1),
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

                # Total comparison
                elif mkt["key"] == "totals" and point is not None:
                    total_key = int(point)
                    if name == "Over":
                        model_prob = sim_result.over_probs.get(total_key, 0.5)
                    else:
                        model_prob = 1.0 - sim_result.over_probs.get(total_key, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="totals")
                        edges.append({
                            "market": "totals",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "fair_total": round(sim_result.fair_total, 1),
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

                # Moneyline comparison
                elif mkt["key"] == "h2h":
                    if name == home:
                        model_prob = sim_result.home_win_pct
                    else:
                        model_prob = sim_result.away_win_pct

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="h2h")
                        edges.append({
                            "market": "h2h",
                            "team": name,
                            "bookmaker": book_name,
                            "line": None,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

    edges.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return edges


# ---------------------------------------------------------------------------
# Legacy: compare_poisson_to_market (backward-compatible)
# ---------------------------------------------------------------------------

def compare_poisson_to_market(
    poisson_result: dict,
    market_odds: dict,
    home_team: str,
    away_team: str,
) -> list[dict]:
    """Compare Poisson model output to market odds for soccer/hockey/baseball."""
    edges = []

    for bm in market_odds.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "unknown"))
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                price = o.get("price", 0)
                point = o.get("point")
                market_implied = calculate_implied_probability(price)

                if mkt["key"] == "h2h":
                    if name == home_team:
                        model_prob = poisson_result["home_win"]
                    elif name == away_team:
                        model_prob = poisson_result["away_win"]
                    else:
                        model_prob = poisson_result.get("draw", 0)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edges.append({
                            "market": "h2h",
                            "team": name,
                            "bookmaker": book_name,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "ev": ev,
                        })

                elif mkt["key"] == "totals" and point is not None:
                    if name == "Over":
                        model_prob = poisson_result["over_probs"].get(point, 0.5)
                    else:
                        model_prob = 1.0 - poisson_result["over_probs"].get(point, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edges.append({
                            "market": "totals",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "ev": ev,
                        })

    edges.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return edges


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _poisson_pmf(k: int, lam: float) -> float:
    """Poisson probability mass function: P(X=k) = (lam^k * e^-lam) / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _std_dev(values: list) -> float:
    """Calculate standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return variance ** 0.5
