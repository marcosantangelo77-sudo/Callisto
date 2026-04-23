"""
Granger temporal prediction — identify which book's line movements predict others.

Tests whether book A's price changes help predict book B's future price
changes beyond what B's own history predicts. The "sharp leader" per
sport/market is the book whose movements most often temporally predict others.

NOTE: This measures temporal prediction, not causal direction. A book that
consistently moves first may be reacting to the same information faster,
not necessarily causing the movement.

Uses VAR (Vector Autoregression) with F-test for temporal precedence.
Runs as a periodic analysis during deep work cycles (weekly), not real-time.
Results are cached in granger_results table.

Requires: numpy (already in codebase), scipy.stats.f for F-distribution.
Gracefully degrades when insufficient data (<100 paired observations).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger("callisto.granger_causality")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Minimum observations required for a valid Granger test
MIN_OBSERVATIONS = 50


def granger_test(
    x: np.ndarray,
    y: np.ndarray,
    max_lag: int = 3,
) -> dict:
    """
    Test if x temporally predicts y (Granger test).

    Uses F-test comparing:
      Restricted model: y_t = a0 + a1*y_{t-1} + ... + ap*y_{t-p} + e
      Unrestricted: y_t = a0 + a1*y_{t-1} + ... + b1*x_{t-1} + ... + e

    If unrestricted significantly reduces RSS, x's history helps predict y
    beyond y's own history — x temporally leads y.

    Args:
        x: time series of "leader" (e.g., Pinnacle price changes)
        y: time series of "follower" (e.g., DraftKings price changes)
        max_lag: maximum lag to test (tests 1..max_lag, picks best by AIC)

    Returns:
        dict with f_statistic, p_value, optimal_lag, is_significant, n_observations
    """
    n = min(len(x), len(y))
    if n < MIN_OBSERVATIONS:
        return {
            "f_statistic": None,
            "p_value": None,
            "optimal_lag": None,
            "is_significant": False,
            "n_observations": n,
            "error": f"Insufficient data: {n} < {MIN_OBSERVATIONS} required",
        }

    x = np.asarray(x[:n], dtype=np.float64)
    y = np.asarray(y[:n], dtype=np.float64)

    best_result = None
    best_aic = float("inf")

    for lag in range(1, max_lag + 1):
        if n - lag < lag * 2 + 5:  # Need enough DoF
            continue

        # Build lagged matrices
        Y = y[lag:]
        T = len(Y)

        # Restricted model: only lagged y
        X_r = np.column_stack([np.ones(T)] + [y[lag - i - 1 : n - i - 1] for i in range(lag)])

        # Unrestricted model: lagged y + lagged x
        X_u = np.column_stack([
            X_r,
            *[x[lag - i - 1 : n - i - 1] for i in range(lag)],
        ])

        try:
            # OLS: beta = (X'X)^-1 X'Y
            beta_r = np.linalg.lstsq(X_r, Y, rcond=None)[0]
            beta_u = np.linalg.lstsq(X_u, Y, rcond=None)[0]

            rss_r = np.sum((Y - X_r @ beta_r) ** 2)
            rss_u = np.sum((Y - X_u @ beta_u) ** 2)

            # F-test
            df_num = lag  # extra parameters in unrestricted
            df_den = T - X_u.shape[1]  # residual DoF
            if df_den <= 0 or rss_u <= 0:
                continue

            f_stat = ((rss_r - rss_u) / df_num) / (rss_u / df_den)

            # P-value from F-distribution
            try:
                from scipy.stats import f as f_dist
                p_value = 1 - f_dist.cdf(f_stat, df_num, df_den)
            except ImportError:
                # Fallback: approximate with chi-squared
                p_value = _approximate_f_pvalue(f_stat, df_num, df_den)

            # AIC for lag selection
            aic = T * np.log(rss_u / T) + 2 * X_u.shape[1]

            if aic < best_aic:
                best_aic = aic
                best_result = {
                    "f_statistic": round(float(f_stat), 4),
                    "p_value": round(float(p_value), 6),
                    "optimal_lag": lag,
                    "is_significant": p_value < 0.05,
                    "n_observations": T,
                }
        except (np.linalg.LinAlgError, ValueError):
            continue

    if best_result is None:
        return {
            "f_statistic": None,
            "p_value": None,
            "optimal_lag": None,
            "is_significant": False,
            "n_observations": n,
            "error": "Could not compute F-test (numerical issues)",
        }

    return best_result


def _approximate_f_pvalue(f_stat: float, df1: int, df2: int) -> float:
    """Rough approximation of F p-value without scipy."""
    # Use the fact that for large df2, F ~ chi2/df1
    # Very rough — only used as fallback
    if f_stat <= 0:
        return 1.0
    # For df2 > 30, F-distribution is approximately normal
    z = (f_stat * df1 / df2 - 1) / (2 / df2) ** 0.5
    # Normal CDF approximation
    p = 0.5 * (1 + _erf(z / 2 ** 0.5))
    return max(0.0, 1 - p)


def _erf(x: float) -> float:
    """Approximation of error function."""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (
        ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
         - 0.284496736) * t + 0.254829592
    ) * t * np.exp(-x * x)
    return sign * y


async def analyze_book_leadership(
    db_path: str,
    sport: str,
    market_type: str = "h2h",
    min_observations: int = MIN_OBSERVATIONS,
) -> dict:
    """
    Run pairwise Granger tests between books for a sport.

    Uses line_movements table to build time series of price changes per book.
    Identifies the "sharp leader" — the book whose movements most often
    temporally predict others' subsequent movements.

    Returns dict with leader_book, pair_results, and data sufficiency info.
    """
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        # Get price movements grouped by bookmaker, ordered by time
        cursor = await db.execute(
            "SELECT bookmaker, detected_at, price_movement "
            "FROM line_movements "
            "WHERE sport = ? AND price_movement != 0 "
            "ORDER BY detected_at",
            (sport,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return {
            "sport": sport,
            "market_type": market_type,
            "leader_book": None,
            "leader_score": 0,
            "pair_results": [],
            "n_pairs_tested": 0,
            "n_pairs_sufficient_data": 0,
            "warning": "No line movement data for this sport",
        }

    # Group movements by bookmaker
    book_series: dict[str, list[float]] = {}
    for bookmaker, _, movement in rows:
        book = bookmaker.lower()
        if book not in book_series:
            book_series[book] = []
        book_series[book].append(float(movement))

    # Filter books with enough data
    valid_books = {
        book: np.array(series)
        for book, series in book_series.items()
        if len(series) >= min_observations
    }

    if len(valid_books) < 2:
        return {
            "sport": sport,
            "market_type": market_type,
            "leader_book": None,
            "leader_score": 0,
            "pair_results": [],
            "n_pairs_tested": 0,
            "n_pairs_sufficient_data": len(valid_books),
            "warning": f"Need 2+ books with {min_observations}+ movements. "
                       f"Have: {', '.join(f'{b}({len(s)})' for b, s in book_series.items())}",
        }

    # Pairwise Granger tests
    pair_results = []
    leads_count: dict[str, int] = {b: 0 for b in valid_books}
    book_names = sorted(valid_books.keys())

    for i, book_a in enumerate(book_names):
        for book_b in book_names[i + 1:]:
            # Align series to same length
            n = min(len(valid_books[book_a]), len(valid_books[book_b]))
            x = valid_books[book_a][:n]
            y = valid_books[book_b][:n]

            # Test A → B
            ab = granger_test(x, y, max_lag=3)
            # Test B → A
            ba = granger_test(y, x, max_lag=3)

            direction = "none"
            if ab.get("is_significant") and not ba.get("is_significant"):
                direction = f"{book_a}_leads_{book_b}"
                leads_count[book_a] += 1
            elif ba.get("is_significant") and not ab.get("is_significant"):
                direction = f"{book_b}_leads_{book_a}"
                leads_count[book_b] += 1
            elif ab.get("is_significant") and ba.get("is_significant"):
                direction = "bidirectional"

            pair_results.append({
                "book_a": book_a,
                "book_b": book_b,
                "a_causes_b": ab,
                "b_causes_a": ba,
                "direction": direction,
            })

    # Identify leader
    total_pairs = len(pair_results)
    leader_book = max(leads_count, key=leads_count.get) if leads_count else None
    leader_score = leads_count.get(leader_book, 0) / max(total_pairs, 1)

    return {
        "sport": sport,
        "market_type": market_type,
        "leader_book": leader_book,
        "leader_score": round(leader_score, 3),
        "pair_results": pair_results,
        "n_pairs_tested": total_pairs,
        "n_pairs_sufficient_data": len(valid_books),
        "books_tested": book_names,
    }


async def store_results(db_path: str, results: dict) -> int:
    """Persist Granger results to granger_results table."""
    import aiosqlite

    sport = results["sport"]
    market_type = results["market_type"]
    now = datetime.now(timezone.utc).isoformat()
    stored = 0

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        for pair in results.get("pair_results", []):
            for direction_key in ["a_causes_b", "b_causes_a"]:
                test = pair[direction_key]
                if test.get("f_statistic") is None:
                    continue
                try:
                    await db.execute(
                        "INSERT INTO granger_results "
                        "(sport, market_type, book_a, book_b, f_statistic, p_value, "
                        "optimal_lag, is_significant, direction, n_observations, computed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sport, market_type, pair["book_a"], pair["book_b"],
                            test["f_statistic"], test["p_value"],
                            test["optimal_lag"], test["is_significant"],
                            pair["direction"], test["n_observations"], now,
                        ),
                    )
                    stored += 1
                except Exception as e:
                    logger.debug(f"Granger store error: {e}")
        await db.commit()

    logger.info(f"Stored {stored} Granger results for {sport}/{market_type}")
    return stored


async def get_sharp_leader(
    db_path: str,
    sport: str,
    market_type: str = "h2h",
) -> Optional[str]:
    """Return the identified sharp leader book from most recent analysis.

    Only considers results from the last 30 days to prevent stale data
    from persisting through regime changes (e.g., a book changing its
    pricing model or data feed).
    """
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        # Only consider results from the last 30 days to avoid stale regime data
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cursor = await db.execute(
            "SELECT book_a, book_b, direction FROM granger_results "
            "WHERE sport = ? AND market_type = ? AND is_significant = 1 "
            "AND computed_at > ? "
            "ORDER BY computed_at DESC LIMIT 20",
            (sport, market_type, cutoff),
        )
        rows = await cursor.fetchall()

    if not rows:
        return None

    # Count leadership
    leads: dict[str, int] = {}
    for book_a, book_b, direction in rows:
        if direction and "_leads_" in direction:
            leader = direction.split("_leads_")[0]
            leads[leader] = leads.get(leader, 0) + 1

    if not leads:
        return None
    return max(leads, key=leads.get)
