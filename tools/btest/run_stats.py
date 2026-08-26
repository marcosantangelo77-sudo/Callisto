"""Statistical metrics for recalculating backtest run stats.

Extracted from tools/backtest.py (slice 2). Pure functions over the
deduplicated per-signal rows pulled from backtest_events — kept free of
DB access so they can be unit-tested directly.
"""

from __future__ import annotations


def compute_signal_metrics(
    wins: int,
    losses: int,
    expected_rate: float,
    signal_events: list[tuple],
) -> dict:
    """Compute statistical metrics for a set of decided signals.

    Args:
        wins / losses: decided W/L counts (pushes excluded).
        expected_rate: null-hypothesis win rate from avg book implied prob
            (NOT 0.5 coin-flip — 5W-0L on -300 favorites is not impressive).
        signal_events: deduplicated per-event rows of
            (odds_american, result_str, fair_prob, edge).

    Returns dict with p_binomial, p_ttest, z_score, sharpe, sortino, brier,
    ic, roi_pct.
    """
    total_decided = wins + losses
    p_binomial = 1.0
    p_ttest = 1.0
    z_score = 0.0
    sharpe = 0.0
    sortino = None
    brier = None
    ic = None
    roi_pct = 0.0

    if total_decided <= 0:
        return {
            "p_binomial": p_binomial,
            "p_ttest": p_ttest,
            "z_score": z_score,
            "sharpe": sharpe,
            "sortino": sortino,
            "brier": brier,
            "ic": ic,
            "roi_pct": roi_pct,
        }

    from scipy.stats import binomtest, ttest_1samp
    import numpy as np

    result = binomtest(wins, total_decided, expected_rate or 0.5, alternative="greater")
    p_binomial = result.pvalue

    returns = []
    brier_preds = []
    brier_outcomes = []
    predicted_edges = []
    realized_edges = []

    from tools.math_utils import american_to_decimal

    for odds_am, result_str, fair_prob, edge_val in signal_events:
        if result_str == "won" and odds_am:
            try:
                dec = american_to_decimal(odds_am)
                returns.append(dec - 1.0)
                if edge_val is not None:
                    predicted_edges.append(edge_val)
                    realized_edges.append(dec - 1.0)
            except Exception:
                returns.append(1.0)
        elif result_str == "lost":
            returns.append(-1.0)
            if edge_val is not None:
                predicted_edges.append(edge_val)
                realized_edges.append(-1.0)

        if fair_prob is not None:
            brier_preds.append(fair_prob)
            brier_outcomes.append(1 if result_str == "won" else 0)

    if len(returns) >= 2:
        arr = np.array(returns)
        t_stat, p_val = ttest_1samp(arr, 0)
        p_ttest = p_val / 2 if t_stat > 0 else 1 - p_val / 2
        z_score = t_stat
        sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
        neg = arr[arr < 0]
        if len(neg) > 0 and neg.std() > 0:
            sortino = float(arr.mean() / neg.std())

    if returns:
        roi_pct = sum(returns) / len(returns) * 100

    # Brier score
    if len(brier_preds) >= 2:
        bp = np.array(brier_preds)
        bo = np.array(brier_outcomes)
        brier = float(np.mean((bp - bo) ** 2))

    # Information coefficient (Pearson correlation between predicted and
    # realized edge)
    if len(predicted_edges) >= 3:
        pe = np.array(predicted_edges)
        re = np.array(realized_edges)
        if pe.std() > 0 and re.std() > 0:
            ic = float(np.corrcoef(pe, re)[0, 1])

    return {
        "p_binomial": p_binomial,
        "p_ttest": p_ttest,
        "z_score": z_score,
        "sharpe": sharpe,
        "sortino": sortino,
        "brier": brier,
        "ic": ic,
        "roi_pct": roi_pct,
    }


def summarize_run_stats(
    total_events: int,
    signals_count: int,
    raw_signals: int,
    results: dict[str, int],
    unresolved: int,
    metrics: dict,
    hit_rate: float | None,
    avg_edge=None,
    avg_ev=None,
    avg_clv=None,
) -> dict:
    """Assemble the backtest_runs UPDATE payload from computed pieces."""
    wins = results.get("won", 0)
    losses = results.get("lost", 0)
    pushes = results.get("push", 0)
    return {
        "total_events": total_events,
        "signals_generated": signals_count,
        "actual_win": wins,
        "actual_loss": losses,
        "actual_push": pushes,
        "unresolved": unresolved,
        "hit_rate": hit_rate,
        "avg_edge": avg_edge,
        "avg_ev": avg_ev,
        "avg_clv": avg_clv,
        "p_value_binomial": metrics["p_binomial"],
        "p_value_ttest": metrics["p_ttest"],
        "z_score": metrics["z_score"],
        "sharpe_ratio": metrics["sharpe"],
        "sortino_ratio_val": metrics["sortino"],
        "brier_score": metrics["brier"],
        "information_coefficient": metrics["ic"],
        "roi_pct": metrics["roi_pct"],
    }


def fingerprint_stale(cached_fp, current_fp) -> bool:
    """True when a run's (events, signals, resolved) fingerprint changed."""
    return cached_fp != current_fp


def prune_fingerprints(fingerprints: dict, active_run_ids: list[str], cap: int) -> dict:
    """Prune the fingerprint cache to active runs once it exceeds cap."""
    if len(fingerprints) <= cap:
        return fingerprints
    active_set = set(active_run_ids)
    return {k: v for k, v in fingerprints.items() if k in active_set}


__all__ = [
    "compute_signal_metrics",
    "summarize_run_stats",
    "fingerprint_stale",
    "prune_fingerprints",
]
