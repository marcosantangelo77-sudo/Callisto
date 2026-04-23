"""End-to-end integration test — verifies all wired connections before restart."""
import asyncio
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures = []
passes = []


def check(name, condition, detail=""):
    if condition:
        passes.append(name)
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        failures.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


async def run_tests():
    print("=" * 70)
    print("CALLISTO END-TO-END INTEGRATION TEST")
    print("=" * 70)

    # 1. Schema
    print("\n[1] Schema")
    try:
        from tools.schema import ensure_schema
        await ensure_schema()
        check("ensure_schema", True)
    except Exception as e:
        check("ensure_schema", False, str(e))

    # 2. DB write lock + retry
    print("\n[2] DB Write Lock")
    try:
        from tools.db_utils import get_write_lock, execute_with_retry, commit_with_retry
        import aiosqlite
        lock = get_write_lock()
        db = await aiosqlite.connect("memory/callisto.db")
        await db.execute("PRAGMA busy_timeout = 60000")
        async with lock:
            await execute_with_retry(db,
                "INSERT OR REPLACE INTO system_improvements (id, cycle, category, suggestion, priority) VALUES (?, ?, ?, ?, ?)",
                (999, 0, "e2e_test", "integration_test_write_lock", "low"),
                operation="test")
            await commit_with_retry(db, operation="test")
        cursor = await db.execute("SELECT suggestion FROM system_improvements WHERE id = 999")
        row = await cursor.fetchone()
        check("write_lock_serializes", row and row[0] == "integration_test_write_lock")
        await db.execute("DELETE FROM system_improvements WHERE id = 999")
        await db.commit()
        await db.close()
    except Exception as e:
        check("write_lock", False, str(e))

    # 3. CLV logging
    print("\n[3] CLV Logging")
    try:
        from tools.clv_tracker import CLVTracker
        tracker = CLVTracker()
        await tracker.initialize()
        bet_id = await tracker.record_bet(
            sport="test", game_description="E2E Test",
            team="TestTeam", market="h2h", bookmaker="test",
            placement_odds=-110, stake=10, event_id="e2e_test_001")
        result = await tracker.resolve_bet(bet_id, "won", payout=19.09)
        check("resolve_bet_returns_won", result.get("result") == "won")

        import aiosqlite
        async with aiosqlite.connect("memory/callisto.db") as db:
            cursor = await db.execute("SELECT * FROM clv_log WHERE bet_id = ?", (str(bet_id),))
            row = await cursor.fetchone()
        check("clv_log_written", row is not None)

        # Cleanup
        async with aiosqlite.connect("memory/callisto.db") as db:
            await db.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
            await db.execute("DELETE FROM clv_log WHERE bet_id = ?", (str(bet_id),))
            await db.execute("DELETE FROM bankroll WHERE bet_id = ?", (bet_id,))
            await db.commit()
        await tracker.close()
    except Exception as e:
        check("clv_logging", False, str(e))

    # 4. Context enrichment
    print("\n[4] Context Enrichment")
    try:
        from tools.data_collector import DataCollector
        dc = DataCollector()
        await dc.initialize()
        result = await dc.collect_scores("basketball_nba", "20260325")

        import aiosqlite
        async with aiosqlite.connect("memory/callisto.db") as db:
            cursor = await db.execute(
                "SELECT context_json FROM game_contexts WHERE sport = ? AND game_date = ? LIMIT 1",
                ("basketball_nba", "2026-03-25"))
            row = await cursor.fetchone()
        if row:
            ctx = json.loads(row[0])
            has_rest = "home_rest_days" in ctx or "away_rest_days" in ctx
            check("enrichment_rest_days", has_rest, f"rest={ctx.get('home_rest_days')}")
            check("enrichment_broadcasts", "broadcasts" in ctx, f"bc={ctx.get('broadcasts', [])[:2]}")
        else:
            check("enrichment", False, "No game context found")
        await dc.close()
    except Exception as e:
        check("enrichment", False, str(e))

    # 5. Market microstructure
    print("\n[5] Market Microstructure")
    try:
        from tools.edge_scanner import full_edge_scan
        import aiosqlite
        async with aiosqlite.connect("memory/callisto.db") as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cursor = await db.execute(
                "SELECT snapshot_json FROM odds_snapshots WHERE game_count > 0 ORDER BY timestamp DESC LIMIT 1")
            row = await cursor.fetchone()

        if row:
            snapshot = json.loads(row[0])
            report = full_edge_scan(snapshot)
            found = False
            for key in ["cross_book_h2h", "cross_book_spreads", "cross_book_totals"]:
                for edge in report.get(key, []):
                    if edge.get("hhi") is not None:
                        found = True
                        break
            total = report.get("total_edges", 0)
            check("edge_scanner_hhi", found or total == 0,
                  f"{total} edges, HHI present={found}")
        else:
            check("microstructure", False, "No snapshots")
    except Exception as e:
        check("microstructure", False, str(e))

    # 6. Learned correlations
    print("\n[6] Learned Correlations")
    try:
        from tools.learned_correlations import LearnedCorrelationStore
        store = LearnedCorrelationStore()
        await store.initialize()
        stats = store.get_stats()
        obs = stats.get("pairs_with_30_plus_obs", 0)
        check("learned_corr_has_data", obs > 0, f"{obs} pairs with 30+ obs")
        blended = store.get_blended("nba", "player_points", "game_total", prior=0.35)
        check("learned_corr_blending", isinstance(blended, float), f"blended={blended:.4f}")
        await store.close()
    except Exception as e:
        check("learned_correlations", False, str(e))

    # 7. Event bus
    print("\n[7] Event Bus")
    try:
        from tools.event_bus import EventBus, EVENT_EDGE_DETECTED
        bus = EventBus()
        received = []
        async def handler(data): received.append(data)
        bus.subscribe(EVENT_EDGE_DETECTED, handler)
        await bus.publish(EVENT_EDGE_DETECTED, {"test": True})
        await asyncio.sleep(0.2)
        check("event_bus_pubsub", len(received) == 1, f"received {len(received)}")
    except Exception as e:
        check("event_bus", False, str(e))

    # 8. Game scheduler
    print("\n[8] Game Scheduler")
    try:
        from tools.game_scheduler import GameScheduler
        from tools.event_bus import EventBus
        sched = GameScheduler(event_bus=EventBus())
        count = await sched.refresh_calendar()
        check("game_scheduler_loads", True, f"{count} games")
    except Exception as e:
        check("game_scheduler", False, str(e))

    # 9. Promotion gates
    print("\n[9] Promotion Gates")
    try:
        from tools.hypothesis import PROMOTION_GATES
        bt_gate = [v for k, v in PROMOTION_GATES.items() if "paper" in k][0]
        check("gate_brier", "max_brier" in bt_gate, f"threshold={bt_gate.get('max_brier')}")
        check("gate_edge_rate", "min_positive_edge_rate" in bt_gate, f"min={bt_gate.get('min_positive_edge_rate')}")
        live_gate = [v for k, v in PROMOTION_GATES.items() if "live" in k][0]
        check("gate_ic", "min_ic" in live_gate, f"min={live_gate.get('min_ic')}")
    except Exception as e:
        check("promotion_gates", False, str(e))

    # 10. Multi-book backtest
    print("\n[10] Multi-Book Backtest")
    try:
        import inspect
        from tools.backtest import BacktestEngine
        src = inspect.getsource(BacktestEngine._process_game_lines)
        check("multi_book_loop", "for eval_target in common_books" in src)
        check("sharp_exclusion", "SHARP_BOOKS" in src)
    except Exception as e:
        check("multi_book", False, str(e))

    # 11. Pipeline validator + watchdog
    print("\n[11] Pipeline Validator + Watchdog")
    try:
        from tools.autonomous import ResearchLoop
        check("phase_validate", hasattr(ResearchLoop, "_phase_validate"))
        check("phase_watchdog", hasattr(ResearchLoop, "_phase_system_watchdog"))
        check("reactive_handler", hasattr(ResearchLoop, "_on_game_completed"))
    except Exception as e:
        check("pipeline_phases", False, str(e))

    # 12. Hermes actionable learnings
    print("\n[12] Hermes Learnings")
    try:
        from tools.hermes_memory import HermesMemory
        hm = HermesMemory()
        learnings = await hm.get_actionable_learnings(limit=5, min_confidence=0.5)
        check("hermes_learnings", len(learnings) > 0, f"{len(learnings)} returned")
    except Exception as e:
        check("hermes", False, str(e))

    # 13. Sport monitoring
    print("\n[13] Sport Monitoring")
    try:
        from tools.line_monitor import MONITORED_SPORTS
        check("nhl_monitored", "icehockey_nhl" in MONITORED_SPORTS)
        check("nba_monitored", "basketball_nba" in MONITORED_SPORTS)
        check("mlb_monitored", "baseball_mlb" in MONITORED_SPORTS)
        check("ncaaw_monitored", "basketball_ncaaw" in MONITORED_SPORTS)
    except Exception as e:
        check("monitoring", False, str(e))

    # 14. Temporal analysis pattern discovery
    print("\n[14] Pattern Discovery")
    try:
        from tools.temporal_analysis import generate_hypotheses_from_analysis
        check("pattern_discovery_importable", True)
    except Exception as e:
        check("pattern_discovery", False, str(e))

    # 15. TCI scraper
    print("\n[15] TCI Scraper")
    try:
        from tools.tci_scraper import compute_tci, build_tci_for_tournament
        check("tci_scraper_importable", True)
    except Exception as e:
        check("tci_scraper", False, str(e))

    # 16. Telegram alerts
    print("\n[16] Telegram Alerts")
    try:
        from tools.telegram import alert_sharp_move, alert_prop_edges, alert_bet_result
        check("telegram_sharp_alert", callable(alert_sharp_move))
        check("telegram_prop_alert", callable(alert_prop_edges))
        check("telegram_bet_result", callable(alert_bet_result))
    except Exception as e:
        check("telegram_alerts", False, str(e))

    # 17. Full simulation
    print("\n[17] Simulation")
    try:
        from tools.simulation import simulate_basketball, simulate_poisson
        from tools.sim import nba_game_sim, player_prop_sim
        check("simulation_full", callable(simulate_basketball))
        check("simulation_lite", callable(nba_game_sim))
    except Exception as e:
        check("simulation", False, str(e))

    # 18. Orchestrator tool dispatch
    print("\n[18] Orchestrator")
    try:
        from orchestrator import Orchestrator
        check("orchestrator_importable", True)
    except Exception as e:
        check("orchestrator", False, str(e))

    # 19. AGP protocol
    print("\n[19] AGP Protocol")
    try:
        from agp import Domain, SourceClass, ConfidenceTier, Evidence, AGPSession
        check("agp_protocol", True)
    except Exception as e:
        check("agp", False, str(e))

    # 20. All critical DB tables exist
    print("\n[20] Database Tables")
    try:
        import aiosqlite
        async with aiosqlite.connect("memory/callisto.db") as db:
            required = [
                "hypotheses", "backtest_events", "backtest_runs", "signals",
                "bets", "clv_log", "bankroll", "closing_lines", "closing_lines_v2",
                "game_contexts", "game_results", "player_stats", "embeddings",
                "odds_snapshots", "line_movements", "prop_snapshots",
                "ev_opportunities", "paper_trades", "market_microstructure",
                "learned_correlations", "kl_metrics", "granger_results",
                "event_log", "hermes_learnings", "tci_scores",
                "historical_odds_cache", "integrity_checks",
            ]
            missing = []
            for table in required:
                cursor = await db.execute(
                    f"SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,))
                if not await cursor.fetchone():
                    missing.append(table)
            check("all_tables_exist", len(missing) == 0,
                  f"missing: {missing}" if missing else f"all {len(required)} present")
    except Exception as e:
        check("db_tables", False, str(e))

    # SUMMARY
    print("\n" + "=" * 70)
    total = len(passes) + len(failures)
    print(f"RESULTS: {len(passes)}/{total} PASSED, {len(failures)} FAILED")
    print("=" * 70)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  X {f}")
        print(f"\nVERDICT: FIX {len(failures)} FAILURES BEFORE RESTART")
        return False
    else:
        print("\nVERDICT: ALL TESTS PASSED — READY FOR RESTART")
        return True


if __name__ == "__main__":
    ok = asyncio.run(run_tests())
    sys.exit(0 if ok else 1)
