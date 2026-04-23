"""
One-time audit: why are 60% of backtest events unresolved?
Checks game_results vs game_contexts coverage, team name matching.
"""
import asyncio
import aiosqlite
import json
import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "data/callisto.db")


async def audit():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. game_results count
        row = await (await db.execute("SELECT COUNT(*) FROM game_results")).fetchone()
        print(f"game_results rows: {row[0]}")

        # 2. game_contexts with scores
        row = await (await db.execute(
            "SELECT COUNT(*) FROM game_contexts WHERE home_score IS NOT NULL"
        )).fetchone()
        print(f"game_contexts with scores: {row[0]}")

        row = await (await db.execute(
            "SELECT COUNT(*) FROM game_contexts WHERE home_score IS NULL"
        )).fetchone()
        print(f"game_contexts without scores: {row[0]}")

        # 3. Unresolved backtest events
        row = await (await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE actual_result IS NULL"
        )).fetchone()
        unresolved = row[0]
        row = await (await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE actual_result IS NOT NULL"
        )).fetchone()
        resolved = row[0]
        print(f"\nBacktest events: {resolved} resolved, {unresolved} unresolved ({unresolved/(resolved+unresolved)*100:.0f}%)")

        # 4. Unresolved events - sample dates and teams
        print("\n=== SAMPLE UNRESOLVED EVENTS ===")
        rows = await (await db.execute(
            "SELECT id, event_id, sport, game_date, model_factors "
            "FROM backtest_events WHERE actual_result IS NULL LIMIT 10"
        )).fetchall()
        for r in rows:
            mf = json.loads(r[4]) if r[4] else {}
            home = mf.get("home_team", "?")
            away = mf.get("away_team", "?")
            ev_parts = r[1].split("|") if r[1] and "|" in r[1] else []
            ev_home = ev_parts[1] if len(ev_parts) >= 3 else "?"
            ev_away = ev_parts[2] if len(ev_parts) >= 3 else "?"
            print(f"  id={r[0]} sport={r[2]} date={r[3]} ev_id_teams=[{ev_home} vs {ev_away}] mf_teams=[{home} vs {away}]")

        # 5. Check if those dates exist in game_contexts/game_results
        print("\n=== SCORE AVAILABILITY FOR UNRESOLVED DATES ===")
        rows = await (await db.execute(
            "SELECT DISTINCT sport, game_date FROM backtest_events WHERE actual_result IS NULL"
        )).fetchall()
        for sport, gdate in rows[:10]:
            gc_row = await (await db.execute(
                "SELECT COUNT(*) FROM game_contexts WHERE sport = ? AND game_date = ? AND home_score IS NOT NULL",
                (sport, gdate)
            )).fetchone()
            gr_row = await (await db.execute(
                "SELECT COUNT(*) FROM game_results WHERE sport = ? AND game_date = ?",
                (sport, gdate)
            )).fetchone()
            print(f"  {sport} {gdate}: game_results={gr_row[0]}, game_contexts_w_scores={gc_row[0]}")

        # 6. Books used distribution
        print("\n=== BOOKS USED DISTRIBUTION ===")
        rows = await (await db.execute(
            "SELECT json_extract(model_factors, '$.books_used') as books, COUNT(*) as cnt, "
            "SUM(signal_generated) as signals FROM backtest_events "
            "WHERE model_factors IS NOT NULL GROUP BY books ORDER BY books"
        )).fetchall()
        for r in rows:
            print(f"  books={r[0]}: {r[1]} events, {r[2]} signals")

        # 7. Top hypotheses by events with books
        print("\n=== TOP HYPOTHESES ===")
        rows = await (await db.execute(
            "SELECT h.name, COUNT(be.id) as events, SUM(be.signal_generated) as signals, "
            "ROUND(AVG(be.edge), 4) as avg_edge, "
            "ROUND(AVG(json_extract(be.model_factors, '$.books_used')), 1) as avg_books, "
            "SUM(CASE WHEN be.actual_result IS NOT NULL THEN 1 ELSE 0 END) as resolved "
            "FROM backtest_events be JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id "
            "GROUP BY be.hypothesis_id ORDER BY events DESC LIMIT 15"
        )).fetchall()
        for r in rows:
            print(f"  {r[0][:55]:55s} ev={r[1]:3d} sig={r[2]:2d} edge={r[3]:+.4f} bk={r[4]} res={r[5]}")

        # 8. Promotion-ready check
        print("\n=== PROMOTION READINESS ===")
        rows = await (await db.execute(
            "SELECT h.name, "
            "SUM(CASE WHEN be.signal_generated = 1 THEN 1 ELSE 0 END) as total_signals, "
            "SUM(CASE WHEN be.signal_generated = 1 AND be.actual_result IS NOT NULL THEN 1 ELSE 0 END) as resolved_signals "
            "FROM backtest_events be JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id "
            "GROUP BY be.hypothesis_id HAVING total_signals >= 3 ORDER BY total_signals DESC"
        )).fetchall()
        print(f"Hypotheses with >=3 signals: {len(rows)}")
        for r in rows:
            print(f"  {r[0]}: {r[1]} total signals, {r[2]} resolved signals (need 5 resolved for promotion)")


if __name__ == "__main__":
    asyncio.run(audit())
