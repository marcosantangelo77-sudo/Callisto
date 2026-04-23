"""Dry-run: count what the prop resolver would do against live Callisto DB.

Read-only. Makes no UPDATEs. Reports the same buckets as ResolveReport
would produce if we actually ran the resolver.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DB = "C:/Users/marco/OneDrive/Desktop/Callisto/memory/callisto.db"

from tools.prop_stat_map import (  # noqa: E402
    fallback_stat_types,
    is_prop_market,
    market_to_stat_type,
)

PREFIXES = ("player_", "pitcher_", "batter_", "skater_", "goalie_")


def scan():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    # Total unresolved prop rows, by sport + by market.
    like = " OR ".join(f"market LIKE '{p}%'" for p in PREFIXES)
    total_unresolved = c.execute(
        f"SELECT COUNT(*) FROM backtest_events WHERE actual_result IS NULL AND ({like})"
    ).fetchone()[0]

    per_sport = c.execute(
        f"SELECT sport, COUNT(*) FROM backtest_events "
        f"WHERE actual_result IS NULL AND ({like}) GROUP BY sport"
    ).fetchall()

    per_market = c.execute(
        f"SELECT market, COUNT(*) FROM backtest_events "
        f"WHERE actual_result IS NULL AND ({like}) GROUP BY market ORDER BY 2 DESC"
    ).fetchall()

    # Top 500 rows we'd actually process (matches limit used by the cron).
    rows = c.execute(
        f"SELECT id, sport, event_id, player, market, line, side, game_date "
        f"FROM backtest_events WHERE actual_result IS NULL AND ({like}) "
        f"ORDER BY game_date DESC, id DESC LIMIT 500"
    ).fetchall()

    resolvable = 0
    missing_stat = 0
    missing_game = 0
    unknown_market = 0
    no_line = 0
    by_sport: dict = {}

    for ev_id, sport, event_id, player, market, line, side, gd in rows:
        bucket = by_sport.setdefault(
            sport or "?",
            {"scanned": 0, "resolvable": 0, "missing_stat": 0,
             "missing_game": 0, "unknown_market": 0, "no_line": 0},
        )
        bucket["scanned"] += 1

        if line is None:
            no_line += 1
            bucket["no_line"] += 1
            continue

        stat_type = market_to_stat_type(market)
        if stat_type is None:
            unknown_market += 1
            bucket["unknown_market"] += 1
            continue

        # Final game check — event_id in player_stats OR game_results row.
        row = c.execute(
            "SELECT 1 FROM player_stats WHERE sport = ? AND event_id = ? LIMIT 1",
            (sport, event_id or ""),
        ).fetchone()
        if not row:
            row = c.execute(
                "SELECT 1 FROM game_results WHERE sport = ? AND game_date = ? "
                "AND home_score IS NOT NULL LIMIT 1",
                (sport, gd),
            ).fetchone()
            if not row:
                missing_game += 1
                bucket["missing_game"] += 1
                continue

        # Stat row probe — try primary then fallbacks. Use LIKE on player
        # as a first approximation (real resolver uses fuzzy index).
        found = False
        for cand in (stat_type, *fallback_stat_types(stat_type)):
            r = c.execute(
                "SELECT 1 FROM player_stats "
                "WHERE sport = ? AND stat_type = ? "
                "AND (event_id = ? OR game_date = ?) "
                "AND player_name LIKE ? LIMIT 1",
                (sport, cand, event_id or "", gd, f"%{(player or '').split()[-1] if player else ''}%"),
            ).fetchone()
            if r:
                found = True
                break
        if found:
            resolvable += 1
            bucket["resolvable"] += 1
        else:
            missing_stat += 1
            bucket["missing_stat"] += 1

    out = {
        "total_unresolved_props": total_unresolved,
        "per_sport_unresolved": per_sport,
        "per_market_unresolved": per_market,
        "sample_dryrun": {
            "sampled": len(rows),
            "resolvable_now": resolvable,
            "missing_player_stats": missing_stat,
            "missing_game_result": missing_game,
            "unknown_market": unknown_market,
            "no_line": no_line,
            "by_sport": by_sport,
        },
    }
    c.close()
    return out


if __name__ == "__main__":
    result = scan()
    p = Path(__file__).parent / "_prop_dryrun_out.json"
    p.write_text(json.dumps(result, indent=2, default=str))
    print("WROTE", p)
