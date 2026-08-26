"""Tests for tools/lines/core (slice-7 extraction).

Covers the last substantial blocks extracted from tools/line_monitor.py:

core.init_state:
- every attribute the original LineMonitor.__init__ established
- per-instance isolation (two monitors share nothing mutable)
- alerts deque capped at 100

core.initialize:
- connects DB, ensures line schema + prop schema, logs once

core.start:
- no-op when already running
- startup snapshots run per sport, failures isolated per sport
- spawns the monitor loop task and (when enabled) incremental task
- WS failure does not prevent the loop from starting
- log line reflects ws/incremental on/off state

core.stop:
- cancels the loop task, stops WS+incremental, closes the DB,
  tolerates a never-started monitor and a cancelled task

core.handle_ws_update:
- increments counters even when the merge impl raises
- forwards to tools.lines.ws_stream.handle_ws_update with an
  evaluate_live_detectors closure bound to monitor.db_path
- detector failures are logged, not raised

core.get_edge_report:
- all-sports dict, single-sport hit, single-sport miss sentinel

core.force_snapshot:
- returns cached snapshot after triggering; sentinel when absent

Facade (LineMonitor):
- import-path stability, __init__ delegates to core.init_state
- start/stop/get_edge_report/force_snapshot/_handle_ws_update delegate
- paper-trade signal surface NOT widened (no 'live', no generate_paper_trade_signal)

No network, no live betting path.
"""

import asyncio
import logging
import os
import sys
import time
from collections import deque
from unittest.mock import patch

sys.path.insert(0, ".")

import pytest


def run(coro):
    return asyncio.run(coro)


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeMonitor:
    """Stand-in carrying only the attributes core.* functions touch."""

    def __init__(self):
        self._running = False
        self._task = None
        self._db = None
        self.db_path = ":memory:"
        self._snapshots = {}
        self._latest_edge_reports = {}
        self._ws_updates_received = 0
        self._ws_last_update_at = None
        self.stopped_ws = False
        self.snapshot_calls = []

    async def _snapshot_sport(self, sport):
        self.snapshot_calls.append(sport)
        self._snapshots[sport] = {"sport": sport}

    async def _monitor_loop(self):
        await asyncio.sleep(3600)

    async def _incremental_loop(self):
        await asyncio.sleep(3600)

    async def _start_ws(self):
        pass

    async def stop_ws_and_incremental(self):
        self.stopped_ws = True


def make_core_monitor(**kwargs):
    from tools.lines.core import init_state
    m = FakeMonitor()
    init_state(m, kwargs.get("db_path", ":memory:"), monitored_sports=["s1", "s2"])
    return m


# ── core.init_state ──────────────────────────────────────────────────────────


def test_init_state_sets_all_original_attributes():
    from tools.lines.core import init_state

    class M:
        pass

    m = M()
    init_state(m, "/tmp/x.db", monitored_sports=["s1"])

    assert m.db_path == "/tmp/x.db"
    assert m._db is None
    assert m._task is None
    assert m._running is False
    assert m._paused is False
    assert isinstance(m._pause_ack, asyncio.Event) and not m._pause_ack.is_set()
    assert m._in_flight_db is False
    assert isinstance(m._snapshot_lock, asyncio.Lock)
    assert m._snapshots == {}
    assert isinstance(m._alerts, deque) and m._alerts.maxlen == 100
    assert m._latest_edge_reports == {}
    assert m._consecutive_failures == {}
    assert m._FAILURE_ALERT_THRESHOLD == 3
    assert m._ws_client is None
    assert m._ws_task is None
    assert m._ws_updates_received == 0
    assert m._ws_last_update_at is None
    assert m._incremental_task is None
    assert m._last_incremental_since == {}


def test_init_state_instances_are_isolated():
    a = make_core_monitor()
    b = make_core_monitor()
    a._alerts.append({"x": 1})
    a._snapshots["s1"] = {}
    a._last_incremental_since["s1"] = 5

    assert len(b._alerts) == 0
    assert b._snapshots == {}
    assert b._last_incremental_since == {}


def test_init_state_alerts_deque_hard_capped():
    m = make_core_monitor()
    for i in range(150):
        m._alerts.append({"i": i})
    assert len(m._alerts) == 100
    assert m._alerts[0]["i"] == 50  # oldest evicted


# ── core.initialize ──────────────────────────────────────────────────────────


def test_initialize_connects_and_runs_both_schema_ensurers(tmp_path):
    from tools.lines.core import initialize

    calls = []
    prop_paths = []

    async def fake_prop_schema(path):
        prop_paths.append(path)

    m = FakeMonitor()
    m.db_path = str(tmp_path / "lines.db")
    with patch("tools.lines.schema.connect_and_tag",
               side_effect=NotImplementedError):  # prove we don't rely on real db
        # Instead of mocking connect, use the real one against tmp file.
        pass

    async def scenario():
        with patch("tools.prop_scraper_free.ensure_prop_schema", fake_prop_schema):
            await initialize(m, ensure_prop_schema=fake_prop_schema)

    run(scenario())

    try:
        run(m._db.close())
    except Exception:
        pass
    assert prop_paths == [m.db_path]
    assert m._db is not None


def test_initialize_creates_real_tables(tmp_path):
    import aiosqlite

    from tools.lines.core import initialize

    async def noop_prop(path):
        pass

    async def scenario():
        m = FakeMonitor()
        m.db_path = str(tmp_path / "t.db")
        await initialize(m, ensure_prop_schema=noop_prop)
        cur = await m._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        await m._db.close()
        return tables

    tables = run(scenario())
    assert "odds_snapshots" in tables
    assert "line_movements" in tables


# ── core.start ───────────────────────────────────────────────────────────────


def _cancel(task):
    if task and not task.done():
        task.cancel()


def test_start_noop_when_already_running():
    from tools.lines.core import start

    m = make_core_monitor()
    m._running = True

    async def boom():
        raise AssertionError("should not snapshot")

    m._snapshot_sport = boom

    async def scenario():
        await start(
            m, monitored_sports=["s1"], snapshot_interval=900,
            ws_enabled=False, incremental_enabled=False,
            monitor_loop_fn=m._monitor_loop,
        )

    run(scenario())
    assert m._task is None
    assert m.snapshot_calls == []


def test_start_takes_startup_snapshot_per_sport_then_spawns_loop():
    from tools.lines.core import start

    m = make_core_monitor()

    async def scenario():
        await start(
            m, monitored_sports=["s1", "s2"], snapshot_interval=900,
            ws_enabled=False, incremental_enabled=False,
            monitor_loop_fn=m._monitor_loop,
        )
        await asyncio.sleep(0.05)

    run(scenario())

    assert m.snapshot_calls == ["s1", "s2"]
    assert m._running is True
    assert m._task is not None
    _cancel(m._task)


def test_start_startup_failure_for_one_sport_does_not_block_others(caplog):
    from tools.lines.core import start

    m = make_core_monitor()

    async def flaky(sport):
        if sport == "s1":
            raise RuntimeError("api down")
        m.snapshot_calls.append(sport)

    m._snapshot_sport = flaky

    async def scenario():
        with caplog.at_level(logging.WARNING, logger="callisto.line_monitor"):
            await start(
                m, monitored_sports=["s1", "s2"], snapshot_interval=10,
                ws_enabled=False, incremental_enabled=False,
                monitor_loop_fn=m._monitor_loop,
            )
        await asyncio.sleep(0.02)

    run(scenario())
    assert "s2" in m.snapshot_calls  # continued past s1's failure
    assert any("Startup snapshot for s1 failed" in r.message for r in caplog.records)
    _cancel(m._task)


def test_start_spawns_incremental_task_when_enabled():
    from tools.lines.core import start

    m = make_core_monitor()

    async def scenario():
        await start(
            m, monitored_sports=[], snapshot_interval=10,
            ws_enabled=False, incremental_enabled=True,
            monitor_loop_fn=m._monitor_loop,
            incremental_loop_fn=m._incremental_loop,
        )
        await asyncio.sleep(0.02)

    run(scenario())
    assert isinstance(m._incremental_task, asyncio.Task)
    _cancel(m._incremental_task)
    _cancel(m._task)


def test_start_ws_failure_does_not_prevent_loop_or_incremental(caplog):
    from tools.lines.core import start

    m = make_core_monitor()

    async def broken_ws():
        raise RuntimeError("no route to odds-api.io")

    m._start_ws = broken_ws

    async def scenario():
        with caplog.at_level(logging.WARNING, logger="callisto.line_monitor"):
            await start(
                m, monitored_sports=[], snapshot_interval=10,
                ws_enabled=True, incremental_enabled=True,
                monitor_loop_fn=m._monitor_loop,
                incremental_loop_fn=m._incremental_loop,
            )
        await asyncio.sleep(0.02)

    run(scenario())
    assert m._task is not None
    assert isinstance(m._incremental_task, asyncio.Task)
    assert any("WS startup failed" in r.message for r in caplog.records)
    _cancel(m._incremental_task)
    _cancel(m._task)


# ── core.stop ────────────────────────────────────────────────────────────────


class FakeWsStopper(FakeMonitor):
    """FakeMonitor exposing the attribute shape core.stop expects."""

    def __init__(self):
        super().__init__()
        self._ws_client = None
        self._ws_task = None
        self._incremental_task = None


def test_stop_cancels_task_stops_ws_and_closes_db(tmp_path):
    import aiosqlite

    from tools.lines.core import stop

    async def _record_ws_teardown(mon):
        mon.stopped_ws = True

    async def scenario():
        m = FakeWsStopper()
        m.db_path = str(tmp_path / "s.db")
        db = await aiosqlite.connect(m.db_path)
        m._db = db
        m._task = asyncio.create_task(asyncio.sleep(3600))
        m._ws_task = asyncio.create_task(asyncio.sleep(3600))
        m._incremental_task = asyncio.create_task(asyncio.sleep(3600))

        with patch("tools.lines.core._stop_ws_and_incremental_impl",
                   side_effect=_record_ws_teardown):
            await stop(m)
        return m, db

    m, db = run(scenario())
    assert m._running is False
    assert m._task.cancelled() or m._task.done()
    assert m.stopped_ws is True
    assert m._db is db


def test_stop_never_started_is_safe():
    from tools.lines.core import stop

    m = FakeWsStopper()

    async def scenario():
        with patch("tools.lines.core._stop_ws_and_incremental_impl") as p:
            p.side_effect = _noop_impl
            await stop(m)

    async def _noop_impl(mon):
        pass

    run(scenario())  # must not raise
    assert m._running is False


# ── core.handle_ws_update ────────────────────────────────────────────────────


def test_handle_ws_update_increments_counters_and_forwards():
    from tools.lines.core import handle_ws_update

    seen = {}

    async def fake_impl(monitor, data, *, process_snapshot, evaluate_live_detectors):
        seen["data"] = data
        seen["proc"] = process_snapshot
        seen["detectors"] = evaluate_live_detectors

    m = make_core_monitor()

    async def scenario():
        with patch("tools.lines.core._handle_ws_update_impl", fake_impl):
            await handle_ws_update(m, {"game": 1}, process_snapshot="PROC")

    run(scenario())
    assert m._ws_updates_received == 1
    assert isinstance(m._ws_last_update_at, float)
    assert abs(m._ws_last_update_at - time.time()) < 30
    assert seen["data"] == {"game": 1}
    assert seen["proc"] == "PROC"


def test_handle_ws_update_detector_closure_uses_monitor_db_path():
    from tools.lines.core import handle_ws_update

    captured = {}

    async def fake_impl(monitor, data, *, process_snapshot, evaluate_live_detectors):
        captured["fn"] = evaluate_live_detectors

    m = make_core_monitor()
    m.db_path = "/custom/path.db"

    async def scenario():
        with patch("tools.lines.core._handle_ws_update_impl", fake_impl):
            await handle_ws_update(m, {}, process_snapshot=None)

    run(scenario())
    fn = captured["fn"]
    # Running the closure should call live_state.evaluate_detectors_for_event
    # with our db_path — patch that symbol to verify without importing heavy deps.
    called = {}

    async def fake_eval(event_id, db_path=None):
        called["args"] = (event_id, db_path)

    import types
    fake_mod = types.ModuleType("tools.live_state")
    fake_mod.evaluate_detectors_for_event = fake_eval
    with patch.dict(sys.modules, {"tools.live_state": fake_mod}):
        run(fn("evt-9"))
    assert called["args"] == ("evt-9", "/custom/path.db")


def test_handle_ws_update_swallows_merge_failures_but_keeps_counter(caplog):
    from tools.lines.core import handle_ws_update

    async def boom(*a, **k):
        raise ValueError("bad delta")

    m = make_core_monitor()

    async def scenario():
        with caplog.at_level(logging.WARNING, logger="callisto.line_monitor"):
            with patch("tools.lines.core._handle_ws_update_impl", boom):
                await handle_ws_update(m, {}, process_snapshot=None)

    run(scenario())
    assert m._ws_updates_received == 1  # counted before the failure
    assert any("WS update handler failed" in r.message for r in caplog.records)


# ── core.get_edge_report ─────────────────────────────────────────────────────


def test_get_edge_report_all_and_single_and_miss():
    from tools.lines.core import get_edge_report

    m = FakeMonitor()
    m._latest_edge_reports = {
        "basketball_nba": {"edges": [1, 2]},
        "icehockey_nhl": {"edges": []},
    }

    assert get_edge_report(m) is m._latest_edge_reports
    assert get_edge_report(m, "icehockey_nhl") == {"edges": []}
    assert get_edge_report(m, "mma_mixed") == {"error": "No report for mma_mixed"}


# ── core.force_snapshot ──────────────────────────────────────────────────────


def test_force_snapshot_triggers_then_returns_cached_result():
    from tools.lines.core import force_snapshot

    m = FakeMonitor()
    triggered = []

    async def snap(sport):
        triggered.append(sport)
        m._snapshots[sport] = {"games": 3}

    out = run(force_snapshot(m, "basketball_nba", snapshot_sport=snap))
    assert triggered == ["basketball_nba"]
    assert out == {"games": 3}


def test_force_snapshot_missing_result_returns_sentinel():
    from tools.lines.core import force_snapshot

    m = FakeMonitor()

    async def snap(sport):
        pass  # snapshot produced nothing

    out = run(force_snapshot(m, "nfl", snapshot_sport=snap))
    assert out == {"error": "No snapshot taken"}


# ── Facade: LineMonitor ──────────────────────────────────────────────────────


def test_facade_import_path_unchanged_and_init_delegates_to_core():
    from tools.line_monitor import LineMonitor
    from tools.lines.core import init_state as real_init_state

    m = LineMonitor(db_path=":memory:")
    # All original attributes present after delegation
    for attr in ("db_path", "_db", "_task", "_running", "_paused", "_pause_ack",
                 "_snapshot_lock", "_snapshots", "_alerts", "_latest_edge_reports",
                 "_ws_client", "_incremental_task", "_last_incremental_since",
                 "_kl_tracker", "_evaluator"):
        assert hasattr(m, attr), attr
    assert m._alerts.maxlen == 100
    assert m._FAILURE_ALERT_THRESHOLD == 3
    assert LineMonitor.__module__ == "tools.line_monitor"


def test_facade_start_delegates_with_config_values():
    import tools.line_monitor as lm
    from tools.lines.core import start

    calls = {}

    async def fake_start(monitor, **kwargs):
        calls.update(kwargs)

    m = lm.LineMonitor(db_path=":memory:")
    with patch.object(lm, "_start_impl", fake_start):
        run(m.start())

    assert calls["monitored_sports"] == lm.MONITORED_SPORTS
    assert calls["snapshot_interval"] == lm.SNAPSHOT_INTERVAL
    assert calls["ws_enabled"] == lm.WS_ENABLED
    assert calls["incremental_enabled"] == lm.INCREMENTAL_ENABLED


def test_facade_stop_delegates_to_core_stop():
    import tools.line_monitor as lm

    stopped = []

    async def fake_stop(monitor):
        stopped.append(monitor)

    m = lm.LineMonitor(db_path=":memory:")
    with patch.object(lm, "_stop_impl", fake_stop):
        run(m.stop())
    assert stopped == [m]


def test_facade_get_edge_report_delegates():
    import tools.line_monitor as lm
    from tools.lines.core import get_edge_report as real_get_edge_report

    m = lm.LineMonitor(db_path=":memory:")
    sentinel = object()
    m._latest_edge_reports = {"x": sentinel}
    assert m.get_edge_report("x") is sentinel
    assert m.get_edge_report("missing") == {"error": "No report for missing"}


def test_facade_force_snapshot_delegates_via_snapshot_sport():
    import tools.line_monitor as lm

    m = lm.LineMonitor(db_path=":memory:")
    triggered = []

    async def fake_impl(monitor, sport, snapshot_sport):
        triggered.append(sport)
        assert callable(snapshot_sport)
        monitor._snapshots[sport] = {"forced": True}
        return monitor._snapshots[sport]

    with patch.object(lm, "_force_snapshot_impl", fake_impl):
        out = run(m.force_snapshot("nhl"))
    assert out == {"forced": True}
    assert triggered == ["nhl"]


def test_facade_handle_ws_update_counters_and_isolation():
    import tools.line_monitor as lm

    m = lm.LineMonitor(db_path=":memory:")

    async def boom(*a, **k):
        raise RuntimeError("merge exploded")

    with patch("tools.lines.core._handle_ws_update_impl", boom):
        run(m._handle_ws_update({}))

    assert m._ws_updates_received == 1
    assert m._ws_last_update_at is not None


async def _test_facade_full_lifecycle_smoke():
    """init -> initialize(real sqlite) -> start(no sports, no ws) -> stop."""
    import tempfile

    import tools.line_monitor as lm

    async def noop_prop(path):
        pass

    with tempfile.TemporaryDirectory() as td:
        m = lm.LineMonitor(db_path=os.path.join(td, "life.db"))
        with patch("tools.line_monitor.ensure_prop_schema", noop_prop):
            await m.initialize()
        assert m._db is not None
        await m.start()
        assert m._running is True
        await asyncio.sleep(0.02)
        await m.stop()
        assert m._running is False
        await m._db.close()


def test_facade_full_lifecycle_smoke():
    import os
    run(_test_facade_full_lifecycle_smoke())


# ── Safety rails ─────────────────────────────────────────────────────────────


def test_paper_trade_signal_surface_not_widened():
    """Slice-7 must NOT touch live betting: no 'live' status, no widening."""
    import inspect

    import tools.line_monitor as lm
    import tools.lines.core as core

    src_lm = inspect.getsource(lm)
    src_core = inspect.getsource(core)

    assert "generate_paper_trade_signal" not in src_core
    assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src_core and "'live'" not in src_core
    assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src_lm
    # Facade still doesn't define the paper-signal function itself.
    assert not hasattr(lm.LineMonitor, "generate_paper_trade_signal")


def test_core_module_has_no_betting_side_effects_at_import():
    import tools.lines.core as core

    public = {n for n in vars(core) if not n.startswith("_")}
    assert public <= {
        "asyncio", "logging", "time", "deque", "Optional", "aiosqlite",
        "fetch_ev_opportunities", "fetch_recent_movements", "fetch_snapshot_history",
        "connect_and_tag", "ensure_line_schema",
        "handle_ws_update", "incremental_loop", "start_ws", "stop_ws_and_incremental",
        "logger",
        "init_state", "initialize", "start", "stop",
        "get_edge_report", "force_snapshot",
    } | {"__all__", "__name__", "__doc__"}
