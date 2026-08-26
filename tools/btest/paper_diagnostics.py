"""Paper-trade signal diagnostics.

Extracted from tools/backtest.py (slice 2). Pure helpers over the pending
backtest_events row tuples produced during paper-trade signal generation —
no DB access, unit-testable directly.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("callisto.backtest")

# Tuple layout of a backtest_events row:
# (run_id[0], event_id[1], hyp_id[2], sport[3], player[4],
#  market[5], line[6], side[7], book[8], odds_american[9], implied[10],
#  fair_prob[11], model_factors_json[12], edge[13], ev_pct[14],
#  kelly[15], signal_generated[16], game_date[17], snapshot_time[18])
COL_EDGE = 13
COL_FAIR_PROB = 11
COL_MODEL_FACTORS = 12
COL_BOOK = 8


def edge_distribution(rows: list[tuple]) -> dict:
    """Summarize the edge distribution across pending rows.

    Shows why 0-signal cycles happen: how many candidate edges were above
    threshold and what book-count range they came from.
    """
    if not rows:
        return {
            "max_edge": 0,
            "min_edge": 0,
            "above_thresh": 0,
            "min_books_seen": 0,
            "max_books_seen": 0,
        }
    edges = [row[COL_EDGE] for row in rows]
    books_counts = []
    for row in rows:
        try:
            factors = json.loads(row[COL_MODEL_FACTORS]) if row[COL_MODEL_FACTORS] else {}
            books_counts.append(factors.get("books_used", 0))
        except Exception:
            pass
    return {
        "max_edge": max(edges) if edges else 0,
        "min_edge": min(edges) if edges else 0,
        "above_thresh": sum(1 for e in edges if e >= 0),
        "min_books_seen": min(books_counts) if books_counts else 0,
        "max_books_seen": max(books_counts) if books_counts else 0,
    }


def suppression_reasons(
    rows: list[tuple],
    edge_threshold: float,
    market_type: str,
    max_rows: int | None = None,
) -> list[str]:
    """Diagnose WHY above-threshold candidates still produced zero signals.

    Prevents false 'paper trading is broken' alarms: each reason names the
    gate that suppressed an above-threshold edge (heavy favorite vs minimum
    book count).
    """
    reasons: list[str] = []
    for row in rows:
        edge_val = row[COL_EDGE]
        if edge_val < edge_threshold:
            continue
        fair_prob = row[COL_FAIR_PROB]
        try:
            factors = json.loads(row[COL_MODEL_FACTORS]) if row[COL_MODEL_FACTORS] else {}
        except Exception:
            factors = {}
        n_books = factors.get("books_used", 0)
        if market_type == "h2h" and fair_prob > 0.80:
            reasons.append(
                f"heavy_fav(fair={fair_prob:.3f},edge={edge_val:.4f},book={row[COL_BOOK]})"
            )
        elif n_books < 4:
            reasons.append(
                f"min_books(n={n_books},edge={edge_val:.4f},book={row[COL_BOOK]})"
            )
        if max_rows is not None and len(reasons) >= max_rows:
            break
    return reasons


__all__ = ["edge_distribution", "suppression_reasons"]
