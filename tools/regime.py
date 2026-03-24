"""
Regime detection and recency bias exploitation — finding real shifts vs noise.

The public (and books shading to public money) overreacts to recent results.
A team wins 3 straight → public perception inflates → line moves away from
true value → we bet the other side.

But sometimes a team IS genuinely better. A coaching change, key player
returning, scheme adjustment — these create real regime changes in the
underlying data. We need to separate signal from noise.

This module provides:
1. Change-point detection (PELT + CUSUM) on performance time series
2. Recency bias quantification — how much is perception diverging from reality
3. Regime-aware power ratings that weight recent regimes appropriately
4. Bayesian prior management — seasonal weight schedules for priors
5. Mean reversion detection — extreme performers tend to regress

All of this feeds the edge scanner: if we detect a regime change the
public hasn't priced in, that's alpha. If we detect the public
overweighting a streak with no underlying shift, that's also alpha.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import stats
from scipy.signal import argrelextrema

logger = logging.getLogger("callisto.regime")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChangePointResult:
    """Result from change-point detection."""
    indices: list[int]
    method: str
    n_segments: int
    segment_means: list[float]
    segment_variances: list[float]
    confidence: float  # 0-1, how confident we are these are real change points


@dataclass
class RecencyBiasResult:
    """Quantification of recency bias for a team."""
    bias_direction: str         # "overvalued" or "undervalued" by public
    bias_magnitude: float       # 0-1 scale, how severe the bias is
    mean_reversion_probability: float  # probability recent streak reverts
    recent_performance: float   # average of recent window
    underlying_performance: float  # average of full season metrics
    perception_gap: float       # signed difference (positive = public overvalues)


@dataclass
class PowerRating:
    """Regime-aware power rating for a team."""
    rating: float
    regime: str           # "improving", "declining", "stable", "volatile"
    confidence: float     # 0-1
    regime_rating: float  # rating from current regime only
    season_rating: float  # full season rating
    regime_start_index: int  # when current regime began
    regime_games: int     # how many games in current regime


@dataclass
class BayesianResult:
    """Result of Bayesian prior update."""
    posterior: float
    prior_decay_applied: float   # how much the prior was decayed
    prior_contribution: float    # portion of posterior from prior
    evidence_contribution: float  # portion from evidence
    credible_interval: tuple[float, float]  # 90% credible interval


@dataclass
class MeanReversionSignal:
    """Signal for expected mean reversion."""
    reversion_expected: bool
    magnitude: float       # expected size of reversion (in metric units)
    confidence: float      # 0-1
    current_zscore: float  # how many SDs from historical mean
    historical_mean: float
    current_value: float
    half_life_games: float  # estimated games until halfway reverted


# ---------------------------------------------------------------------------
# 1. Change-point detection
# ---------------------------------------------------------------------------

def _cost_normal(data: np.ndarray) -> float:
    """
    Cost function for normally-distributed data segment.

    Negative log-likelihood of the segment under a normal model:
    n/2 * log(variance) where variance is the MLE estimate.
    This is the standard cost for PELT with Gaussian assumptions.
    """
    n = len(data)
    if n <= 1:
        return 0.0
    variance = np.var(data)
    if variance < 1e-12:
        return 0.0
    return (n / 2.0) * np.log(variance)


def _pelt_search(data: np.ndarray, penalty: float, min_segment: int = 3) -> list[int]:
    """
    PELT (Pruned Exact Linear Time) algorithm for change-point detection.

    Killick, Fearnhead, Eckley (2012). "Optimal Detection of Changepoints
    with a Linear Computational Cost."

    The key insight: if adding a candidate change point can never improve
    the segmentation cost in the future, prune it from the candidate set.
    This gives O(n) expected complexity vs O(n^2) for exact search.

    Parameters:
        data: 1D array of observations
        penalty: BIC-like penalty per change point (controls sensitivity)
        min_segment: minimum segment length to prevent overfitting

    Returns:
        List of change-point indices (positions where new segment begins)
    """
    n = len(data)
    if n < 2 * min_segment:
        return []

    # F[t] = optimal cost of segmenting data[0:t]
    # last_cp[t] = last change point in optimal segmentation ending at t
    INF = float("inf")
    F = np.full(n + 1, INF)
    F[0] = -penalty  # base case: no data, offset by one penalty

    last_cp = np.zeros(n + 1, dtype=int)
    # R = set of candidate change-point positions (pruned over time)
    R = [0]

    for t in range(min_segment, n + 1):
        candidates = []
        best_cost = INF
        best_s = 0

        for s in R:
            if t - s < min_segment:
                continue

            segment = data[s:t]
            cost = F[s] + _cost_normal(segment) + penalty

            if cost < best_cost:
                best_cost = cost
                best_s = s

            candidates.append((s, cost))

        F[t] = best_cost
        last_cp[t] = best_s

        # PELT pruning: discard candidates that can never be optimal again.
        # A candidate s is prunable if F[s] + cost(s, t) > F[t].
        # Because cost is additive, if it's already too expensive now,
        # adding more data won't help.
        R_new = []
        for s, c in candidates:
            if F[s] + _cost_normal(data[s:t]) <= F[t]:
                R_new.append(s)
        R_new.append(t)
        R = R_new

    # Backtrack to recover change points
    cps = []
    idx = n
    while idx > 0:
        cp = last_cp[idx]
        if cp > 0:
            cps.append(cp)
        idx = cp

    cps.sort()
    return cps


def _cusum_search(data: np.ndarray, threshold: float = 1.5,
                  drift: float = 0.0) -> list[int]:
    """
    CUSUM (Cumulative Sum) change-point detection.

    Page (1954). Simpler and more robust than PELT for single
    change-point detection. We run it iteratively for multiple points.

    Tracks cumulative deviation from the running mean. When the
    cumulative sum exceeds a threshold, a change point is flagged.

    Parameters:
        data: 1D array of observations
        threshold: detection threshold in standard deviations
        drift: allowance parameter (tolerance before flagging)

    Returns:
        List of change-point indices
    """
    n = len(data)
    if n < 4:
        return []

    mean = np.mean(data)
    std = np.std(data)
    if std < 1e-12:
        return []

    # Normalize
    z = (data - mean) / std

    # Track positive and negative cumulative sums
    s_pos = np.zeros(n)
    s_neg = np.zeros(n)
    change_points = []

    for i in range(1, n):
        s_pos[i] = max(0, s_pos[i - 1] + z[i] - drift)
        s_neg[i] = max(0, s_neg[i - 1] - z[i] - drift)

        if s_pos[i] > threshold or s_neg[i] > threshold:
            change_points.append(i)
            # Reset after detection
            s_pos[i] = 0
            s_neg[i] = 0

    return change_points


def detect_regime_change(performance_data: list[float],
                         method: str = "pelt",
                         penalty: Optional[float] = None,
                         threshold: Optional[float] = None,
                         min_segment: int = 3) -> list[int]:
    """
    Detect regime changes in team performance time series.

    A regime change means the underlying data-generating process shifted.
    Examples: new scheme installed, key player returns from injury,
    coaching change, or a team just "clicking."

    Parameters:
        performance_data: Time series of efficiency metrics. Could be:
            - Points per possession (basketball)
            - Yards per play (football)
            - Expected goals per game (soccer/hockey)
            - Runs per game / FIP (baseball)
        method: "pelt" (preferred, exact) or "cusum" (faster, simpler)
        penalty: For PELT — higher = fewer change points. Default uses BIC.
        threshold: For CUSUM — higher = fewer change points. Default 1.5 SD.
        min_segment: Minimum games in a regime segment.

    Returns:
        List of indices where regime changes were detected.
        Empty list means no significant regime changes found.
    """
    if len(performance_data) < 2 * min_segment:
        logger.debug("Not enough data for regime detection (%d points, need %d)",
                     len(performance_data), 2 * min_segment)
        return []

    data = np.array(performance_data, dtype=float)

    if method == "pelt":
        if penalty is None:
            # BIC-style penalty: log(n) * variance_estimate
            # This is the standard choice — balances fit vs complexity
            n = len(data)
            penalty = np.log(n) * np.var(data)
            # Floor the penalty so we don't get degenerate results on
            # near-constant data
            penalty = max(penalty, 0.1)

        cps = _pelt_search(data, penalty, min_segment)

    elif method == "cusum":
        if threshold is None:
            threshold = 1.5
        cps = _cusum_search(data, threshold)

    else:
        raise ValueError(f"Unknown method '{method}'. Use 'pelt' or 'cusum'.")

    if cps:
        logger.info("Detected %d regime change(s) at indices %s using %s",
                     len(cps), cps, method)
    else:
        logger.debug("No regime changes detected using %s", method)

    return cps


def analyze_regimes(performance_data: list[float],
                    method: str = "pelt",
                    **kwargs) -> ChangePointResult:
    """
    Full regime analysis: detect change points and characterize each segment.

    Returns a ChangePointResult with segment statistics and confidence.
    """
    data = np.array(performance_data, dtype=float)
    cps = detect_regime_change(performance_data, method=method, **kwargs)

    # Build segments
    boundaries = [0] + cps + [len(data)]
    segment_means = []
    segment_variances = []

    for i in range(len(boundaries) - 1):
        seg = data[boundaries[i]:boundaries[i + 1]]
        segment_means.append(float(np.mean(seg)))
        segment_variances.append(float(np.var(seg)))

    # Confidence: based on how distinct adjacent segments are.
    # Use Welch's t-test between adjacent segments; average the p-values.
    if len(segment_means) > 1:
        p_values = []
        for i in range(len(boundaries) - 2):
            seg_a = data[boundaries[i]:boundaries[i + 1]]
            seg_b = data[boundaries[i + 1]:boundaries[i + 2]]
            if len(seg_a) >= 2 and len(seg_b) >= 2:
                _, p = stats.ttest_ind(seg_a, seg_b, equal_var=False)
                p_values.append(p)

        if p_values:
            # Convert average p-value to confidence
            avg_p = np.mean(p_values)
            confidence = float(1.0 - avg_p)
        else:
            confidence = 0.5
    else:
        confidence = 0.0  # no change points → no confidence in regime change

    return ChangePointResult(
        indices=cps,
        method=method,
        n_segments=len(segment_means),
        segment_means=segment_means,
        segment_variances=segment_variances,
        confidence=round(confidence, 4),
    )


# ---------------------------------------------------------------------------
# 2. Recency bias quantification
# ---------------------------------------------------------------------------

def recency_bias_score(recent_results: list,
                       season_results: list,
                       metric_key: Optional[str] = None) -> dict:
    """
    Quantify how much the public is likely overweighting recent results.

    The logic:
    - Public sees W-W-W and thinks "hot team" → bets them
    - Books shade the line toward public money
    - If the underlying metrics (efficiency, expected points, etc.) haven't
      actually improved, the line is inflated → value on the other side

    Parameters:
        recent_results: Last N games' performance metrics (typically 3-5 games).
            Can be floats (raw metric) or dicts with a metric_key field.
        season_results: Full season performance metrics (same format).
        metric_key: If results are dicts, which key to extract.

    Returns:
        Dict with bias_direction, bias_magnitude, mean_reversion_probability.
    """
    # Extract numeric values
    if metric_key and isinstance(recent_results[0], dict):
        recent_vals = np.array([g[metric_key] for g in recent_results], dtype=float)
        season_vals = np.array([g[metric_key] for g in season_results], dtype=float)
    else:
        recent_vals = np.array(recent_results, dtype=float)
        season_vals = np.array(season_results, dtype=float)

    recent_mean = float(np.mean(recent_vals))
    season_mean = float(np.mean(season_vals))
    season_std = float(np.std(season_vals))

    if season_std < 1e-12:
        # No variance in season data — can't assess bias
        return RecencyBiasResult(
            bias_direction="neutral",
            bias_magnitude=0.0,
            mean_reversion_probability=0.5,
            recent_performance=recent_mean,
            underlying_performance=season_mean,
            perception_gap=0.0,
        ).__dict__

    # Z-score of recent window vs season distribution
    z = (recent_mean - season_mean) / season_std

    # Bias magnitude: how extreme is the recent window?
    # Use the CDF to convert z-score to a 0-1 scale.
    # |z| > 1 means the recent window is notably different from season average.
    raw_magnitude = float(2 * abs(stats.norm.cdf(z) - 0.5))  # 0 to 1

    # Scale it: small samples amplify randomness.
    # Fewer recent games → more likely to be noise → higher bias potential.
    sample_noise_factor = 1.0 / math.sqrt(len(recent_vals))
    # If the recent divergence is large relative to what we'd expect from
    # random sampling, the public is probably overreacting.
    expected_divergence = season_std * sample_noise_factor
    actual_divergence = abs(recent_mean - season_mean)
    divergence_ratio = actual_divergence / expected_divergence if expected_divergence > 0 else 0.0

    # Bias magnitude: clamp to 0-1
    bias_magnitude = float(min(1.0, raw_magnitude * min(divergence_ratio / 2.0, 1.5)))

    # Direction: if recent > season, public overvalues → line inflated
    if z > 0.25:
        bias_direction = "overvalued"
    elif z < -0.25:
        bias_direction = "undervalued"
    else:
        bias_direction = "neutral"

    # Mean reversion probability: how likely is the recent streak to revert?
    # Based on the empirical observation that extreme performance regresses.
    # Use a logistic function of the z-score.
    # At z=0, P(reversion) = 0.5 (coin flip). At z=2, P ≈ 0.88.
    reversion_prob = float(1.0 / (1.0 + math.exp(-0.8 * abs(z))))

    # Also check: did the underlying distribution actually shift?
    # Run a quick t-test of recent vs rest-of-season.
    rest_of_season = np.array([v for v in season_vals
                               if v not in recent_vals[:3]], dtype=float)
    if len(rest_of_season) >= 3 and len(recent_vals) >= 3:
        _, p_shift = stats.ttest_ind(recent_vals, rest_of_season, equal_var=False)
        # If p < 0.05, the shift might be real → lower reversion probability
        if p_shift < 0.05:
            reversion_prob *= 0.6  # real shift detected, reduce reversion expectation
            bias_magnitude *= 0.5  # less bias if shift is genuine

    perception_gap = float(recent_mean - season_mean)

    result = RecencyBiasResult(
        bias_direction=bias_direction,
        bias_magnitude=round(bias_magnitude, 4),
        mean_reversion_probability=round(reversion_prob, 4),
        recent_performance=round(recent_mean, 4),
        underlying_performance=round(season_mean, 4),
        perception_gap=round(perception_gap, 4),
    )

    logger.info("Recency bias: %s (magnitude=%.3f, reversion_prob=%.3f)",
                result.bias_direction, result.bias_magnitude,
                result.mean_reversion_probability)

    return result.__dict__


# ---------------------------------------------------------------------------
# 3. Regime-aware power ratings
# ---------------------------------------------------------------------------

def _classify_regime(segment_means: list[float], current_variance: float,
                     season_std: float) -> str:
    """
    Classify the current regime based on segment trajectory.

    Returns: "improving", "declining", "stable", or "volatile"
    """
    if len(segment_means) < 2:
        return "stable"

    # Look at the last two segments
    prev = segment_means[-2]
    curr = segment_means[-1]
    diff = curr - prev

    # Volatile: if current segment variance is much higher than season
    if current_variance > 0 and season_std > 0:
        if math.sqrt(current_variance) > 1.5 * season_std:
            return "volatile"

    # Threshold for meaningful change: 0.5 season SDs
    threshold = 0.5 * season_std if season_std > 0 else abs(diff) * 0.3

    if diff > threshold:
        return "improving"
    elif diff < -threshold:
        return "declining"
    else:
        return "stable"


def calculate_power_rating(team_data: dict,
                           regime_weight: float = 0.6) -> dict:
    """
    Calculate a regime-aware power rating for a team.

    Instead of blindly averaging the whole season, we:
    1. Detect regime changes in the performance data
    2. Weight the current regime more heavily
    3. But also check if the "regime change" passes statistical tests

    Parameters:
        team_data: Dict with:
            - "performance_history": list[float] — game-by-game efficiency
            - "name": str — team name (for logging)
            - "league_avg": float (optional) — league average for normalization
        regime_weight: How much to weight the current regime vs full season.
            0.6 = 60% current regime, 40% full season. Adjustable.

    Returns:
        Dict with rating, regime, confidence, component ratings.
    """
    history = team_data.get("performance_history", [])
    name = team_data.get("name", "Unknown")
    league_avg = team_data.get("league_avg", None)

    if not history or len(history) < 4:
        season_mean = float(np.mean(history)) if history else 0.0
        return PowerRating(
            rating=round(season_mean, 4),
            regime="insufficient_data",
            confidence=0.0,
            regime_rating=season_mean,
            season_rating=season_mean,
            regime_start_index=0,
            regime_games=len(history),
        ).__dict__

    data = np.array(history, dtype=float)
    season_mean = float(np.mean(data))
    season_std = float(np.std(data))

    # Detect regime changes
    regime_result = analyze_regimes(history)

    if regime_result.indices:
        # Current regime starts at the last change point
        last_cp = regime_result.indices[-1]
        current_regime_data = data[last_cp:]
        regime_mean = float(np.mean(current_regime_data))
        regime_var = float(np.var(current_regime_data))
        regime_games = len(current_regime_data)

        # Classify the regime
        regime_label = _classify_regime(
            regime_result.segment_means, regime_var, season_std
        )

        # Adjust regime weight by confidence.
        # If the change point isn't very confident, lean more on season average.
        effective_weight = regime_weight * regime_result.confidence
        effective_weight = max(0.2, min(0.9, effective_weight))

        # Also adjust by regime sample size — tiny regimes get less weight
        sample_factor = min(1.0, regime_games / 8.0)  # full weight at 8+ games
        effective_weight *= sample_factor
        effective_weight = max(0.2, effective_weight)  # floor

        # Blended rating
        rating = effective_weight * regime_mean + (1 - effective_weight) * season_mean

        # Confidence in the rating: based on regime detection confidence
        # and sample size
        confidence = regime_result.confidence * sample_factor

    else:
        # No regime change detected — use full season with slight recency tilt
        # Apply exponential weighting: recent games matter a bit more
        n = len(data)
        weights = np.exp(np.linspace(-1, 0, n))  # gentle exponential decay
        weights /= weights.sum()
        rating = float(np.dot(data, weights))

        regime_label = "stable"
        regime_mean = season_mean
        regime_games = len(data)
        last_cp = 0
        confidence = 0.7  # moderate confidence in stable regime

    # Normalize to league average if provided
    if league_avg is not None and league_avg != 0:
        rating = rating - league_avg  # positive = above average

    result = PowerRating(
        rating=round(rating, 4),
        regime=regime_label,
        confidence=round(confidence, 4),
        regime_rating=round(regime_mean, 4),
        season_rating=round(season_mean, 4),
        regime_start_index=last_cp,
        regime_games=regime_games,
    )

    logger.info("%s power rating: %.2f (regime=%s, confidence=%.2f)",
                name, result.rating, result.regime, result.confidence)

    return result.__dict__


# ---------------------------------------------------------------------------
# 4. Bayesian prior management
# ---------------------------------------------------------------------------

def prior_weight_schedule(games_played: int, sport: str = "nba") -> float:
    """
    How much to weight priors vs current-season data.

    Early season: lean heavily on priors (last year + offseason changes).
    Mid season: balanced.
    Late season: mostly current data.

    The schedule follows a logistic decay — the transition from prior-heavy
    to data-heavy is smooth, not a hard cutoff.

    Parameters:
        games_played: Number of games completed this season.
        sport: Sport determines the season length and transition points.

    Returns:
        Float 0-1. 1.0 = full prior weight, 0.0 = ignore prior entirely.
    """
    # Season lengths and transition midpoints by sport
    schedules = {
        "nba": {"season_length": 82, "midpoint": 25, "steepness": 0.12},
        "nfl": {"season_length": 17, "midpoint": 6, "steepness": 0.5},
        "mlb": {"season_length": 162, "midpoint": 50, "steepness": 0.06},
        "nhl": {"season_length": 82, "midpoint": 25, "steepness": 0.12},
        "ncaab": {"season_length": 35, "midpoint": 12, "steepness": 0.25},
        "ncaaf": {"season_length": 12, "midpoint": 4, "steepness": 0.6},
        "soccer": {"season_length": 38, "midpoint": 12, "steepness": 0.2},
    }

    params = schedules.get(sport.lower(), schedules["nba"])
    midpoint = params["midpoint"]
    steepness = params["steepness"]

    # Logistic decay: starts near 1.0, transitions to near 0.0
    # w = 1 / (1 + exp(steepness * (games - midpoint)))
    prior_w = 1.0 / (1.0 + math.exp(steepness * (games_played - midpoint)))

    # Floor at 0.05 — never completely ignore priors
    # (even late-season, a small prior prevents overfitting to recent noise)
    prior_w = max(0.05, prior_w)

    return round(prior_w, 4)


def bayesian_update(prior: float, evidence: list[float],
                    prior_weight: float = 0.5,
                    prior_variance: Optional[float] = None) -> dict:
    """
    Bayesian update of a team rating given new evidence.

    Uses conjugate normal-normal model:
    - Prior: N(prior, prior_variance)
    - Likelihood: N(evidence_mean, evidence_variance / n)
    - Posterior: weighted combination

    Parameters:
        prior: Prior estimate (e.g., last season's rating + offseason adjustments).
        evidence: List of observed values this season.
        prior_weight: 0-1, how much to weight the prior. Use prior_weight_schedule().
        prior_variance: Uncertainty in the prior. If None, estimated from evidence.

    Returns:
        Dict with posterior, prior_decay_applied, contributions, credible interval.
    """
    if not evidence:
        return BayesianResult(
            posterior=prior,
            prior_decay_applied=0.0,
            prior_contribution=1.0,
            evidence_contribution=0.0,
            credible_interval=(prior - 2.0, prior + 2.0),
        ).__dict__

    ev = np.array(evidence, dtype=float)
    ev_mean = float(np.mean(ev))
    ev_var = float(np.var(ev, ddof=1)) if len(ev) > 1 else 1.0
    n = len(ev)

    # Estimate prior variance if not provided
    if prior_variance is None:
        # Use evidence variance as a rough prior variance estimate
        # Scale up because prior uncertainty is typically larger
        prior_variance = ev_var * 2.0
    prior_variance = max(prior_variance, 1e-6)

    # Precision (inverse variance) formulation for conjugate update
    prior_precision = prior_weight / prior_variance
    evidence_precision = (1.0 - prior_weight) * n / ev_var if ev_var > 0 else 0.0
    total_precision = prior_precision + evidence_precision

    if total_precision < 1e-12:
        posterior = (prior + ev_mean) / 2.0
        posterior_var = prior_variance
    else:
        # Posterior mean: precision-weighted average
        posterior = (prior_precision * prior + evidence_precision * ev_mean) / total_precision
        posterior_var = 1.0 / total_precision

    # Contributions
    prior_contribution = prior_precision / total_precision if total_precision > 0 else 0.5
    evidence_contribution = 1.0 - prior_contribution

    # 90% credible interval
    posterior_std = math.sqrt(posterior_var)
    z90 = 1.645
    ci_low = posterior - z90 * posterior_std
    ci_high = posterior + z90 * posterior_std

    # Prior decay: how much the prior was discounted from its original value
    prior_decay = 1.0 - prior_weight

    result = BayesianResult(
        posterior=round(posterior, 4),
        prior_decay_applied=round(prior_decay, 4),
        prior_contribution=round(prior_contribution, 4),
        evidence_contribution=round(evidence_contribution, 4),
        credible_interval=(round(ci_low, 4), round(ci_high, 4)),
    )

    logger.debug("Bayesian update: prior=%.2f → posterior=%.2f "
                 "(prior_contrib=%.2f, evidence_contrib=%.2f)",
                 prior, result.posterior,
                 result.prior_contribution, result.evidence_contribution)

    return result.__dict__


def seasonal_bayesian_rating(prior_rating: float,
                             game_metrics: list[float],
                             sport: str = "nba",
                             prior_variance: Optional[float] = None) -> dict:
    """
    Convenience function: applies prior_weight_schedule + bayesian_update.

    Takes a prior (e.g., preseason projection) and the season's game-by-game
    metrics, automatically determines the right prior/evidence balance based
    on how many games have been played, and returns the updated rating.

    Parameters:
        prior_rating: Preseason or prior-year rating.
        game_metrics: This season's game-by-game performance values.
        sport: For weight schedule calculation.
        prior_variance: Prior uncertainty. If None, estimated.

    Returns:
        BayesianResult dict with the updated rating.
    """
    games_played = len(game_metrics)
    pw = prior_weight_schedule(games_played, sport)
    return bayesian_update(prior_rating, game_metrics, prior_weight=pw,
                           prior_variance=prior_variance)


# ---------------------------------------------------------------------------
# 5. Mean reversion detection
# ---------------------------------------------------------------------------

def mean_reversion_signal(team_metric_history: list[float],
                          league_avg: float,
                          historical_mean: Optional[float] = None,
                          regression_rate: float = 0.5) -> dict:
    """
    Detect whether a team is likely to regress toward the mean.

    Sports metrics revert to the mean. Teams performing far above or below
    their historical baseline tend to revert — the question is how fast
    and how much.

    Key insight for betting: if a team is 2 SDs above their norm, the
    public prices them as if they'll stay there. We price in the reversion.

    Parameters:
        team_metric_history: Time series of the team's metric values.
            At least 10 data points recommended for reliability.
        league_avg: League-wide average for this metric.
        historical_mean: Team's long-term average. If None, uses the mean
            of team_metric_history.
        regression_rate: How fast metrics regress. 0.5 = regress halfway
            to the mean each "period." Sport-specific:
            - NBA shooting %: ~0.4-0.6
            - NFL passer rating: ~0.3-0.5
            - MLB BABIP: ~0.6-0.8 (high regression)
            - Soccer xG: ~0.4-0.6

    Returns:
        Dict with reversion_expected, magnitude, confidence, z-score, etc.
    """
    if len(team_metric_history) < 3:
        return MeanReversionSignal(
            reversion_expected=False,
            magnitude=0.0,
            confidence=0.0,
            current_zscore=0.0,
            historical_mean=league_avg,
            current_value=float(np.mean(team_metric_history)) if team_metric_history else 0.0,
            half_life_games=float("inf"),
        ).__dict__

    data = np.array(team_metric_history, dtype=float)
    current_value = float(np.mean(data[-5:]))  # recent 5-game window

    if historical_mean is None:
        historical_mean = float(np.mean(data))

    # Blend historical mean with league average
    # The "true talent" is somewhere between the team's history and league avg
    # More data → lean more on team history. Less data → lean on league avg.
    sample_reliability = min(1.0, len(data) / 30.0)  # full reliability at 30+ games
    true_mean = sample_reliability * historical_mean + (1 - sample_reliability) * league_avg

    # Standard deviation from history
    hist_std = float(np.std(data))
    if hist_std < 1e-12:
        hist_std = abs(current_value - true_mean) * 0.5
        if hist_std < 1e-12:
            return MeanReversionSignal(
                reversion_expected=False,
                magnitude=0.0,
                confidence=0.0,
                current_zscore=0.0,
                historical_mean=round(true_mean, 4),
                current_value=round(current_value, 4),
                half_life_games=float("inf"),
            ).__dict__

    # Z-score: how far is current performance from expected true level?
    z = (current_value - true_mean) / hist_std

    # Expected reversion magnitude
    # Based on regression_rate: how much of the deviation we expect to revert
    deviation = current_value - true_mean
    expected_reversion = deviation * regression_rate
    reversion_magnitude = abs(expected_reversion)

    # Half-life in games: how quickly does this metric regress?
    # Half-life = -1 / log2(1 - regression_rate)
    if regression_rate > 0 and regression_rate < 1:
        half_life = -1.0 / math.log2(1.0 - regression_rate)
    else:
        half_life = float("inf")

    # Is reversion expected?
    # Yes if z-score is extreme enough (> 1 SD from mean)
    reversion_expected = abs(z) > 1.0

    # Confidence: based on z-score magnitude and sample size
    # Higher z → more confidence reversion will happen
    # More data → more confidence in the z-score
    z_confidence = float(2.0 * abs(stats.norm.cdf(z) - 0.5))  # 0 to 1
    sample_confidence = min(1.0, len(data) / 20.0)
    confidence = z_confidence * sample_confidence

    # Additional check: is the trend accelerating or decelerating?
    # If recent games are moving BACK toward the mean, reversion is already
    # underway → signal is less actionable.
    if len(data) >= 8:
        recent_half = data[len(data) // 2:]
        earlier_half = data[:len(data) // 2]
        recent_dev = abs(float(np.mean(recent_half)) - true_mean)
        earlier_dev = abs(float(np.mean(earlier_half)) - true_mean)
        if recent_dev < earlier_dev * 0.7:
            # Already reverting — reduce signal
            confidence *= 0.5
            reversion_magnitude *= 0.5

    result = MeanReversionSignal(
        reversion_expected=reversion_expected,
        magnitude=round(reversion_magnitude, 4),
        confidence=round(confidence, 4),
        current_zscore=round(z, 4),
        historical_mean=round(true_mean, 4),
        current_value=round(current_value, 4),
        half_life_games=round(half_life, 2),
    )

    if reversion_expected:
        direction = "downward" if z > 0 else "upward"
        logger.info("Mean reversion signal: %s (z=%.2f, magnitude=%.3f, confidence=%.2f)",
                     direction, z, reversion_magnitude, confidence)

    return result.__dict__


# ---------------------------------------------------------------------------
# Composite: full regime analysis for a team
# ---------------------------------------------------------------------------

def full_regime_analysis(team_data: dict, sport: str = "nba") -> dict:
    """
    Run the complete regime analysis pipeline for a single team.

    Combines all five modules into a single assessment:
    1. Detect regime changes in performance data
    2. Quantify recency bias
    3. Calculate regime-aware power rating
    4. Apply Bayesian prior management
    5. Check for mean reversion signals

    Parameters:
        team_data: Dict with:
            - "name": str
            - "performance_history": list[float] — game-by-game efficiency
            - "prior_rating": float (optional) — preseason/prior-year rating
            - "league_avg": float (optional) — league average
            - "recent_window": int (optional) — games for recency analysis (default 5)

    Returns:
        Dict with all analysis results combined.
    """
    name = team_data.get("name", "Unknown")
    history = team_data.get("performance_history", [])
    prior_rating = team_data.get("prior_rating")
    league_avg = team_data.get("league_avg", float(np.mean(history)) if history else 0.0)
    recent_window = team_data.get("recent_window", 5)

    logger.info("Running full regime analysis for %s (%d games)", name, len(history))

    results = {"team": name, "games_analyzed": len(history)}

    # 1. Regime detection
    results["regime_changes"] = analyze_regimes(history).__dict__ if len(history) >= 6 else None

    # 2. Recency bias
    if len(history) >= recent_window + 3:
        recent = history[-recent_window:]
        results["recency_bias"] = recency_bias_score(recent, history)
    else:
        results["recency_bias"] = None

    # 3. Power rating
    results["power_rating"] = calculate_power_rating(team_data)

    # 4. Bayesian update
    if prior_rating is not None:
        results["bayesian_rating"] = seasonal_bayesian_rating(
            prior_rating, history, sport=sport
        )
    else:
        results["bayesian_rating"] = None

    # 5. Mean reversion
    if len(history) >= 8:
        results["mean_reversion"] = mean_reversion_signal(
            history, league_avg
        )
    else:
        results["mean_reversion"] = None

    # Composite signal: combine everything into an actionable summary
    signals = []
    if results["recency_bias"] and results["recency_bias"]["bias_magnitude"] > 0.3:
        signals.append(f"recency_bias_{results['recency_bias']['bias_direction']}")
    if results["mean_reversion"] and results["mean_reversion"]["reversion_expected"]:
        direction = "down" if results["mean_reversion"]["current_zscore"] > 0 else "up"
        signals.append(f"mean_reversion_{direction}")
    if results["power_rating"]["regime"] in ("improving", "declining"):
        signals.append(f"regime_{results['power_rating']['regime']}")

    results["actionable_signals"] = signals
    results["has_edge_signal"] = len(signals) > 0

    logger.info("%s analysis complete: %d actionable signal(s): %s",
                name, len(signals), signals)

    return results
