"""
Change-point detection on performance time series (PELT + CUSUM).

Extracted from the original ``tools/regime.py`` (section 1 + data structures).
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

logger = logging.getLogger("callisto.regime")


# ---------------------------------------------------------------------------
# Data structure
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


# ---------------------------------------------------------------------------
# Change-point detection
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
        # Evaluate all *eligible* candidates (old enough to form a segment).
        # Candidates that are still too young must be carried forward —
        # dropping them here would permanently remove valid future
        # change points from the search.
        eligible = []
        deferred = []
        best_cost = INF
        best_s = 0

        for s in R:
            if t - s < min_segment:
                deferred.append(s)
                continue

            segment = data[s:t]
            cost = F[s] + _cost_normal(segment) + penalty

            if cost < best_cost:
                best_cost = cost
                best_s = s

            eligible.append((s, cost))

        F[t] = best_cost
        last_cp[t] = best_s

        # PELT pruning: discard candidates that can never be optimal again.
        # A candidate s is prunable if F[s] + cost(s, t) > F[t].
        # Because cost is additive, if it's already too expensive now,
        # adding more data won't help.
        R_new = []
        for s, c in eligible:
            if F[s] + _cost_normal(data[s:t]) <= F[t]:
                R_new.append(s)
        R_new.extend(deferred)
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
