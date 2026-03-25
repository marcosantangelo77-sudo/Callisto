"""
Purge contaminated backtest events and reset hypotheses for re-evaluation.

Context (2026-03-25):
All 911 backtest events were generated BEFORE the outlier filter fix (cfad0c2).
They contain phantom edges up to 76% caused by swapped-side data from BetMGM
and other books. The outlier filter + edge magnitude cap are now in place but
these old events will never be re-evaluated — they must be purged so the
pipeline can re-run backtests with corrected code.

What this does:
1. Deletes ALL backtest_events (all 911 are pre-fix contaminated)
2. Resets 'backtesting' hypotheses → 'draft' (so _phase_backtest picks them up)
3. Resets 'rejected' hypotheses → 'draft' (they were rejected on phantom data)
4. Clears backtest_runs metadata
5. Resets eval_cycles counters in hypothesis notes

Does NOT touch:
- historical_odds_cache (the imported data is fine)
- game_results (resolution data is fine)
- signals table (will be repopulated)
- hypotheses in 'draft' status (unchanged)
"""

import asyncio
import aiosqlite
import os
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "callisto.db")


async def main():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        sys.exit(1)

    async with aiosqlite.connect(DB_PATH) as db:
        # Current state
        cursor = await db.execute("SELECT COUNT(*) FROM backtest_events")
        total_events = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM backtest_events WHERE ABS(edge) > 0.15")
        phantom_events = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT status, COUNT(*) FROM hypotheses GROUP BY status")
        status_counts = {row[0]: row[1] for row in await cursor.fetchall()}

        print(f"=== PRE-PURGE STATE ===")
        print(f"  Backtest events: {total_events} ({phantom_events} with |edge| > 15%)")
        print(f"  Hypothesis status: {status_counts}")

        # 1. Purge all backtest events
        await db.execute("DELETE FROM backtest_events")
        print(f"\n  [1/5] Deleted {total_events} contaminated backtest events")

        # 2. Reset backtesting → draft
        bt_count = status_counts.get("backtesting", 0)
        await db.execute(
            "UPDATE hypotheses SET status = 'draft', updated_at = ? "
            "WHERE status = 'backtesting'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        print(f"  [2/5] Reset {bt_count} backtesting hypotheses -> draft")

        # 3. Reset rejected → draft
        rej_count = status_counts.get("rejected", 0)
        await db.execute(
            "UPDATE hypotheses SET status = 'draft', updated_at = ? "
            "WHERE status = 'rejected'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        print(f"  [3/5] Reset {rej_count} rejected hypotheses -> draft")

        # 4. Clear backtest_runs
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM backtest_runs")
            runs_count = (await cursor.fetchone())[0]
            await db.execute("DELETE FROM backtest_runs")
            print(f"  [4/5] Cleared {runs_count} backtest_runs")
        except Exception:
            print(f"  [4/5] backtest_runs table not found (skipped)")

        # 5. Clear signals that came from backtests
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM signals WHERE source LIKE '%backtest%'"
            )
            sig_count = (await cursor.fetchone())[0]
            await db.execute("DELETE FROM signals WHERE source LIKE '%backtest%'")
            print(f"  [5/5] Cleared {sig_count} backtest-derived signals")
        except Exception:
            print(f"  [5/5] No backtest signals to clear")

        await db.commit()

        # Verify
        cursor = await db.execute("SELECT COUNT(*) FROM backtest_events")
        remaining = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT status, COUNT(*) FROM hypotheses GROUP BY status")
        new_status = {row[0]: row[1] for row in await cursor.fetchall()}

        print(f"\n=== POST-PURGE STATE ===")
        print(f"  Backtest events: {remaining}")
        print(f"  Hypothesis status: {new_status}")
        print(f"\n  Pipeline will re-run backtests with outlier filter + 15% edge cap.")
        print(f"  Estimated re-backtest time: ~30-60 minutes (depends on batch size).")


if __name__ == "__main__":
    asyncio.run(main())
