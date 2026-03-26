"""
Market microstructure metrics for betting edge quality assessment.

Quantitative measures of market structure that improve edge confidence scoring:
  - HHI (Herfindahl-Hirschman Index): book concentration per market
  - Shannon Entropy: line distribution uncertainty across books
  - Sortino Ratio: downside-only risk metric for hypothesis returns
  - Brier Score: probability calibration quality
  - Information Coefficient: predicted vs realized edge correlation

These metrics answer: "Is this edge real, or is the market just noisy?"
"""

import logging
import math
from typing import Optional

logger = logging.getLogger("callisto.market_microstructure")


# ── Core Metrics ──


def hhi(shares: list[float]) -> float:
    """
    Herfindahl-Hirschman Index for market concentration.

    Args:
        shares: list of market shares (should sum to ~1.0).
                For betting: each book's implied probability share of the total.

    Returns:
        HHI on 0-10000 scale.
        < 1500 = competitive (books agree, divergence is meaningful signal)
        1500-2500 = moderate concentration
        > 2500 = concentrated (fewer books or one dominates, edge may be noise)
        10000 = monopoly (single book)

    Example: 5 books each at 0.20 share → HHI = 2000
             3 books at 0.33 each → HHI = 3333
             10 books at 0.10 each → HHI = 1000
    """
    if not shares:
        return 10000.0
    return sum(s ** 2 for s in shares) * 10000


def shannon_entropy(probs: list[float]) -> float:
    """
    Shannon entropy of a probability distribution.

    Measures uncertainty/disagreement in the distribution.
    Applied to: distribution of implied probabilities across books for the same outcome.

    Args:
        probs: list of probabilities (need not sum to 1; will be normalized).

    Returns:
        Entropy in bits. Higher = more disagreement among books.
        0.0 = all books identical (no information)
        log2(N) = maximum entropy (uniform distribution, total disagreement)

    For typical 5-book markets:
        < 0.5 bits = strong agreement (edge is likely closing or noise)
        0.5-2.0 bits = moderate disagreement (normal market)
        > 2.0 bits = high disagreement (genuine opportunity window)
    """
    if not probs:
        return 0.0
    total = sum(probs)
    if total <= 0:
        return 0.0
    normalized = [p / total for p in probs if p > 0]
    return -sum(p * math.log2(p) for p in normalized)


def sortino_ratio(returns: list[float], target: float = 0.0) -> Optional[float]:
    """
    Sortino ratio — risk-adjusted return penalizing only downside deviation.

    Unlike Sharpe (which penalizes ALL variance including upside), Sortino only
    penalizes losses. Better for betting where high-variance wins are desirable.

    Args:
        returns: list of per-bet or per-period returns (e.g., [0.05, -0.10, 0.20])
        target: minimum acceptable return (default 0.0 = breakeven)

    Returns:
        Sortino ratio (higher = better risk-adjusted return).
        None if insufficient data.
        > 1.0 = good
        > 2.0 = excellent
    """
    if len(returns) < 2:
        return None
    excess = [r - target for r in returns]
    mean_excess = sum(excess) / len(excess)
    downside_sq = [min(0, r) ** 2 for r in excess]
    downside_dev = math.sqrt(sum(downside_sq) / len(downside_sq))
    if downside_dev < 1e-9:
        return None if mean_excess <= 0 else float("inf")
    return mean_excess / downside_dev


def brier_score(predictions: list[float], outcomes: list[int]) -> Optional[float]:
    """
    Brier score — mean squared error of probability predictions.

    Measures calibration quality: are our devigged fair probabilities accurate?

    Args:
        predictions: predicted probabilities (0.0-1.0)
        outcomes: actual binary outcomes (0 or 1)

    Returns:
        Brier score on 0-1 scale. Lower = better calibration.
        0.0 = perfect predictions
        0.25 = coin-flip baseline (predict 0.5 for everything)
        1.0 = maximally wrong (predict 1.0, always get 0)
        None if no data.
    """
    if not predictions or len(predictions) != len(outcomes):
        return None
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def information_coefficient(
    predicted_edges: list[float],
    realized_edges: list[float],
) -> Optional[float]:
    """
    Information coefficient — Pearson correlation between predicted and realized edges.

    Answers: "Do our predicted edges predict actual outcomes?"

    Args:
        predicted_edges: edge % at signal time
        realized_edges: edge % at closing line (or actual outcome edge)

    Returns:
        Pearson correlation coefficient (-1 to 1).
        > 0.05 = good for betting (small correlations are profitable at scale)
        > 0.10 = excellent
        < 0.0 = model is anti-predictive
        None if insufficient data.
    """
    n = len(predicted_edges)
    if n < 3 or n != len(realized_edges):
        return None

    mean_p = sum(predicted_edges) / n
    mean_r = sum(realized_edges) / n

    cov = sum((p - mean_p) * (r - mean_r) for p, r in zip(predicted_edges, realized_edges)) / n
    std_p = math.sqrt(sum((p - mean_p) ** 2 for p in predicted_edges) / n)
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in realized_edges) / n)

    if std_p < 1e-9 or std_r < 1e-9:
        return None
    return cov / (std_p * std_r)


# ── Snapshot-Level Computation ──


def compute_market_metrics(
    implied_probs: list[float],
    book_names: list[str],
    sharp_books: set[str],
) -> dict:
    """
    Compute HHI and entropy for a single market from a snapshot.

    Args:
        implied_probs: implied probability per book for one outcome
        book_names: corresponding book name per probability
        sharp_books: set of sharp book name strings (lowercase)

    Returns:
        dict with hhi_overall, hhi_sharp, entropy_overall, entropy_sharp, num_books
    """
    if not implied_probs or len(implied_probs) < 2:
        return {
            "hhi_overall": 10000.0,
            "hhi_sharp": None,
            "entropy_overall": 0.0,
            "entropy_sharp": None,
            "num_books": len(implied_probs),
        }

    # Normalize implied probs as market shares
    total = sum(implied_probs)
    shares = [p / total for p in implied_probs] if total > 0 else []

    # Separate sharp vs soft
    sharp_probs = []
    for prob, name in zip(implied_probs, book_names):
        if name.lower() in sharp_books:
            sharp_probs.append(prob)

    sharp_total = sum(sharp_probs) if sharp_probs else 0
    sharp_shares = [p / sharp_total for p in sharp_probs] if sharp_total > 0 else []

    return {
        "hhi_overall": round(hhi(shares), 1),
        "hhi_sharp": round(hhi(sharp_shares), 1) if sharp_shares else None,
        "entropy_overall": round(shannon_entropy(implied_probs), 4),
        "entropy_sharp": round(shannon_entropy(sharp_probs), 4) if sharp_probs else None,
        "num_books": len(implied_probs),
    }
