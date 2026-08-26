"""
tools.hypothesis.stats — pure-Python statistical helpers.

Split out of tools/hypothesis.py (facade re-exports everything).
No scipy dependency.
"""
from __future__ import annotations

import math


def _erfc(x: float) -> float:
    """Complementary error function approximation (Abramowitz & Stegun 7.1.26)."""
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
           t * (-1.453152027 + t * 1.061405429))))
    result = poly * math.exp(-x * x)
    return result if x >= 0 else 2.0 - result


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * _erfc(-x / math.sqrt(2))


def _norm_sf(x: float) -> float:
    """Standard normal survival function: P(Z > x)."""
    return 1.0 - _norm_cdf(x)


def _exact_binomial_sf(wins: int, total: int, p: float) -> float:
    """Exact binomial survival function: P(X >= wins | n=total, p).
    Used for small samples (n <= 30) where normal approximation is unreliable."""
    if wins <= 0:
        return 1.0
    if wins > total:
        return 0.0
    # P(X >= wins) = sum_{k=wins}^{total} C(n,k) * p^k * (1-p)^(n-k)
    prob = 0.0
    # Use log-space to avoid overflow for larger n
    log_comb = 0.0  # log(C(n, wins))
    for i in range(wins):
        log_comb += math.log(total - i) - math.log(i + 1)
    for k in range(wins, total + 1):
        if k > wins:
            log_comb += math.log(total - k + 1) - math.log(k)
        prob += math.exp(log_comb + k * math.log(p) + (total - k) * math.log(1 - p))
    return prob


def binomial_pvalue(wins: int, total: int, expected_rate: float) -> float:
    """
    One-sided binomial test.
    H0: true win rate = expected_rate
    H1: true win rate > expected_rate
    Uses exact binomial for n <= 30, normal approximation for n > 30.
    """
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 1.0
    if total <= 30:
        return _exact_binomial_sf(wins, total, expected_rate)
    mean = total * expected_rate
    std = math.sqrt(total * expected_rate * (1 - expected_rate))
    if std < 1e-9:
        return 1.0
    z = (wins - 0.5 - mean) / std
    return _norm_sf(z)


def ttest_one_sample(values: list[float]) -> tuple[float, float]:
    """
    One-sample t-test: is mean(values) significantly > 0?
    Returns (t_statistic, p_value).
    Uses normal approximation (valid for N > 30).
    """
    n = len(values)
    if n < 2:
        return 0.0, 1.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var < 1e-12:
        if abs(mean) < 1e-12:
            return 0.0, 1.0
        # Zero variance but nonzero mean: perfectly significant
        return float("inf") if mean > 0 else float("-inf"), 0.0 if mean > 0 else 1.0
    se = math.sqrt(var / n)
    t = mean / se
    p = _norm_sf(t)
    return t, p


def z_score(observed: int, total: int, expected_rate: float) -> float:
    """Z-score for observed vs expected proportion."""
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 0.0
    std = math.sqrt(expected_rate * (1 - expected_rate) / total)
    if std < 1e-9:
        return 0.0
    return (observed / total - expected_rate) / std


def sharpe_ratio(returns: list[float]) -> float:
    """Sharpe ratio (not annualized — daily or per-bet)."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0
    return mean / std if std > 1e-9 else 0.0


def max_drawdown(returns: list[float]) -> float:
    """Maximum drawdown from a series of per-bet returns.

    Uses a 100-unit starting bankroll so MDD is expressed as a fraction of
    capital, not peak cumulative profit.  The old code started at 0, which
    made early losses produce >100 % drawdown values — mathematically
    correct for a zero-start series but meaningless as a risk metric.
    """
    if not returns:
        return 0.0
    equity = 100.0          # 100-unit bankroll, flat $1 per signal
    peak = equity
    worst = 0.0
    for r in returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > worst:
            worst = dd
    return worst


def calibration_bins(
    predictions: list[tuple[float, bool]], n_bins: int = 10,
) -> list[dict]:
    """
    Bin predictions by predicted probability, compare to observed hit rate.
    Returns list of {bin_start, bin_end, count, predicted_avg, observed_rate}.
    """
    if not predictions:
        return []
    sorted_preds = sorted(predictions, key=lambda x: x[0])
    bin_size = max(len(sorted_preds) // n_bins, 1)
    bins = []
    for i in range(0, len(sorted_preds), bin_size):
        chunk = sorted_preds[i:i + bin_size]
        probs = [p for p, _ in chunk]
        outcomes = [o for _, o in chunk]
        bins.append({
            "bin_start": round(min(probs), 4),
            "bin_end": round(max(probs), 4),
            "count": len(chunk),
            "predicted_avg": round(sum(probs) / len(probs), 4),
            "observed_rate": round(sum(outcomes) / len(outcomes), 4) if outcomes else 0,
        })
    return bins


# ──────────────────────────────────────────────────
# HYPOTHESIS MANAGER
# ──────────────────────────────────────────────────
