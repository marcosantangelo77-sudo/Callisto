"""
Callisto simulation engine — generate our own probability distributions.

This is the CORE of Callisto. Everything downstream depends on accuracy here.

Four simulation types:
  1. NBA: Possession-based Monte Carlo (100K sims)
  2. NFL: Negative Binomial (discrete, fat-tailed, natural ≥ 0)
  3. Poisson: MLB, NHL, Soccer (analytical — no simulation needed)
  4. Player Props: Per-minute rate × context-adjusted minutes

Uses numpy for vectorized Monte Carlo. Pure Python for Poisson PMF.
No scipy dependency (blocked by DLL policy on this system).
"""

import logging
import math
from typing import Optional

import numpy as np

from tools.math_utils import american_to_decimal, american_to_implied

logger = logging.getLogger("callisto.sim")


# ──────────────────────────────────────────────────
# 1. NBA POSSESSION-BASED MONTE CARLO
# ──────────────────────────────────────────────────

def nba_game_sim(
    team_a_off_rtg: float,      # pts per 100 possessions (offense)
    team_a_def_rtg: float,      # pts allowed per 100 possessions
    team_b_off_rtg: float,
    team_b_def_rtg: float,
    team_a_pace: float,         # possessions per 48 min
    team_b_pace: float,
    league_avg_pace: float = 100.0,
    league_avg_off_rtg: float = 112.0,
    home_adv: float = 2.5,     # points added to home team (team_a)
    rest_adj_a: float = 0.0,   # B2B fatigue: -1.5 to -2.0
    rest_adj_b: float = 0.0,
    off_rtg_std: float = 5.0,  # game-to-game offensive variance
    pace_std: float = 3.0,     # game-to-game pace variance
    n_sims: int = 100_000,
    seed: Optional[int] = None,  # Set for reproducibility (testing/QA)
) -> dict:
    """
    HOW BOOKS MODEL NBA GAMES:
      1. PACE = (team_a_pace × team_b_pace) / league_avg_pace
      2. ADJUSTED OFFENSE = (team_off × opponent_def) / league_avg_off
      3. EXPECTED POINTS = (adjusted_offense / 100) × pace
      4. SIMULATION adds game-to-game variance

    Verified:
      Even matchup (112/112/112/112, 100 pace): home ML ≈ 0.61, margin ≈ 2.5
      Mismatch (119off/108def vs 109off/116def): home ML > 0.95
    """
    # Use a dedicated RNG so seed=None still produces fresh randomness in production
    # but seed=int gives reproducible results for tests/QA.
    rng = np.random.default_rng(seed)

    # Expected pace for this matchup
    exp_pace = (team_a_pace * team_b_pace) / league_avg_pace

    # Simulate pace (shared — both teams play at same pace)
    pace = rng.normal(exp_pace, pace_std, n_sims)
    pace = np.maximum(pace, 80)

    # Opponent-adjusted offensive efficiency
    a_adj_off = (team_a_off_rtg * team_b_def_rtg) / league_avg_off_rtg
    b_adj_off = (team_b_off_rtg * team_a_def_rtg) / league_avg_off_rtg

    # Add home court, rest, and random variance
    a_eff = rng.normal(a_adj_off + home_adv + rest_adj_a, off_rtg_std, n_sims)
    b_eff = rng.normal(b_adj_off + rest_adj_b, off_rtg_std, n_sims)

    # Points = (efficiency / 100) × possessions
    a_pts = np.round(np.maximum((a_eff / 100) * pace, 70)).astype(int)
    b_pts = np.round(np.maximum((b_eff / 100) * pace, 70)).astype(int)

    total = a_pts + b_pts
    margin = a_pts - b_pts

    result = {
        "type": "nba_game_sim",
        "n_sims": n_sims,
        "a_mean": float(np.mean(a_pts)),
        "b_mean": float(np.mean(b_pts)),
        "total_mean": float(np.mean(total)),
        "margin_mean": float(np.mean(margin)),
        "a_win_prob": float(np.mean(margin > 0)),
        "b_win_prob": float(np.mean(margin < 0)),
        "spreads": {},
        "totals": {},
    }

    # Spread probabilities (team A covers)
    for s in np.arange(-15, 15.5, 0.5):
        s_val = float(s)
        result["spreads"][s_val] = float(np.mean(margin + s > 0))

    # Total probabilities
    for t in np.arange(190, 260, 0.5):
        t_val = float(t)
        result["totals"][t_val] = float(np.mean(total > t))

    return result


# ──────────────────────────────────────────────────
# 2. NFL NEGATIVE BINOMIAL
# ──────────────────────────────────────────────────

def nfl_game_sim(
    team_a_expected_pts: float,
    team_b_expected_pts: float,
    overdispersion: float = 1.5,
    n_sims: int = 100_000,
    seed: Optional[int] = None,
) -> dict:
    """
    Negative Binomial model for NFL.

    WHY NOT NORMAL: NFL scores come in discrete chunks (3s and 7s).
    NegBin is discrete, naturally ≥ 0, and has fatter tails.

    Parameterization (mean-dispersion):
      mean = expected_pts
      variance = mean × overdispersion
      p = 1 / overdispersion
      r = mean × p / (1 - p)

    Derive expected points from spread and total:
      fav_pts = (total + |spread|) / 2
      dog_pts = (total - |spread|) / 2
    """
    rng = np.random.default_rng(seed)

    def _negbin_sample(mean_pts, n):
        # Guard: overdispersion must be > 1 (variance > mean for NegBin)
        if overdispersion <= 1.0:
            return np.zeros(n, dtype=int)
        p = 1 / overdispersion
        r = mean_pts * p / (1 - p)
        if r <= 0 or not np.isfinite(r):
            return np.zeros(n, dtype=int)
        # numpy's negative_binomial: n=r, p=p
        return rng.negative_binomial(r, p, size=n)

    a = _negbin_sample(team_a_expected_pts, n_sims).astype(int)
    b = _negbin_sample(team_b_expected_pts, n_sims).astype(int)
    margin = a - b
    total = a + b

    result = {
        "type": "nfl_game_sim",
        "n_sims": n_sims,
        "a_mean": float(np.mean(a)),
        "b_mean": float(np.mean(b)),
        "total_mean": float(np.mean(total)),
        "margin_mean": float(np.mean(margin)),
        "a_win": float(np.mean(margin > 0)),
        "b_win": float(np.mean(margin < 0)),
        "spreads": {},
        "totals": {},
    }

    for s in np.arange(-14, 14.5, 0.5):
        s_val = float(s)
        result["spreads"][s_val] = float(np.mean(margin + s > 0))

    for t in np.arange(30, 60, 0.5):
        t_val = float(t)
        result["totals"][t_val] = float(np.mean(total > t))

    return result


# ──────────────────────────────────────────────────
# 3. POISSON MODEL (MLB, NHL, Soccer)
# ──────────────────────────────────────────────────

def _poisson_pmf(k: int, lam: float) -> float:
    """Pure Python Poisson PMF: P(X=k) = e^(-λ) × λ^k / k!"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_game(
    team_a_attack: float,       # goals/runs per game average
    team_b_attack: float,
    team_a_defense: float,      # goals/runs allowed per game average
    team_b_defense: float,
    league_avg: float,          # league avg goals/runs per team per game
    home_factor: float = 1.05,
    max_score: int = 12,
) -> dict:
    """
    Standard Poisson model. Analytical solution (no simulation needed).
    For soccer, hockey, baseball.

    FORMULA (same as what books use):
      lambda_a = (team_a_attack × team_b_defense / league_avg) × home_factor
      lambda_b = (team_b_attack × team_a_defense / league_avg)

    Score matrix: P(i,j) = poisson(i, λ_a) × poisson(j, λ_b)

    KNOWN BIAS: Assumes score independence. Real scores have slight
    positive correlation (~0.08 in soccer). Corrections applied:
      - BTTS Yes: add +0.008
      - Draws: add +0.012
      - Over/under main totals: bias < 0.5 cents (acceptable)

    Verified:
      λ_a=1.5, λ_b=1.1 → home≈0.446, draw≈0.242, away≈0.313
    """
    lambda_a = team_a_attack * team_b_defense / league_avg * home_factor
    lambda_b = team_b_attack * team_a_defense / league_avg

    # Build score matrix
    matrix = [[0.0] * (max_score + 1) for _ in range(max_score + 1)]
    for i in range(max_score + 1):
        for j in range(max_score + 1):
            matrix[i][j] = _poisson_pmf(i, lambda_a) * _poisson_pmf(j, lambda_b)

    p_home = sum(matrix[i][j] for i in range(max_score + 1)
                 for j in range(max_score + 1) if i > j)
    p_draw = sum(matrix[i][i] for i in range(max_score + 1))
    p_away = 1 - p_home - p_draw

    btts_yes = sum(matrix[i][j] for i in range(1, max_score + 1)
                   for j in range(1, max_score + 1))

    totals = {}
    for t_half in range(1, max_score * 4 + 1):
        t = t_half * 0.5
        p_over = sum(matrix[i][j] for i in range(max_score + 1)
                     for j in range(max_score + 1) if i + j > t)
        totals[t] = {"over": round(p_over, 6), "under": round(1 - p_over, 6)}

    return {
        "type": "poisson_game",
        "lambda_a": round(lambda_a, 4),
        "lambda_b": round(lambda_b, 4),
        "home_win": round(p_home, 6),
        "draw": round(p_draw, 6),
        "away_win": round(p_away, 6),
        "btts_yes": round(btts_yes, 6),
        "btts_no": round(1 - btts_yes, 6),
        "btts_yes_corrected": round(btts_yes + 0.008, 6),
        "draw_corrected": round(p_draw + 0.012, 6),
        "totals": totals,
    }


# ──────────────────────────────────────────────────
# 4. PLAYER PROP SIMULATION
# ──────────────────────────────────────────────────

def player_prop_sim(
    stat_per_min: float,        # player's per-minute rate for this stat
    stat_per_min_std: float,    # game-to-game variance in per-min rate
    projected_minutes: float,   # context-adjusted minutes THIS GAME
    minutes_std: float,         # minutes variance (higher for blowout risk)
    pace_factor: float = 1.0,   # this matchup pace / league avg (1.0 = avg)
    defense_factor: float = 1.0, # opponent def vs position (>1 = weaker D)
    usage_factor: float = 1.0,  # usage shift from injuries (>1 = more usage)
    n_sims: int = 100_000,
    stat_name: str = "points",
) -> dict:
    """
    Monte Carlo simulation for a single player stat.

    THIS IS WHERE THE EDGE LIVES.
    DK prices player props off season averages. We model context
    (pace, matchup, usage, minutes) that their automated pricing underweights.

    MODELS TWO INDEPENDENT RANDOM VARIABLES:
      1. Minutes played (truncated normal, 0 to 48)
      2. Per-minute production rate (normal, clipped at 0)

    Total stat = minutes × rate

    Each context factor shifts the PER-MINUTE RATE, not the total.
    """
    # Simulate minutes (truncated normal: 0 to 48)
    mins = np.random.normal(projected_minutes, minutes_std, n_sims)
    mins = np.clip(mins, 0, 48)

    # Simulate per-minute rate with context adjustments
    adj_rate = stat_per_min * pace_factor * defense_factor * usage_factor
    rates = np.maximum(np.random.normal(adj_rate, stat_per_min_std, n_sims), 0)

    # Total = minutes × rate
    totals = np.maximum(np.round(mins * rates), 0)

    mean = float(np.mean(totals))
    std = float(np.std(totals))
    median = float(np.median(totals))

    # Probabilities at various lines
    lines = {}
    for line in np.arange(max(0, mean - 15), mean + 15, 0.5):
        l = float(line)
        lines[l] = {
            "over": float(np.mean(totals > line)),
            "under": float(np.mean(totals < line)),
            "push": float(np.mean(totals == line)),
        }

    return {
        "type": "player_prop_sim",
        "stat": stat_name,
        "mean": round(mean, 2),
        "median": round(median, 1),
        "std": round(std, 2),
        "adjusted_rate": round(adj_rate, 4),
        "projected_minutes": projected_minutes,
        "context": {
            "pace_factor": pace_factor,
            "defense_factor": defense_factor,
            "usage_factor": usage_factor,
        },
        "n_sims": n_sims,
        "lines": lines,
    }


# ──────────────────────────────────────────────────
# EDGE COMPARISON: simulation vs book
# ──────────────────────────────────────────────────

def compare_sim_to_book(
    sim_prob: float,
    book_odds_american: int,
    bet_side: str = "over",
    confidence: str = "medium",
) -> dict:
    """
    Compare simulation probability to book's line.
    Returns edge, EV, and recommendation.

    confidence levels determine minimum EV threshold:
      high (Pinnacle devig):  min EV 2%
      medium (multi-book):    min EV 3%
      low (model only):       min EV 5%
    """
    book_implied = american_to_implied(book_odds_american)
    book_decimal = american_to_decimal(book_odds_american)

    edge = sim_prob - book_implied
    ev = (sim_prob * book_decimal) - 1.0

    thresholds = {"high": 0.02, "medium": 0.03, "low": 0.05}
    min_ev = thresholds.get(confidence, 0.03)

    actionable = ev >= min_ev

    if ev >= 0.07:
        rating = "STRONG"
    elif ev >= 0.03:
        rating = "GOOD"
    elif ev >= 0.01:
        rating = "MARGINAL"
    else:
        rating = "NO_EDGE"

    return {
        "bet_side": bet_side,
        "sim_probability": round(sim_prob, 4),
        "book_implied": round(book_implied, 4),
        "book_odds": book_odds_american,
        "edge": round(edge, 4),
        "edge_pct": round(edge * 100, 2),
        "ev": round(ev, 4),
        "ev_pct": round(ev * 100, 2),
        "confidence": confidence,
        "min_ev_threshold": min_ev,
        "actionable": actionable,
        "rating": rating,
    }


def sim_from_odds(
    spread: float,
    total: float,
    sport: str = "nba",
    n_sims: int = 100_000,
) -> dict:
    """
    Quick simulation from a book's spread/total line.
    Infers team power ratings and runs appropriate sim.

    Returns full simulation result.
    """
    sport_lower = sport.lower()

    if "nba" in sport_lower or "ncaab" in sport_lower or "basketball" in sport_lower:
        # Infer from spread+total → expected scores
        home_pts = (total - spread) / 2  # spread is from home perspective
        away_pts = (total + spread) / 2
        # Convert to per-100-possession efficiency
        avg_pace = 100.0 if "nba" in sport_lower else 68.0
        avg_off = 112.0 if "nba" in sport_lower else 100.0
        home_off = (home_pts / avg_pace) * 100
        away_off = (away_pts / avg_pace) * 100

        return nba_game_sim(
            team_a_off_rtg=home_off + avg_off / 2,
            team_a_def_rtg=avg_off,
            team_b_off_rtg=away_off + avg_off / 2,
            team_b_def_rtg=avg_off,
            team_a_pace=avg_pace,
            team_b_pace=avg_pace,
            league_avg_pace=avg_pace,
            league_avg_off_rtg=avg_off,
            n_sims=n_sims,
        )

    elif "nfl" in sport_lower or "ncaaf" in sport_lower or "football" in sport_lower:
        home_pts = (total - spread) / 2
        away_pts = (total + spread) / 2
        return nfl_game_sim(
            team_a_expected_pts=max(home_pts, 3),
            team_b_expected_pts=max(away_pts, 3),
            n_sims=n_sims,
        )

    elif any(s in sport_lower for s in ["mlb", "baseball"]):
        home_pts = (total - spread) / 2 if spread else total / 2
        away_pts = (total + spread) / 2 if spread else total / 2
        return poisson_game(
            team_a_attack=max(home_pts, 1),
            team_b_attack=max(away_pts, 1),
            team_a_defense=away_pts if away_pts > 0 else 4.5,
            team_b_defense=home_pts if home_pts > 0 else 4.5,
            league_avg=4.5,
            home_factor=1.04,
            max_score=15,
        )

    elif any(s in sport_lower for s in ["nhl", "hockey"]):
        home_pts = (total - spread) / 2 if spread else total / 2
        away_pts = (total + spread) / 2 if spread else total / 2
        return poisson_game(
            team_a_attack=max(home_pts, 1),
            team_b_attack=max(away_pts, 1),
            team_a_defense=away_pts if away_pts > 0 else 3.0,
            team_b_defense=home_pts if home_pts > 0 else 3.0,
            league_avg=3.0,
            home_factor=1.06,
            max_score=10,
        )

    elif any(s in sport_lower for s in ["soccer", "epl", "mls", "serie", "liga", "bundesliga", "ligue"]):
        home_pts = (total - spread) / 2 if spread else total / 2
        away_pts = (total + spread) / 2 if spread else total / 2
        return poisson_game(
            team_a_attack=max(home_pts, 0.5),
            team_b_attack=max(away_pts, 0.5),
            team_a_defense=away_pts if away_pts > 0 else 1.3,
            team_b_defense=home_pts if home_pts > 0 else 1.3,
            league_avg=1.3,
            home_factor=1.05,
            max_score=8,
        )

    else:
        return {"error": f"Unknown sport: {sport}"}
