"""Historical backtest over odds_snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.arb.models import DEFAULT_BUDGET, DEFAULT_EPSILON
from tools.arb.orchestrator import full_arbitrage_scan
from tools.arb.prices import _parse_ts


# ---------------------------------------------------------------------------
# Historical backtest over odds_snapshots.
# ---------------------------------------------------------------------------
def backtest_arbs(
    db_path: str,
    *,
    days: int = 30,
    epsilon: float = DEFAULT_EPSILON,
    # Historical snapshots often lack per-outcome fetched_at because they
    # pre-date the WS fetched_at column. Use a wide-open stale window and
    # allow missing timestamps — the question is "how often has an arb
    # mathematically existed?", independent of how long it lasted.
    stale_seconds: float = 86400.0,
    budget: float = DEFAULT_BUDGET,
    allow_missing_ts: bool = True,
    limit_snapshots: Optional[int] = None,
) -> dict:
    """Replay ``days`` of odds_snapshots and count how often arbs appeared.

    Returns a summary dict with:
        total_snapshots_scanned
        snapshots_with_arb
        total_arb_instances    (dedupe'd across points per game/market)
        per_day_mean
        profit_pct_p50, profit_pct_p90
        lifespan_seconds_mean  (requires consecutive-snapshot tracking)
        per_sport counts
        book_limit_impact_pct  (% of arbs that would hit book caps)
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    query = (
        "SELECT id, sport, timestamp, snapshot_json "
        "FROM odds_snapshots "
        "WHERE timestamp >= ? "
        "ORDER BY sport, timestamp"
    )
    params: list = [cutoff]
    if limit_snapshots:
        query += f" LIMIT {int(limit_snapshots)}"
    cur = conn.execute(query, params)

    total = 0
    with_arb = 0
    profits: list[float] = []
    per_sport: dict[str, int] = defaultdict(int)
    per_day: dict[str, int] = defaultdict(int)
    book_limit_hits = 0

    # Lifespan tracking: {(sport, game_id, market, key): first_seen_ts}
    lifespans: list[float] = []
    active_arbs: dict[tuple, datetime] = {}
    seen_this_round: set = set()

    prev_sport: Optional[str] = None
    for row in cur:
        total += 1
        sport = row["sport"]
        ts_str = row["timestamp"]
        snap_ts = _parse_ts(ts_str) or datetime.now(timezone.utc)
        if sport != prev_sport:
            # Closing out all active arbs from the previous sport.
            for key, start in active_arbs.items():
                lifespans.append(0.0)  # one-off, evaporated by next scan
            active_arbs.clear()
            prev_sport = sport

        try:
            snap = json.loads(row["snapshot_json"])
        except Exception:
            continue

        # Use snapshot's own timestamp as "now" so stale_seconds is meaningful
        # relative to WHEN the snapshot was recorded, not calendar-now.
        res = full_arbitrage_scan(
            snap,
            epsilon=epsilon,
            stale_seconds=stale_seconds,
            budget=budget,
            now=snap_ts,
            include_synthetic=False,       # keep backtest focused on pure/dutch
            allow_missing_ts=allow_missing_ts,
        )
        arbs = res["pure_arbs"] + res["dutch_books"]
        if arbs:
            with_arb += 1
            day = snap_ts.date().isoformat()
            per_day[day] += len(arbs)
            per_sport[sport] += len(arbs)
            round_keys = set()
            for a in arbs:
                profits.append(a["profit_pct"])
                if a.get("limited_by_book_caps"):
                    book_limit_hits += 1
                key = (sport, a["game_id"], a["market_type"],
                       tuple(sorted(leg["bookmaker_canonical"] for leg in a["legs"])))
                round_keys.add(key)
                if key not in active_arbs:
                    active_arbs[key] = snap_ts
            # Close out arbs that didn't appear this round.
            gone = [k for k in active_arbs if k not in round_keys]
            for k in gone:
                lifespans.append((snap_ts - active_arbs.pop(k)).total_seconds())
        else:
            # No arbs this round -> close all active.
            for k, start in list(active_arbs.items()):
                lifespans.append((snap_ts - start).total_seconds())
            active_arbs.clear()

    # Close any still-active arbs at the end of the window.
    for k, start in active_arbs.items():
        lifespans.append(0.0)

    conn.close()

    def pctl(data: list[float], q: float) -> float:
        if not data:
            return 0.0
        s = sorted(data)
        k = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
        return s[k]

    days_span = max(1, days)
    total_instances = sum(per_day.values())
    return {
        "days_analyzed": days,
        "total_snapshots_scanned": total,
        "snapshots_with_arb": with_arb,
        "total_arb_instances": total_instances,
        "arbs_per_day_mean": round(total_instances / days_span, 3),
        "per_sport": dict(per_sport),
        "per_day": dict(per_day),
        "profit_pct_p50": round(pctl(profits, 50) * 100, 3) if profits else 0.0,
        "profit_pct_p90": round(pctl(profits, 90) * 100, 3) if profits else 0.0,
        "profit_pct_max": round(max(profits) * 100, 3) if profits else 0.0,
        "lifespan_seconds_mean": round(sum(lifespans) / len(lifespans), 1) if lifespans else 0.0,
        "lifespan_seconds_p50": round(pctl(lifespans, 50), 1) if lifespans else 0.0,
        "lifespan_seconds_p90": round(pctl(lifespans, 90), 1) if lifespans else 0.0,
        "book_limit_impact_pct": round(100.0 * book_limit_hits / max(1, total_instances), 2),
        "params": {
            "epsilon": epsilon,
            "stale_seconds": stale_seconds,
            "budget": budget,
            "allow_missing_ts": allow_missing_ts,
        },
    }
