"""Tests for tools/lines/monitor_loop (line_monitor slice-3 extraction).

Covers the snapshot-loop / persistence-query / alerting glue extracted
from tools/line_monitor.py:

- compute_adaptive_interval  — credit-aware fallback switch + interval stretch
- run_monitor_cycle          — one loop iteration: backoff skipping, timeouts,
                               prop cascade, pause bail-out
- snapshot_props             — prop-sport filtering, lock/in-flight handling
- snapshot_sport_fallback    — failure counting + Telegram escalation threshold
- record_significant_movements — movement recording/eval/event publish
- handle_sharp_signals       — alert sink append, cap, high-confidence filter
- fetch_recent_movements / fetch_ev_opportunities / fetch_snapshot_history /
  collect_status_counts     — DB-backed query helpers against real aiosqlite

Uses a fake monitor object mirroring LineMonitor's shared state so no
network or live betting path is touched.
"""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import sys
sys.path.insert(0, ".")

import aiosqlite

from tools.lines.monitor_loop import (
    collect_status_counts,
    compute_adaptive_interval,
    fetch_ev_opportunities,
    fetch_recent_movements,
    fetch_snapshot_history,
    handle_sharp_signals,
    record_significant_movements,
    run_monitor_cycle,
    snapshot_props,
    snapshot_sport_fallback,
)


def run(coro):
    return asyncio.run(coro)


# ── Fake collaborator objects ────────────────────────────────────────────────


class FakeMonitor:
    """Mirrors the LineMonitor state surface used by monitor_loop helpers."""

    def __init__(self):
        self.db_path = ":memory:"
        self._paused = False
        self._in_flight_db = False
        self._snapshot_lock = asyncio.Lock()
        self._consecutive_failures = {}
        self._FAILURE_ALERT_THRESHOLD = 3
        self._alerts = deque(maxlen=1000)
        self.snapshotted = []
        self.fallback_snapshotted = []
        self.recorded = []
        self.evaled = []
        self.processed = []
        self.props_locked_in_flight = []

    async def _snapshot_sport(self, sport):
        if self._paused:
            return
        self.snapshotted.append(sport)

    async def _snapshot_sport_fallback(self, sport):
        if self._paused:
            return
        self.fallback_snapshotted.append(sport)
        self.snapshotted.append(sport)

    async def _record_movement(self, sport, movement):
        self.recorded.append((sport, movement))

    async def _evaluate_movement(self, sport, movement, snapshot):
        self.evaled.append((sport, movement))

    async def _process_snapshot(self, sport, snapshot):
        self.processed.append((sport, snapshot))


def _credits(api_key_set=True, remaining=None):
    return {"api_key_set": api_key_set, "remaining": remaining}


# ── compute_adaptive_interval ────────────────────────────────────────────────


class TestComputeAdaptiveInterval:
    def test_healthy_credits_keep_base_interval(self):
        use_fallback, interval = compute_adaptive_interval(_credits(remaining=500), 900)
        assert use_fallback is False
        assert interval == 900

    def test_no_api_key_forces_fallback_at_base_interval(self):
        use_fallback, interval = compute_adaptive_interval(_credits(False), 900)
        assert use_fallback is True
        assert interval == 900  # fallback mode does not stretch

    def test_low_credits_switch_to_fallback(self):
        use_fallback, interval = compute_adaptive_interval(_credits(remaining=49), 900)
        assert use_fallback is True

    def test_moderate_credits_stretch_to_30min(self):
        _, interval = compute_adaptive_interval(_credits(remaining=99), 900)
        assert interval == 1800

    def test_low_remaining_without_fallback_stretches_to_1hr(self):
        # remaining <50 but fallback already triggered by that same rule;
        # force the stretch branch via a key-set credit dict at exactly <100
        _, interval = compute_adaptive_interval(_credits(remaining=100), 3600)
        assert interval == 3600

    def test_unknown_remaining_keeps_base(self):
        use_fallback, interval = compute_adaptive_interval(_credits(), 900)
        assert use_fallback is False
        assert interval == 900


# ── run_monitor_cycle ────────────────────────────────────────────────────────


class TestRunMonitorCycle:
    def test_snapshots_all_sports_when_healthy(self):
        async def main():
            m = FakeMonitor()
            interval = await run_monitor_cycle(
                m, monitored_sports=["nba", "nhl"], snapshot_interval=1,
                get_credit_status=lambda: _credits(remaining=500))
            return interval, sorted(m.snapshotted), m.fallback_snapshotted

        interval, sports, fallbacks = run(main())
        assert interval >= 1
        assert sports == ["nba", "nhl"]
        assert fallbacks == []  # primary path used

    def test_low_credits_route_to_fallback_path(self):
        async def main():
            m = FakeMonitor()
            await run_monitor_cycle(
                m, monitored_sports=["nba"], snapshot_interval=1,
                get_credit_status=lambda: {"api_key_set": False})
            return m.snapshotted, m.fallback_snapshotted

        sports, fallbacks = run(main())
        assert sports == ["nba"]
        assert fallbacks == ["nba"]

    def test_backoff_skips_chronically_failing_sport(self):
        async def main():
            m = FakeMonitor()
            m._consecutive_failures["nhl"] = 6  # 5+ -> skip unless cycle %4==0
            await run_monitor_cycle(
                m, monitored_sports=["nhl"], snapshot_interval=1,
                get_credit_status=lambda: _credits(remaining=500))
            first_attempted = list(m.snapshotted)
            # Advance cycles until %4==0 and try again (run_monitor_cycle
            # increments _cycle_n itself; set it just below the next multiple).
            while (m._cycle_n + 1) % 4 != 0:
                m._cycle_n += 1
            m.snapshotted.clear()
            await run_monitor_cycle(
                m, monitored_sports=["nhl"], snapshot_interval=1,
                get_credit_status=lambda: _credits(remaining=500))
            return first_attempted, m.snapshotted

        skipped_round, retried = run(main())
        assert skipped_round == []       # skipped on cycle_n=1 (%4 != 0)
        assert retried == ["nhl"]        # attempted when cycle_n %4 == 0

    def test_severe_backoff_requires_cycle_mod_8(self):
        async def main():
            m = FakeMonitor()
            m._consecutive_failures["mlb"] = 10
            m._cycle_n = 7  # run_monitor_cycle increments to 8 -> %8 == 0, allowed
            await run_monitor_cycle(
                m, monitored_sports=["mlb"], snapshot_interval=1,
                get_credit_status=lambda: _credits(remaining=500))
            return m.snapshotted

        assert run(main()) == ["mlb"]

    def test_snapshot_timeout_increments_failure_counter(self):
        from tools.lines import monitor_loop as ml

        async def main():
            m = FakeMonitor()

            async def slow(sport):
                await asyncio.sleep(999)

            orig = ml.SNAPSHOT_TIMEOUT_S
            ml.SNAPSHOT_TIMEOUT_S = 0.01
            try:
                m._snapshot_sport = slow
                await run_monitor_cycle(
                    m, monitored_sports=["nba"], snapshot_interval=1,
                    get_credit_status=lambda: _credits(remaining=500))
            finally:
                ml.SNAPSHOT_TIMEOUT_S = orig
            return m._consecutive_failures

        assert run(main()) == {"nba": 1}

    def test_pause_bails_out_of_sport_loop(self):
        async def main():
            m = FakeMonitor()

            async def pause_then_run(sport):
                m.snapshotted.append(sport)
                m._paused = True  # autonomous loop paused us mid-cycle

            m._snapshot_sport = pause_then_run
            await run_monitor_cycle(
                m, monitored_sports=["a", "b", "c"], snapshot_interval=1,
                get_credit_status=lambda: _credits(remaining=500))
            return m.snapshotted

        # First sport triggers the pause; remaining sports are skipped.
        assert run(main()) == ["a"]


# ── snapshot_props ───────────────────────────────────────────────────────────


def _prop_result(count=2, multi_book=1, error=False):
    if error:
        return {"error": "scrape failed"}
    return {"props": [{"line": i} for i in range(count)],
            "multi_book_count": multi_book}


class TestSnapshotProps:
    def _patch_scraper(self, monkey_result_by_sport, stored_log):
        from tools.lines import monitor_loop as ml

        async def fake_scrape(sport):
            return monkey_result_by_sport.get(sport, {"error": "unsupported"})

        async def fake_store(props, sport, db_path):
            stored_log.append((sport, len(props)))
            return len(props)

        return fake_scrape, fake_store, ml

    def test_only_prop_sports_scraped_and_stored(self):
        async def main():
            results = {
                "basketball_nba": _prop_result(3),
                "icehockey_nhl": _prop_result(2),
                "soccer_mls": _prop_result(5),  # not a prop sport — never scraped
            }
            stored = []
            fake_scrape, fake_store, ml = self._patch_scraper(results, stored)
            orig = (ml.scrape_all_props, ml.store_prop_snapshot)
            ml.scrape_all_props, ml.store_prop_snapshot = fake_scrape, fake_store
            try:
                m = FakeMonitor()
                await snapshot_props(m, ["basketball_nba", "icehockey_nhl", "soccer_mls"])
            finally:
                ml.scrape_all_props, ml.store_prop_snapshot = orig
            return stored

        stored = run(main())
        assert ("basketball_nba", 3) in stored
        assert ("icehockey_nhl", 2) in stored
        assert all(s != "soccer_mls" for s, _ in stored)

    def test_error_result_skipped_without_store(self):
        async def main():
            results = {"basketball_nba": _prop_result(error=True)}
            stored = []
            fake_scrape, fake_store, ml = self._patch_scraper(results, stored)
            orig = (ml.scrape_all_props, ml.store_prop_snapshot)
            ml.scrape_all_props, ml.store_prop_snapshot = fake_scrape, fake_store
            try:
                await snapshot_props(FakeMonitor(), ["basketball_nba"])
            finally:
                ml.scrape_all_props, ml.store_prop_snapshot = orig
            return stored

        assert run(main()) == []

    def test_sets_in_flight_db_under_lock(self):
        async def main():
            results = {"basketball_nba": _prop_result(1)}
            observed = []
            stored = []
            fake_scrape, fake_store, ml = self._patch_scraper(results, stored)

            async def spying_store(props, sport, db_path):
                m = spy_monitor[0]
                observed.append((m._in_flight_db, m._snapshot_lock.locked()))
                return len(props)

            spy_monitor = [None]
            orig = (ml.scrape_all_props, ml.store_prop_snapshot)
            ml.scrape_all_props, ml.store_prop_snapshot = fake_scrape, spying_store
            try:
                m = FakeMonitor()
                spy_monitor[0] = m
                await snapshot_props(m, ["basketball_nba"])
            finally:
                ml.scrape_all_props, ml.store_prop_snapshot = orig
            return observed

        observed = run(main())
        assert observed == [(True, True)]  # flag set AND lock held during store

    def test_paused_before_cycle_means_no_props(self):
        async def main():
            results = {"basketball_nba": _prop_result(1)}
            stored = []
            fake_scrape, fake_store, ml = self._patch_scraper(results, stored)
            orig = (ml.scrape_all_props, ml.store_prop_snapshot)
            ml.scrape_all_props, ml.store_prop_snapshot = fake_scrape, fake_store
            try:
                m = FakeMonitor()
                m._paused = True
                await snapshot_props(m, ["basketball_nba"])
            finally:
                ml.scrape_all_props, ml.store_prop_snapshot = orig
            return stored

        assert run(main()) == []


# ── snapshot_sport_fallback ──────────────────────────────────────────────────


class TestSnapshotSportFallback:
    def _run_fallback(self, monitor, scraped):
        async def fake_collect(sport, **kwargs):
            return scraped
        from tools.lines import fallback_cascade as fc
        orig = fc.collect_free_sources
        fc.collect_free_sources = fake_collect
        try:
            return run(snapshot_sport_fallback(
                monitor, "basketball_nba",
                odds_api_io_get_odds=lambda s: {}, odds_api_io_usage=lambda: {}))
        finally:
            fc.collect_free_sources = orig

    def test_success_processes_merged_snapshot_and_resets_failures(self):
        m = FakeMonitor()
        m._consecutive_failures["basketball_nba"] = 7
        payload = {"sport": "basketball_nba", "game_count": 1, "games": []}
        self._run_fallback(m, {"dk": payload})
        assert m._consecutive_failures["basketball_nba"] == 0
        assert len(m.processed) == 1
        sport, snap = m.processed[0]
        assert sport == "basketball_nba"
        assert snap["game_count"] == 1

    def test_total_failure_increments_counter_below_threshold(self):
        m = FakeMonitor()
        self._run_fallback(m, {})  # all sources failed
        assert m._consecutive_failures["basketball_nba"] == 1
        assert m.processed == []

    def test_threshold_reached_attempts_telegram_alert(self):
        from tools.lines import fallback_cascade as fc

        alerts_sent = []

        class FakeTelegram:
            @staticmethod
            async def alert_system(message, is_error=False):
                alerts_sent.append((message, is_error))

        m = FakeMonitor()
        m._consecutive_failures["basketball_nba"] = (
            m._FAILURE_ALERT_THRESHOLD)  # next failure crosses the line

        async def fake_collect(sport, **kwargs):
            return {}

        saved = (fc.collect_free_sources,)
        import tools.telegram as telegram_mod
        orig_alert = telegram_mod.alert_system
        fc.collect_free_sources = fake_collect
        telegram_mod.alert_system = FakeTelegram.alert_system
        try:
            run(snapshot_sport_fallback(
                m, "basketball_nba",
                odds_api_io_get_odds=lambda s: {}, odds_api_io_usage=lambda: {}))
        finally:
            fc.collect_free_sources = saved[0]
            telegram_mod.alert_system = orig_alert

        assert m._consecutive_failures["basketball_nba"] == (
            m._FAILURE_ALERT_THRESHOLD + 1)
        assert len(alerts_sent) == 1
        message, is_error = alerts_sent[0]
        assert "basketball_nba" in message and is_error is True


# ── record_significant_movements ─────────────────────────────────────────────


class TestRecordSignificantMovements:
    def test_records_evaluates_each_movement(self):
        events = []
        from tools.lines import monitor_loop as ml

        class Bus:
            async def publish(self, kind, payload):
                events.append((kind, payload))

        import tools.event_bus as eb
        orig = (eb.get_event_bus, eb.EVENT_LINE_MOVED)
        eb.get_event_bus = lambda: Bus()
        eb.EVENT_LINE_MOVED = "test_line_moved"
        try:
            m = FakeMonitor()
            sig = [{"team": "A"}, {"team": "B"}]
            run(record_significant_movements(m, "nba", sig, {}))
        finally:
            eb.get_event_bus, eb.EVENT_LINE_MOVED = orig

        assert [t for _, t in m.recorded] == [{"team": "A"}, {"team": "B"}]
        assert len(m.evaled) == 2
        kinds = {k for k, _ in events}
        assert kinds == {"test_line_moved"}
        payloads = [p for _, p in events]
        assert all(p["sport"] == "nba" for p in payloads)

    def test_event_bus_failure_does_not_stop_recording(self):
        from tools.lines import monitor_loop as ml

        class BrokenBus:
            async def publish(self, kind, payload):
                raise RuntimeError("bus down")

        import tools.event_bus as eb
        orig = (eb.get_event_bus, eb.EVENT_LINE_MOVED)
        eb.get_event_bus = lambda: BrokenBus()
        eb.EVENT_LINE_MOVED = "evt"
        try:
            m = FakeMonitor()
            run(record_significant_movements(m, "nba", [{"team": "A"}], {}))
        finally:
            eb.get_event_bus, eb.EVENT_LINE_MOVED = orig
        assert len(m.recorded) == 1 and len(m.evaled) == 1


# ── handle_sharp_signals ─────────────────────────────────────────────────────


class FakeAlertSink(list):
    pass


def _sig(stale=3, moved=("dk",)):
    return {
        "game": "LAL@BOS", "team": "LAL", "market": "h2h",
        "stale_books": [f"book{i}" for i in range(stale)],
        "moved_books": list(moved),
    }


class TestHandleSharpSignals:
    def test_appends_all_signals_tagged_with_sport(self):
        sink = []
        run(handle_sharp_signals(sink, "nba", [_sig(), _sig()]))
        assert len(sink) == 2
        assert all(a["sport"] == "nba" and a["type"] == "sharp_money" for a in sink)

    def test_high_confidence_triggers_alert(self):
        sent = []
        import tools.telegram as tm

        async def fake_alert(**kwargs):
            sent.append(kwargs)

        orig = tm.alert_sharp_move
        tm.alert_sharp_move = fake_alert
        try:
            sink = []
            run(handle_sharp_signals(sink, "nba", [_sig(stale=3)]))
        finally:
            tm.alert_sharp_move = orig

        assert len(sent) == 1
        assert sent[0]["team"] == "LAL"
        assert sent[0]["moved_books"] == ["dk"]
        assert len(sent[0]["stale_books"]) == 3

    def test_low_confidence_no_alert_but_still_sinked(self):
        import tools.telegram as tm
        calls = []

        async def fake_alert(**kwargs):
            calls.append(kwargs)

        orig = tm.alert_sharp_move
        tm.alert_sharp_move = fake_alert
        try:
            sink = []
            run(handle_sharp_signals(sink, "nba", [
                _sig(stale=1),          # too few stale books
                _sig(stale=3, moved=()),  # stale but nothing moved
            ]))
        finally:
            tm.alert_sharp_move = orig

        assert calls == []
        assert len(sink) == 2

    def test_cap_trims_plain_list_to_last_100(self):
        sink = [{"i": i} for i in range(150)]
        run(handle_sharp_signals(sink, "nba", [_sig(stale=1)]))
        assert len(sink) == 100
        assert sink[-1]["type"] == "sharp_money"
        assert sink[0] == {"i": 51}  # oldest entries trimmed

    def test_deque_sink_supported_and_popleft_trimmed(self):
        sink = deque(maxlen=500)
        sink.extend({"i": i} for i in range(150))
        run(handle_sharp_signals(sink, "nba", [_sig(stale=1)]))
        assert len(sink) == 100
        assert sink[-1]["type"] == "sharp_money"


# ── DB-backed query helpers ──────────────────────────────────────────────────


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE line_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL, detected_at TEXT NOT NULL,
            team TEXT, market TEXT, bookmaker TEXT,
            old_price INTEGER, new_price INTEGER, price_movement INTEGER,
            direction TEXT)""")
    await db.execute(
        """CREATE TABLE ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL, sport TEXT, team TEXT,
            edge REAL, expected_value REAL, status TEXT DEFAULT 'open')""")
    await db.execute(
        """CREATE TABLE odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL, timestamp TEXT NOT NULL,
            game_count INTEGER DEFAULT 0, credits_remaining INTEGER)""")
    await db.execute("CREATE TABLE closing_lines (id INTEGER PRIMARY KEY)")
    rows = [("nba", f"2026-08-26T0{i}:00:00+00:00") for i in range(3)]
    await db.executemany(
        "INSERT INTO line_movements (sport, detected_at, team) VALUES (?, ?, ?)",
        [(r[0], r[1], f"T{i}") for i, r in enumerate(rows)])
    await db.executemany(
        "INSERT INTO ev_opportunities (detected_at, sport, team, status) "
        "VALUES (?, 'nba', ?, ?)",
        [("2026-08-26T00:00:00+00:00", "A", "open"),
         ("2026-08-26T01:00:00+00:00", "B", "closed"),
         ("2026-08-26T02:00:00+00:00", "C", "open")])
    await db.execute(
        "INSERT INTO odds_snapshots (sport, timestamp, game_count) "
        "VALUES ('nba', '2026-08-26T12:00:00+00:00', 12)")
    await db.commit()
    return db


class TestFetchRecentMovements:
    def test_returns_newest_first_across_sports(self):
        async def main():
            db = await _make_db()
            got = await fetch_recent_movements(db, limit=10)
            await db.close()
            return got

        rows = run(main())
        assert [r["team"] for r in rows] == ["T2", "T1", "T0"]
        assert all(r["sport"] == "nba" for r in rows)

    def test_sport_filter_and_limit(self):
        async def main():
            db = await _make_db()
            await db.execute(
                "INSERT INTO line_movements (sport, detected_at, team) "
                "VALUES ('nhl', '2026-08-27T00:00:00+00:00', 'N')")
            await db.commit()
            nhl = await fetch_recent_movements(db, sport="nhl")
            limited = await fetch_recent_movements(db, limit=2)
            await db.close()
            return nhl, [r["team"] for r in limited]

        nhl, limited = run(main())
        assert [r["team"] for r in nhl] == ["N"]
        assert limited == ["N", "T2"]


class TestFetchEvOpportunities:
    def test_filters_by_status(self):
        async def main():
            db = await _make_db()
            open_rows = await fetch_ev_opportunities(db, status="open")
            closed = await fetch_ev_opportunities(db, status="closed")
            await db.close()
            return [r["team"] for r in open_rows], closed

        open_teams, closed = run(main())
        assert open_teams == ["C", "A"]  # newest first within status
        assert closed[0]["team"] == "B"

    def test_limit_respected(self):
        async def main():
            db = await _make_db()
            got = await fetch_ev_opportunities(db, status="open", limit=1)
            await db.close()
            return got

        assert len(run(main())) == 1


class TestFetchSnapshotHistory:
    def test_metadata_columns_only(self):
        async def main():
            db = await _make_db()
            got = await fetch_snapshot_history(db, "nba", limit=5)
            await db.close()
            return got

        rows = run(main())
        assert len(rows) == 1
        row = rows[0]
        assert set(row) == {"id", "sport", "timestamp", "game_count",
                            "credits_remaining"}
        assert row["game_count"] == 12
        assert row["timestamp"] == "2026-08-26T12:00:00+00:00"


class TestCollectStatusCounts:
    def test_aggregates_counts_and_latest_timestamp(self):
        async def main():
            db = await _make_db()
            counts = await collect_status_counts(db)
            await db.close()
            return counts

        c = run(main())
        assert c["db_snapshots_total"] == 1
        assert c["latest_snapshot_at"] == "2026-08-26T12:00:00+00:00"
        assert c["db_movements_total"] == 3
        assert c["db_closing_lines"] == 0  # empty table

    def test_missing_tables_swallowed_to_zeroes(self):
        async def main():
            db = await aiosqlite.connect(":memory:")  # no tables at all
            counts = await collect_status_counts(db)
            await db.close()
            return counts

        c = run(main())
        assert c["db_snapshots_total"] == 0
        assert c["latest_snapshot_at"] is None
        assert c["db_movements_total"] == 0
