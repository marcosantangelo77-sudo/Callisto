"""
SGP (Same Game Parlay) correlation exploitation engine.

Books price SGP legs as if they are independent events, but many prop and
game markets within the same game are correlated. When two legs are
positively correlated, the true joint probability of both hitting is HIGHER
than the product of their individual probabilities. That means the book is
underpricing the parlay — that gap is the edge.

Core insight:
    P(A and B) = P(A) * P(B)                        ... if independent
    P(A and B) = P(A) * P(B) + rho * sigma_A * sigma_B  ... with correlation

Where rho is the Pearson correlation between the underlying stat distributions.
A positive rho increases the joint probability, meaning the parlay is worth
more than the book implies. A negative rho does the opposite.

Correlation values are hardcoded from sports analytics research:
- NFL: Football Outsiders, PFF, nflfastR play-by-play data
- NBA: Cleaning the Glass, NBA.com tracking, pbpstats.com
- MLB: FanGraphs, Baseball Savant Statcast
- NHL: Natural Stat Trick, MoneyPuck, Evolving Hockey

These are base correlations. Actual game-specific correlations vary based on
matchup context (pace, game script, weather, etc.), but the base values
provide a structural edge when books ignore them entirely.
"""

import logging
import math
from itertools import combinations
from typing import Optional

from tools.odds_api import calculate_implied_probability

logger = logging.getLogger("callisto.correlation")

# ---------------------------------------------------------------------------
# Module-level learned correlation store (set by api.py at startup)
# ---------------------------------------------------------------------------
# When set, get_correlation() blends hardcoded priors with empirically
# learned estimates (Bayesian shrinkage). When None, falls back to
# hardcoded values only (backward compatible).
_learned_store: "Optional[LearnedCorrelationStore]" = None


def set_learned_store(store) -> None:
    """Wire the LearnedCorrelationStore into all correlation lookups."""
    global _learned_store
    _learned_store = store
    logger.info("Learned correlation store wired into correlation engine")


def get_learned_store():
    """Return the module-level learned store (or None)."""
    return _learned_store


# ---------------------------------------------------------------------------
# Sport-specific correlation matrices
# ---------------------------------------------------------------------------
# Keys are tuples (market_a, market_b). Order doesn't matter — lookups check
# both directions. Values are Pearson correlation coefficients.
#
# Positive = both tend to move in the same direction
# Negative = one goes up when the other goes down
# Zero     = independent (book assumption is correct)

NFL_CORRELATIONS: dict[tuple[str, str], float] = {
    # QB passing volume correlates strongly with team scoring
    ("qb_passing_yards", "team_total"): 0.65,
    ("qb_passing_yards", "game_total"): 0.45,
    ("qb_passing_tds", "team_total"): 0.72,
    ("qb_passing_tds", "game_total"): 0.40,
    ("qb_passing_attempts", "team_total"): 0.35,
    ("qb_passing_yards", "qb_passing_tds"): 0.60,
    ("qb_passing_yards", "qb_completions"): 0.78,

    # WR/TE receiving flows through the QB
    ("wr_receiving_yards", "qb_passing_yards"): 0.55,
    ("wr_receiving_yards", "team_total"): 0.45,
    ("wr_receiving_yards", "game_total"): 0.30,
    ("wr_receptions", "qb_completions"): 0.50,
    ("wr_receptions", "qb_passing_yards"): 0.45,
    ("wr_receiving_tds", "qb_passing_tds"): 0.40,
    ("wr_receiving_tds", "team_total"): 0.38,
    ("te_receiving_yards", "qb_passing_yards"): 0.40,

    # RB rushing depends on game script — favorites run more when ahead
    ("rb_rushing_yards", "team_spread"): 0.45,  # positive when favored (negative spread)
    ("rb_rushing_yards", "team_total"): 0.30,
    ("rb_rushing_yards", "team_ml"): 0.40,
    ("rb_rushing_tds", "team_total"): 0.35,
    ("rb_rushing_tds", "team_spread"): 0.38,
    ("rb_rushing_attempts", "team_spread"): 0.50,  # favorites run clock
    ("rb_rushing_attempts", "rb_rushing_yards"): 0.72,

    # Game script correlations
    ("team_spread", "team_total"): 0.30,  # favorites in high-total games score more
    ("team_spread", "game_total"): 0.15,
    ("team_ml", "team_total"): 0.35,

    # Defensive / turnover correlations
    ("qb_interceptions", "opposing_team_total"): 0.25,  # picks lead to opponent points
    ("qb_interceptions", "team_total"): -0.30,  # picks reduce own team scoring
    ("def_sacks", "qb_passing_yards"): -0.35,  # more sacks = fewer passing yards
    ("def_sacks", "opposing_qb_passing_yards"): -0.35,

    # Kicker correlations
    ("kicker_points", "team_total"): 0.40,
    ("kicker_fg_made", "team_total"): 0.15,  # FGs inversely correlated with TDs

    # Anytime TD scorer correlations
    ("anytime_td", "team_total"): 0.42,
    ("anytime_td", "game_total"): 0.25,

    # Anti-correlations — legs that fight each other
    ("qb_passing_yards", "rb_rushing_yards"): -0.15,  # pass-heavy vs run-heavy scripts
    ("team_total", "opposing_team_total"): -0.10,  # blowouts suppress loser scoring
    ("qb_passing_yards", "opposing_qb_passing_yards"): 0.10,  # slight positive (game pace)
}

NBA_CORRELATIONS: dict[tuple[str, str], float] = {
    # Player scoring and team totals
    ("player_points", "team_total"): 0.50,
    ("player_points", "game_total"): 0.35,
    ("player_points", "team_ml"): 0.20,
    ("player_points", "team_spread"): 0.18,

    # Assists correlate with overall scoring environment
    ("player_assists", "game_total"): 0.40,
    ("player_assists", "team_total"): 0.45,
    ("player_assists", "player_points"): 0.35,  # high-usage players do both

    # Rebounds correlate with pace — more possessions = more misses = more boards
    ("player_rebounds", "game_pace"): 0.35,
    ("player_rebounds", "game_total"): 0.25,
    ("player_rebounds", "player_points"): 0.20,  # stars get both

    # Three-pointers
    ("player_threes", "player_points"): 0.55,
    ("player_threes", "team_total"): 0.30,
    ("player_threes", "game_total"): 0.20,

    # PRA (points + rebounds + assists) combos
    ("player_pra", "game_total"): 0.45,
    ("player_pra", "team_total"): 0.55,
    ("player_pra", "player_points"): 0.85,  # points dominate PRA
    ("player_pra", "player_assists"): 0.60,
    ("player_pra", "player_rebounds"): 0.55,

    # Game-level correlations
    ("team_spread", "team_total"): 0.35,
    ("team_ml", "team_total"): 0.40,
    ("game_total", "game_pace"): 0.60,

    # Blowout effects — starters sit in garbage time
    ("player_points", "game_spread_margin"): -0.15,  # blowout = less star minutes
    ("player_minutes", "game_spread_margin"): -0.25,

    # Same-team player correlations (roster context matters)
    ("teammate_a_points", "teammate_b_points"): 0.15,  # slightly positive (team scoring)
    ("player_points", "opposing_player_points"): 0.10,  # game pace effect

    # Steals / blocks (defensive stats)
    ("player_steals", "game_total"): 0.10,
    ("player_blocks", "game_total"): 0.05,
    ("player_steals", "player_points"): 0.15,  # active players do everything

    # Anti-correlations
    ("player_turnovers", "player_assists"): 0.30,  # high usage = both
    ("player_turnovers", "team_total"): -0.15,  # turnovers hurt scoring
}

MLB_CORRELATIONS: dict[tuple[str, str], float] = {
    # Batter performance and team totals
    ("batter_hits", "team_total"): 0.30,
    ("batter_total_bases", "team_total"): 0.40,
    ("batter_rbi", "team_total"): 0.55,
    ("batter_runs_scored", "team_total"): 0.50,
    ("batter_home_runs", "team_total"): 0.45,
    ("batter_home_runs", "game_total"): 0.25,
    ("batter_hits", "game_total"): 0.20,

    # Pitcher correlations
    ("pitcher_strikeouts", "pitcher_outs"): 0.65,  # deeper outings = more Ks
    ("pitcher_strikeouts", "opposing_team_total"): -0.25,  # more Ks = fewer runs
    ("pitcher_earned_runs", "opposing_team_total"): 0.60,  # direct relationship
    ("pitcher_earned_runs", "game_total"): 0.35,

    # Game-level
    ("team_spread", "team_total"): 0.35,
    ("team_ml", "team_total"): 0.40,
    ("game_total", "wind_speed"): 0.15,  # wind out = more runs (park-dependent)
    ("team_total", "opposing_pitcher_era"): 0.30,  # bad pitching = more runs

    # First 5 innings (F5) correlations
    ("f5_team_total", "team_total"): 0.65,
    ("f5_game_total", "game_total"): 0.60,
    ("f5_ml", "team_ml"): 0.80,

    # Anti-correlations
    ("pitcher_strikeouts", "batter_hits"): -0.20,  # same matchup opposition
    ("batter_stolen_bases", "team_total"): 0.10,  # weak correlation, small samples
}

NHL_CORRELATIONS: dict[tuple[str, str], float] = {
    # Skater correlations
    ("player_points_nhl", "team_total"): 0.40,
    ("player_goals", "team_total"): 0.45,
    ("player_assists_nhl", "team_total"): 0.35,
    ("player_shots_on_goal", "team_total"): 0.30,
    ("player_shots_on_goal", "player_goals"): 0.40,
    ("player_shots_on_goal", "game_total"): 0.20,

    # Goalie correlations
    ("goalie_saves", "opposing_team_total"): 0.15,  # more shots against = more saves but also more goals
    ("goalie_saves", "game_total"): 0.20,
    ("goalie_saves", "opposing_shots_on_goal"): 0.85,

    # Game-level
    ("team_spread", "team_total"): 0.30,
    ("team_ml", "team_total"): 0.35,
    ("game_total", "team_total"): 0.65,

    # Power play correlations
    ("power_play_points", "team_total"): 0.30,
    ("player_goals", "power_play_points"): 0.25,

    # Anti-correlations
    ("goalie_saves", "team_total"): -0.15,  # own team scoring less related to saves
    ("player_blocked_shots", "team_total"): -0.10,  # blocking = defending
}

# Registry for lookup
SPORT_CORRELATIONS: dict[str, dict[tuple[str, str], float]] = {
    "nfl": NFL_CORRELATIONS,
    "ncaaf": NFL_CORRELATIONS,  # same sport structure
    "nba": NBA_CORRELATIONS,
    "ncaab": NBA_CORRELATIONS,  # same sport structure
    "wnba": NBA_CORRELATIONS,
    "mlb": MLB_CORRELATIONS,
    "nhl": NHL_CORRELATIONS,
}

# Market aliases — normalize various naming conventions to canonical form
MARKET_ALIASES: dict[str, str] = {
    # NFL
    "passing_yards": "qb_passing_yards",
    "pass_yards": "qb_passing_yards",
    "passing_tds": "qb_passing_tds",
    "pass_tds": "qb_passing_tds",
    "pass_touchdowns": "qb_passing_tds",
    "passing_attempts": "qb_passing_attempts",
    "pass_attempts": "qb_passing_attempts",
    "completions": "qb_completions",
    "interceptions": "qb_interceptions",
    "rushing_yards": "rb_rushing_yards",
    "rush_yards": "rb_rushing_yards",
    "rushing_tds": "rb_rushing_tds",
    "rush_tds": "rb_rushing_tds",
    "rushing_attempts": "rb_rushing_attempts",
    "rush_attempts": "rb_rushing_attempts",
    "receiving_yards": "wr_receiving_yards",
    "rec_yards": "wr_receiving_yards",
    "receptions": "wr_receptions",
    "receiving_tds": "wr_receiving_tds",
    "rec_tds": "wr_receiving_tds",
    "sacks": "def_sacks",
    "fg_made": "kicker_fg_made",
    "field_goals": "kicker_fg_made",
    "kicker_pts": "kicker_points",
    "td_scorer": "anytime_td",
    "anytime_touchdown": "anytime_td",
    # NBA / basketball
    "points": "player_points",
    "rebounds": "player_rebounds",
    "assists": "player_assists",
    "threes": "player_threes",
    "three_pointers": "player_threes",
    "three_pointers_made": "player_threes",
    "pts_rebs_asts": "player_pra",
    "pra": "player_pra",
    "steals": "player_steals",
    "blocks": "player_blocks",
    "turnovers": "player_turnovers",
    "minutes": "player_minutes",
    # MLB
    "hits": "batter_hits",
    "total_bases": "batter_total_bases",
    "rbi": "batter_rbi",
    "rbis": "batter_rbi",
    "runs": "batter_runs_scored",
    "home_runs": "batter_home_runs",
    "hrs": "batter_home_runs",
    "strikeouts": "pitcher_strikeouts",
    "earned_runs": "pitcher_earned_runs",
    "pitcher_ks": "pitcher_strikeouts",
    "stolen_bases": "batter_stolen_bases",
    # NHL
    "goals": "player_goals",
    "shots": "player_shots_on_goal",
    "shots_on_goal": "player_shots_on_goal",
    "saves": "goalie_saves",
    "hockey_points": "player_points_nhl",
    "hockey_assists": "player_assists_nhl",
    "blocked_shots": "player_blocked_shots",
    # Game-level
    "spread": "team_spread",
    "moneyline": "team_ml",
    "ml": "team_ml",
    "total": "game_total",
    "over_under": "game_total",
    "team_over_under": "team_total",
    "pace": "game_pace",
}


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def _normalize_market(market: str) -> str:
    """Normalize a market name to its canonical form."""
    key = market.strip().lower().replace(" ", "_").replace("-", "_")
    return MARKET_ALIASES.get(key, key)


def get_correlation(
    market_a: str,
    market_b: str,
    sport: str,
    learned_store: "Optional[LearnedCorrelationStore]" = None,
) -> float:
    """
    Look up the correlation coefficient between two markets for a given sport.

    If a learned_store is provided, returns a Bayesian blend of the hardcoded
    prior and the empirically learned estimate (weighted by sample size).
    Without learned_store, returns the hardcoded value (backward compatible).

    Args:
        market_a: First market (e.g., "qb_passing_yards", "passing_yards", "points")
        market_b: Second market (e.g., "team_total", "game_total")
        sport: Sport key (e.g., "nfl", "nba", "mlb", "nhl")
        learned_store: Optional learned correlation store for data-driven blending

    Returns:
        Pearson correlation coefficient from -1.0 to 1.0.
        Returns 0.0 if the pair is not found (assumes independence).
    """
    norm_a = _normalize_market(market_a)
    norm_b = _normalize_market(market_b)
    sport_key = sport.strip().lower()

    # Strip common API prefixes
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break

    matrix = SPORT_CORRELATIONS.get(sport_key)
    if matrix is None:
        logger.warning(f"No correlation matrix for sport '{sport}'. Assuming independence.")
        return 0.0

    # Check both orderings for hardcoded prior
    prior = matrix.get((norm_a, norm_b))
    if prior is None:
        prior = matrix.get((norm_b, norm_a))
    if prior is None:
        prior = 0.0

    # Blend with learned estimate if available
    # Use explicit parameter first, fall back to module-level singleton
    store = learned_store if learned_store is not None else _learned_store
    if store is not None:
        return store.get_blended(sport_key, norm_a, norm_b, prior)

    return prior


def _american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability (no vig removal)."""
    return calculate_implied_probability(odds)


def _implied_to_american(prob: float) -> int:
    """Convert a probability to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    else:
        return int(100 * (1 - prob) / prob)


def _prob_to_decimal(prob: float) -> float:
    """Convert probability to decimal odds."""
    if prob <= 0:
        return float("inf")
    return 1.0 / prob


def _adjust_joint_probability(
    prob_a: float,
    prob_b: float,
    rho: float,
) -> float:
    """
    Calculate the joint probability of two events given their correlation.

    Uses the Gaussian copula approximation:
        P(A and B) = P(A)*P(B) + rho * sqrt(P(A)*(1-P(A)) * P(B)*(1-P(B)))

    This comes from the bivariate normal relationship where:
        Cov(X,Y) = rho * sigma_X * sigma_Y

    For Bernoulli variables, sigma = sqrt(p*(1-p)), so:
        P(A and B) = E[X]*E[Y] + Cov(X,Y)
                    = p_a * p_b + rho * sqrt(p_a*(1-p_a)) * sqrt(p_b*(1-p_b))

    The result is clamped to [0, min(p_a, p_b)] for validity.

    Args:
        prob_a: Marginal probability of event A
        prob_b: Marginal probability of event B
        rho: Pearson correlation coefficient (-1 to 1)

    Returns:
        Adjusted joint probability.
    """
    independent = prob_a * prob_b
    sigma_a = math.sqrt(prob_a * (1 - prob_a)) if 0 < prob_a < 1 else 0.0
    sigma_b = math.sqrt(prob_b * (1 - prob_b)) if 0 < prob_b < 1 else 0.0
    adjustment = rho * sigma_a * sigma_b
    joint = independent + adjustment

    # Clamp to valid probability range
    # Frechet-Hoeffding bounds: max(0, p_a + p_b - 1) <= P(A,B) <= min(p_a, p_b)
    lower_bound = max(0.0, prob_a + prob_b - 1.0)
    upper_bound = min(prob_a, prob_b)
    joint = max(lower_bound, min(upper_bound, joint))

    return joint


def independent_parlay_odds(legs: list[dict]) -> int:
    """
    Calculate parlay odds assuming all legs are independent.

    This is how books price parlays — multiply the individual implied
    probabilities together. The result is the "naive" parlay price.

    Args:
        legs: List of dicts, each with "american_odds" (int).

    Returns:
        American odds for the parlay assuming full independence.
    """
    if not legs:
        return 0

    joint_prob = 1.0
    for leg in legs:
        odds = leg.get("american_odds", -110)
        prob = _american_to_implied(odds)
        joint_prob *= prob

    if joint_prob <= 0 or joint_prob >= 1:
        return 0
    return _implied_to_american(joint_prob)


def correlated_parlay_odds(legs: list[dict], correlations: Optional[dict] = None, sport: str = "nfl") -> int:
    """
    Calculate parlay odds adjusted for correlations between legs.

    Processes legs pairwise: for each pair of legs with a non-zero correlation,
    the joint probability is adjusted upward (positive correlation) or downward
    (negative correlation) relative to the independent product.

    For N legs, we:
    1. Start with the independent joint probability (product of all marginals).
    2. For each correlated pair, compute the adjustment delta:
       delta = rho * sigma_A * sigma_B
    3. Sum all pairwise deltas and apply to the independent product.

    This is an approximation — the full multivariate copula is intractable
    for arbitrary N, but the pairwise adjustment captures the dominant effect
    and is the standard approach in quantitative sports betting.

    Args:
        legs: List of dicts with "american_odds" and "market" keys.
        correlations: Optional dict mapping (market_a, market_b) -> rho.
                      If None, uses the sport's default correlation matrix.
        sport: Sport for default correlation lookup.

    Returns:
        American odds for the correlation-adjusted parlay.
    """
    if not legs:
        return 0

    # Get marginal probabilities
    probs = []
    for leg in legs:
        odds = leg.get("american_odds", -110)
        probs.append(_american_to_implied(odds))

    # Independent product
    independent_joint = 1.0
    for p in probs:
        independent_joint *= p

    # Sum pairwise correlation adjustments
    total_adjustment = 0.0
    for i, j in combinations(range(len(legs)), 2):
        market_a = _normalize_market(legs[i].get("market", ""))
        market_b = _normalize_market(legs[j].get("market", ""))

        if correlations:
            rho = correlations.get((market_a, market_b), 0.0)
            if rho == 0.0:
                rho = correlations.get((market_b, market_a), 0.0)
        else:
            rho = get_correlation(market_a, market_b, sport)

        if rho == 0.0:
            continue

        sigma_a = math.sqrt(probs[i] * (1 - probs[i])) if 0 < probs[i] < 1 else 0.0
        sigma_b = math.sqrt(probs[j] * (1 - probs[j])) if 0 < probs[j] < 1 else 0.0

        # Scale adjustment by the product of all OTHER legs' probabilities
        # so the pairwise adjustment propagates correctly through the full parlay.
        other_product = 1.0
        for k in range(len(probs)):
            if k != i and k != j:
                other_product *= probs[k]

        total_adjustment += rho * sigma_a * sigma_b * other_product

    adjusted_joint = independent_joint + total_adjustment

    # Clamp to valid range
    adjusted_joint = max(1e-9, min(1.0 - 1e-9, adjusted_joint))

    return _implied_to_american(adjusted_joint)


def detect_mispriced_correlation(
    legs: list[dict],
    book_parlay_odds: int,
    sport: str,
) -> dict:
    """
    Detect whether a book is mispricing an SGP by ignoring correlations.

    Compares the book's offered parlay odds (which assume independence or
    apply a crude correlation penalty) against our correlation-adjusted
    fair odds. If the book offers better odds than our adjusted fair value,
    the SGP has positive expected value.

    Args:
        legs: List of dicts with:
            - "american_odds" (int): individual leg odds
            - "market" (str): market type (e.g., "qb_passing_yards", "team_total")
            - "description" (str, optional): human-readable leg description
        book_parlay_odds: The American odds the book is offering for the parlay
        sport: Sport key (e.g., "nfl", "nba")

    Returns:
        Dict with mispricing analysis:
        - true_correlation: weighted average correlation across all leg pairs
        - book_assumed_correlation: implied correlation from book's pricing
        - edge_from_correlation: probability edge from correlation mispricing
        - mispricing_pct: edge as a percentage
        - is_positive_ev: whether the SGP is +EV
        - anti_correlation_warning: flags if legs fight each other
        - leg_pair_correlations: detailed per-pair correlation breakdown
    """
    if not legs or len(legs) < 2:
        return {"error": "Need at least 2 legs for correlation analysis"}

    # Individual marginal probabilities
    marginals = []
    for leg in legs:
        odds = leg.get("american_odds", -110)
        marginals.append(_american_to_implied(odds))

    # Independent joint probability (what a naive book would assume)
    independent_joint = 1.0
    for p in marginals:
        independent_joint *= p

    # Book's implied joint probability from their offered parlay odds
    book_implied_joint = _american_to_implied(book_parlay_odds)

    # Calculate correlation-adjusted joint probability (our fair estimate)
    total_adjustment = 0.0
    pair_details = []
    has_anti_correlation = False

    for (i, j) in combinations(range(len(legs)), 2):
        market_a = legs[i].get("market", "unknown")
        market_b = legs[j].get("market", "unknown")
        rho = get_correlation(market_a, market_b, sport)

        sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
        sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0

        other_product = 1.0
        for k in range(len(marginals)):
            if k != i and k != j:
                other_product *= marginals[k]

        pair_adjustment = rho * sigma_a * sigma_b * other_product

        if rho < -0.05:
            has_anti_correlation = True

        pair_details.append({
            "leg_a": legs[i].get("description", market_a),
            "leg_b": legs[j].get("description", market_b),
            "market_a": market_a,
            "market_b": market_b,
            "correlation": rho,
            "adjustment": round(pair_adjustment, 6),
            "direction": "positive" if rho > 0 else ("negative" if rho < 0 else "independent"),
        })

        total_adjustment += pair_adjustment

    # Our true joint probability with correlations
    true_joint = independent_joint + total_adjustment
    true_joint = max(1e-9, min(1.0 - 1e-9, true_joint))

    # Edge: difference between book's price and our fair price
    # If true_joint > book_implied_joint, the parlay hits more often than
    # the book thinks → the book is underpricing it → +EV for us
    edge = true_joint - book_implied_joint
    mispricing_pct = (edge / book_implied_joint * 100) if book_implied_joint > 0 else 0.0

    # Reverse-engineer what correlation the book is assuming
    # book_implied = independent + book_rho_adj
    # book_rho_adj = book_implied - independent
    book_rho_adj = book_implied_joint - independent_joint
    # Approximate book's assumed weighted-average correlation
    # Using the average sigma product as denominator
    if pair_details:
        avg_sigma_product = 0.0
        for (i, j) in combinations(range(len(marginals)), 2):
            sa = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
            sb = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
            other_prod = 1.0
            for k in range(len(marginals)):
                if k != i and k != j:
                    other_prod *= marginals[k]
            avg_sigma_product += sa * sb * other_prod
        book_assumed_rho = book_rho_adj / avg_sigma_product if avg_sigma_product > 0 else 0.0
    else:
        book_assumed_rho = 0.0

    # Weighted average of our correlations
    weighted_rho_sum = sum(abs(pd["correlation"]) for pd in pair_details)
    num_pairs = len(pair_details)
    avg_rho = weighted_rho_sum / num_pairs if num_pairs > 0 else 0.0

    # Fair odds
    fair_american = _implied_to_american(true_joint)
    independent_american = _implied_to_american(independent_joint)

    # Anti-correlation warnings
    anti_warnings = []
    if has_anti_correlation:
        for pd in pair_details:
            if pd["correlation"] < -0.05:
                anti_warnings.append(
                    f"WARNING: {pd['leg_a']} and {pd['leg_b']} are negatively correlated "
                    f"(rho={pd['correlation']:.2f}). This parlay is HARDER to hit than "
                    f"the independent price suggests."
                )

    return {
        "true_correlation": round(avg_rho, 4),
        "book_assumed_correlation": round(book_assumed_rho, 4),
        "edge_from_correlation": round(edge, 6),
        "mispricing_pct": round(mispricing_pct, 2),
        "is_positive_ev": edge > 0,
        "independent_joint_prob": round(independent_joint, 6),
        "book_implied_joint_prob": round(book_implied_joint, 6),
        "true_joint_prob": round(true_joint, 6),
        "independent_odds": independent_american,
        "book_offered_odds": book_parlay_odds,
        "fair_odds": fair_american,
        "leg_pair_correlations": pair_details,
        "anti_correlation_warning": anti_warnings if anti_warnings else None,
        "assessment": _assess_mispricing(edge, mispricing_pct, has_anti_correlation, avg_rho),
    }


def build_correlated_parlay(
    available_props: list[dict],
    game_data: dict,
    sport: str,
    min_correlation: float = 0.3,
    max_legs: int = 4,
    min_legs: int = 2,
) -> list[dict]:
    """
    Build optimally correlated SGP suggestions from available props and game lines.

    Scans all possible combinations of available props/markets within a game and
    ranks them by the degree of correlation mispricing — i.e., how much edge
    comes from the book treating correlated legs as independent.

    Args:
        available_props: List of available betting options, each a dict with:
            - "market" (str): market type
            - "american_odds" (int): offered odds
            - "description" (str): human-readable description
            - "player" (str, optional): player name
            - "side" (str, optional): "over" or "under"
            - "line" (float, optional): the prop line
        game_data: Game-level data dict with:
            - "home_team" (str)
            - "away_team" (str)
            - "game_total" (float, optional): posted game total
            - "spread" (float, optional): posted spread
        sport: Sport key
        min_correlation: Minimum average pairwise correlation to include a combo.
        max_legs: Maximum legs per parlay suggestion.
        min_legs: Minimum legs per parlay suggestion.

    Returns:
        List of parlay suggestions sorted by correlation edge (highest first).
        Each suggestion includes the legs, correlations, and estimated edge.
    """
    if not available_props:
        return []

    suggestions = []

    # Try all combinations from min_legs to max_legs
    for num_legs in range(min_legs, min(max_legs + 1, len(available_props) + 1)):
        for combo in combinations(range(len(available_props)), num_legs):
            legs = [available_props[idx] for idx in combo]
            markets = [_normalize_market(leg.get("market", "")) for leg in legs]

            # Calculate pairwise correlations
            pair_rhos = []
            total_rho = 0.0
            all_positive = True

            for (i, j) in combinations(range(len(legs)), 2):
                rho = get_correlation(markets[i], markets[j], sport)
                pair_rhos.append({
                    "leg_a": legs[i].get("description", markets[i]),
                    "leg_b": legs[j].get("description", markets[j]),
                    "correlation": rho,
                })
                total_rho += rho
                if rho < 0:
                    all_positive = False

            num_pairs = len(pair_rhos)
            avg_correlation = total_rho / num_pairs if num_pairs > 0 else 0.0

            # Filter: only suggest parlays above the min correlation threshold
            if avg_correlation < min_correlation:
                continue

            # Calculate pricing
            marginals = [_american_to_implied(leg.get("american_odds", -110)) for leg in legs]

            independent_joint = 1.0
            for p in marginals:
                independent_joint *= p

            # Correlation-adjusted probability
            adjustment = 0.0
            for (i, j) in combinations(range(len(legs)), 2):
                rho = get_correlation(markets[i], markets[j], sport)
                sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
                sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
                other_product = 1.0
                for k in range(len(marginals)):
                    if k != i and k != j:
                        other_product *= marginals[k]
                adjustment += rho * sigma_a * sigma_b * other_product

            true_joint = max(1e-9, min(1.0 - 1e-9, independent_joint + adjustment))

            # Edge from correlation = true probability - book's independent price
            correlation_edge = true_joint - independent_joint
            edge_pct = (correlation_edge / independent_joint * 100) if independent_joint > 0 else 0.0

            # Convert to odds
            independent_odds = _implied_to_american(independent_joint)
            fair_odds = _implied_to_american(true_joint)

            home = game_data.get("home_team", "")
            away = game_data.get("away_team", "")

            suggestions.append({
                "game": f"{away} @ {home}" if away and home else "Unknown",
                "num_legs": num_legs,
                "legs": [
                    {
                        "description": leg.get("description", ""),
                        "market": leg.get("market", ""),
                        "american_odds": leg.get("american_odds", 0),
                        "implied_prob": round(_american_to_implied(leg.get("american_odds", -110)), 4),
                        "player": leg.get("player", ""),
                        "side": leg.get("side", ""),
                        "line": leg.get("line"),
                    }
                    for leg in legs
                ],
                "pair_correlations": pair_rhos,
                "avg_correlation": round(avg_correlation, 4),
                "all_positive_correlations": all_positive,
                "independent_joint_prob": round(independent_joint, 6),
                "true_joint_prob": round(true_joint, 6),
                "correlation_edge": round(correlation_edge, 6),
                "correlation_edge_pct": round(edge_pct, 2),
                "independent_parlay_odds": independent_odds,
                "fair_parlay_odds": fair_odds,
                "rating": _rate_correlation_edge(edge_pct, avg_correlation),
            })

    # Sort by correlation edge percentage (highest mispricing first)
    suggestions.sort(key=lambda x: x["correlation_edge_pct"], reverse=True)

    logger.info(
        f"Built {len(suggestions)} correlated parlay suggestions for {sport} "
        f"(min_corr={min_correlation}, max_legs={max_legs})"
    )

    return suggestions


# ---------------------------------------------------------------------------
# Utility / assessment helpers
# ---------------------------------------------------------------------------

def _assess_mispricing(
    edge: float,
    mispricing_pct: float,
    has_anti: bool,
    avg_rho: float,
) -> str:
    """Generate a human-readable assessment of an SGP mispricing."""
    if has_anti:
        return (
            f"CAUTION — This parlay contains negatively correlated legs. "
            f"The true hit rate is LOWER than independent pricing suggests. "
            f"Mispricing: {mispricing_pct:+.1f}%. "
            f"{'Avoid this parlay.' if edge < 0 else 'Edge exists despite anti-correlation, but proceed with caution.'}"
        )

    if edge <= 0:
        return (
            f"NO EDGE — Book pricing is fair or favors the book. "
            f"Mispricing: {mispricing_pct:+.1f}%. Avg correlation: {avg_rho:.2f}. "
            f"The book may be applying a sufficient correlation penalty."
        )

    if mispricing_pct > 15:
        severity = "EXCEPTIONAL"
    elif mispricing_pct > 8:
        severity = "STRONG"
    elif mispricing_pct > 3:
        severity = "GOOD"
    else:
        severity = "MARGINAL"

    return (
        f"{severity} EDGE — Correlation mispricing of {mispricing_pct:+.1f}%. "
        f"Avg pairwise correlation: {avg_rho:.2f}. "
        f"Book is underpricing this parlay by treating correlated legs as independent. "
        f"True hit probability is {edge:.4f} higher than book implies."
    )


def _rate_correlation_edge(edge_pct: float, avg_rho: float) -> str:
    """Rate a parlay suggestion based on its correlation edge."""
    if avg_rho >= 0.5 and edge_pct >= 10:
        return "ELITE"
    elif avg_rho >= 0.4 and edge_pct >= 6:
        return "STRONG"
    elif avg_rho >= 0.3 and edge_pct >= 3:
        return "GOOD"
    elif edge_pct >= 1:
        return "MARGINAL"
    else:
        return "WEAK"


def get_all_correlations(sport: str) -> dict[tuple[str, str], float]:
    """
    Return the full correlation matrix for a sport.

    Useful for inspection, debugging, or building custom correlation overrides.
    """
    sport_key = sport.strip().lower()
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break
    return dict(SPORT_CORRELATIONS.get(sport_key, {}))


def list_correlated_markets(market: str, sport: str, min_abs_rho: float = 0.2) -> list[dict]:
    """
    List all markets that are correlated with the given market above a threshold.

    Useful for identifying which legs to pair in an SGP.

    Args:
        market: The market to find correlations for.
        sport: Sport key.
        min_abs_rho: Minimum absolute correlation to include.

    Returns:
        List of dicts with correlated markets and their correlation values,
        sorted by absolute correlation (strongest first).
    """
    norm = _normalize_market(market)
    sport_key = sport.strip().lower()
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break

    matrix = SPORT_CORRELATIONS.get(sport_key, {})
    results = []

    for (a, b), rho in matrix.items():
        if abs(rho) < min_abs_rho:
            continue
        if a == norm:
            results.append({"market": b, "correlation": rho, "direction": "positive" if rho > 0 else "negative"})
        elif b == norm:
            results.append({"market": a, "correlation": rho, "direction": "positive" if rho > 0 else "negative"})

    results.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    return results


def detect_anti_correlation(legs: list[dict], sport: str) -> list[dict]:
    """
    Scan a set of parlay legs for anti-correlated pairs that make the
    parlay harder to hit than independently priced.

    This is the inverse of the edge — if you build a parlay with legs
    that fight each other (e.g., both QBs throwing 300+ in a game with
    a total of 38), the book is OVERPRICING the parlay in your favor...
    in the wrong direction. You'll hit it LESS often than the odds suggest.

    Args:
        legs: List of dicts with "market" and optionally "description".
        sport: Sport key.

    Returns:
        List of anti-correlated pairs with their correlations and warnings.
    """
    warnings = []

    for (i, j) in combinations(range(len(legs)), 2):
        market_a = legs[i].get("market", "")
        market_b = legs[j].get("market", "")
        rho = get_correlation(market_a, market_b, sport)

        if rho < -0.05:  # Meaningful negative correlation
            warnings.append({
                "leg_a": legs[i].get("description", market_a),
                "leg_b": legs[j].get("description", market_b),
                "market_a": market_a,
                "market_b": market_b,
                "correlation": rho,
                "severity": "HIGH" if rho < -0.25 else ("MODERATE" if rho < -0.15 else "LOW"),
                "warning": (
                    f"These legs are negatively correlated (rho={rho:.2f}). "
                    f"When one hits, the other is LESS likely to hit. "
                    f"The parlay is harder to win than the independent odds suggest."
                ),
            })

    return warnings


def estimate_sgp_vig(
    legs: list[dict],
    book_parlay_odds: int,
    sport: str,
) -> dict:
    """
    Estimate how much extra vig (juice) the book is charging on an SGP
    beyond standard parlay vig.

    Books apply a "correlation tax" to SGPs — they know some legs are
    correlated and mark up the price. The question is: are they charging
    MORE or LESS than the actual correlation warrants?

    If the book's SGP vig exceeds the true correlation adjustment, the
    parlay is overpriced (bad for us). If it's less, the parlay is
    underpriced (edge for us).

    Args:
        legs: Parlay legs with "american_odds" and "market".
        book_parlay_odds: The SGP odds the book is offering.
        sport: Sport key.

    Returns:
        Breakdown of vig components.
    """
    marginals = [_american_to_implied(leg.get("american_odds", -110)) for leg in legs]

    # Independent joint
    independent_joint = 1.0
    for p in marginals:
        independent_joint *= p

    # Book's implied
    book_implied = _american_to_implied(book_parlay_odds)

    # Standard parlay vig (what a normal uncorrelated parlay would be juiced to)
    # Books typically charge ~10-20% vig on parlays via individual leg juice
    standard_vig_joint = independent_joint  # already includes per-leg vig

    # The SGP-specific adjustment the book made
    sgp_adjustment = book_implied - independent_joint

    # Our true correlation adjustment
    true_adjustment = 0.0
    for (i, j) in combinations(range(len(legs)), 2):
        market_a = _normalize_market(legs[i].get("market", ""))
        market_b = _normalize_market(legs[j].get("market", ""))
        rho = get_correlation(market_a, market_b, sport)
        if rho == 0:
            continue
        sigma_a = math.sqrt(marginals[i] * (1 - marginals[i])) if 0 < marginals[i] < 1 else 0.0
        sigma_b = math.sqrt(marginals[j] * (1 - marginals[j])) if 0 < marginals[j] < 1 else 0.0
        other_product = 1.0
        for k in range(len(marginals)):
            if k != i and k != j:
                other_product *= marginals[k]
        true_adjustment += rho * sigma_a * sigma_b * other_product

    # The extra vig beyond correlation
    extra_vig = sgp_adjustment - true_adjustment

    return {
        "independent_prob": round(independent_joint, 6),
        "book_implied_prob": round(book_implied, 6),
        "true_correlated_prob": round(independent_joint + true_adjustment, 6),
        "sgp_adjustment_book": round(sgp_adjustment, 6),
        "sgp_adjustment_true": round(true_adjustment, 6),
        "extra_sgp_vig": round(extra_vig, 6),
        "extra_sgp_vig_pct": round((extra_vig / independent_joint * 100) if independent_joint > 0 else 0, 2),
        "independent_odds": _implied_to_american(independent_joint),
        "book_odds": book_parlay_odds,
        "fair_odds": _implied_to_american(independent_joint + true_adjustment),
        "assessment": (
            f"Book charges {abs(sgp_adjustment):.4f} SGP adjustment vs "
            f"true correlation of {true_adjustment:+.4f}. "
            + (
                f"Book is UNDERCHARGING by {abs(extra_vig):.4f} — edge exists."
                if extra_vig < -0.001
                else (
                    f"Book is OVERCHARGING by {extra_vig:.4f} — no edge, avoid."
                    if extra_vig > 0.001
                    else "Book pricing is approximately fair."
                )
            )
        ),
    }
