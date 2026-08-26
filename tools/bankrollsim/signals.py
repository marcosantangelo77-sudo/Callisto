"""Signal loading + day-grouping for the bankroll Monte Carlo simulator.

Loads resolved, signal-generated backtest events with lookahead defense and
groups them by game_date so the bootstrap can sample days jointly (preserving
same-event correlation).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from tools.bankrollsim.config import DB_PATH


def _load_signals(
    hypothesis_ids: list[str],
    db_path: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Load signal-generated, resolved backtest events for the given hyps.

    Returns (rows, exclusion_counts). Each row is a dict:
        {hypothesis_id, event_id, game_date, sport, market, side,
         odds, edge, ev_pct, actual_result}

    Exclusion counts:
        {"no_signal": int, "unresolved": int, "lookahead": int, "total_considered": int}
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    if not hypothesis_ids:
        conn.close()
        return [], {"no_signal": 0, "unresolved": 0, "lookahead": 0, "total_considered": 0}

    placeholders = ",".join("?" * len(hypothesis_ids))
    cur = conn.execute(
        f"""
        SELECT hypothesis_id, event_id, game_date, snapshot_time, sport,
               market, side, book_odds_american, edge, ev_pct,
               signal_generated, actual_result
        FROM backtest_events
        WHERE hypothesis_id IN ({placeholders})
        """,
        tuple(hypothesis_ids),
    )
    raw = [dict(r) for r in cur.fetchall()]
    conn.close()

    exc = {
        "total_considered": len(raw),
        "no_signal": 0,
        "unresolved": 0,
        "lookahead": 0,
    }
    rows: list[dict] = []
    for r in raw:
        if not r.get("signal_generated"):
            exc["no_signal"] += 1
            continue
        if r.get("actual_result") not in ("won", "lost", "push"):
            exc["unresolved"] += 1
            continue
        # Lookahead defense: if snapshot was taken AFTER the game date,
        # this is a known-leaky row — drop it. We allow same-day snapshots
        # because the audit's pre_commence filter is about intra-day fetches
        # that don't exist as a column yet.
        try:
            gdate = datetime.fromisoformat(r["game_date"]).date()
            st_raw = r.get("snapshot_time") or ""
            # Replace Z for parseability
            st = st_raw.replace("Z", "+00:00") if st_raw else ""
            if st:
                sdt = datetime.fromisoformat(st)
                # If snapshot is > 1 day AFTER the game started, treat as leaky
                if sdt.date() > gdate + timedelta(days=1):
                    exc["lookahead"] += 1
                    continue
        except (ValueError, TypeError):
            pass  # best-effort only — don't exclude on parse failure

        rows.append({
            "hypothesis_id": r["hypothesis_id"],
            "event_id": str(r["event_id"]),
            "game_date": r["game_date"],
            "sport": r.get("sport") or "",
            "market": r.get("market") or "",
            "side": r.get("side") or "",
            "odds": int(r.get("book_odds_american") or -110),
            "edge": float(r.get("edge") or 0.0),
            "ev_pct": float(r.get("ev_pct") or 0.0),
            "actual_result": r["actual_result"],
        })
    return rows, exc


def _group_signals_by_day(
    rows: list[dict],
) -> dict[str, list[dict]]:
    """Group signals by game_date so we can sample jointly."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["game_date"], []).append(r)
    return by_day
