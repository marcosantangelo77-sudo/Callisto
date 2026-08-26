"""Signal confidence tiers + batch INSERT of pending backtest event rows.

Extracted from tools/backtest.py (slice 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid

logger = logging.getLogger("callisto.backtest")

# Backtest_events row layout (positional tuple contract shared by all
# producers/consumers in this package):
# (run_id[0], event_id[1], hyp_id[2], sport[3], player[4],
#  market[5], line[6], side[7], book[8], odds_american[9], implied[10],
#  fair_prob[11], model_factors_json[12], edge[13], ev_pct[14],
#  kelly[15], signal_generated[16], game_date[17], snapshot_time[18])

_BACKTEST_EVENT_INSERT_SQL = (
    "INSERT OR IGNORE INTO backtest_events "
    "(run_id, event_id, hypothesis_id, sport, player, market, "
    "line, side, book, book_odds_american, book_implied_prob, "
    "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
    "signal_generated, game_date, snapshot_time) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def signal_confidence(edge: float) -> str:
    """Categorize edge into confidence tiers based on realistic market edges.

    Real cross-book edges cap at ~2.5%. Old thresholds (5%/3%) were impossible
    to hit, making every signal "low". These thresholds reflect actual edge
    distribution: top-decile edges are ~2%+, median is ~1%.
    """
    if edge >= 0.02:
        return "high"
    elif edge >= 0.012:
        return "medium"
    return "low"


async def insert_pending_rows(db, rows: list[tuple], operation: str) -> None:
    """Batch-INSERT pending backtest event rows with lock-retry backoff.

    Per-game commits caused 274x write-lock contention with line_monitor;
    callers collect rows and flush them once per run via this helper.
    """
    if not rows:
        return
    for attempt in range(5):
        try:
            await db.executemany(_BACKTEST_EVENT_INSERT_SQL, rows)
            break
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 4:
                wait = min(0.5 * (2 ** attempt), 8) + random.uniform(0, 0.5)
                logger.warning(
                    f"DB locked on {operation} executemany "
                    f"(attempt {attempt + 1}/5), retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
            else:
                raise
    from tools.db_utils import commit_with_retry

    await commit_with_retry(db, operation=operation)


def paper_trade_row(event: dict, hypothesis_id: str, trade_id: str) -> tuple:
    """Build the paper_trades insert tuple from a backtest_events row dict."""
    return (
        trade_id,
        hypothesis_id,
        event.get("event_id"),
        event["sport"],
        event.get("player"),
        event["market"],
        event.get("line"),
        event["side"],
        event["book"],
        event.get("signal_odds_american", event.get("book_odds_american")),
        event.get("signal_implied_prob", event.get("book_implied_prob")),
        event.get("model_fair_prob"),
        event.get("edge"),
        event.get("ev_pct"),
        event.get("kelly_fraction"),
    )


def dedup_best_edge_by_event(rows: list[dict]) -> list[dict]:
    """Game-level dedup: keep only the best-edge row per event_id.

    Multiple books can show an edge for the same game; recording all of them
    inflates counts ~5x. Keep the highest-edge entry per event so downstream
    tables reflect independent betting opportunities.
    """
    best_by_event: dict[str, dict] = {}
    for row in rows:
        eid = row.get("event_id", "")
        existing = best_by_event.get(eid)
        if existing is None or (row.get("edge") or 0) > (existing.get("edge") or 0):
            best_by_event[eid] = row
    return list(best_by_event.values())


def new_trade_id() -> str:
    """Short unique trade id used for paper trades."""
    return str(uuid.uuid4())[:12]


__all__ = [
    "signal_confidence",
    "insert_pending_rows",
    "paper_trade_row",
    "dedup_best_edge_by_event",
    "new_trade_id",
]
