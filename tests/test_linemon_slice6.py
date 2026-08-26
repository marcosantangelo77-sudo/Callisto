"""Tests for tools/lines/lifecycle + tools/lines/config (slice-6 extraction).

Covers the final large blocks extracted from tools/line_monitor.py:

lifecycle:
- wait_for_drain — pause flag set, lock acquired, ack+in_flight invariants,
                   timeout path, release-on-retry behavior
- resume_monitor — unpauses, releases a held lock, tolerates unlocked/released
- monitor_loop   — pause/ack handshake, cycle delegation, mid-cycle pause
                   skips the interval sleep, CancelledError exits cleanly,
                   generic errors sleep and continue
- run_one_cycle  — forwards to tools.lines.monitor_loop.run_monitor_cycle
- build_status   — DB-backed counts via collect_status_counts, no-db path,
                   key surface identical to the original inline dict

config:
- every constant re-exported through tools.line_monitor unchanged
- env-driven parsing (intervals are ints, flags are bools)

Facade (LineMonitor):
- import-path stability, delegation of wait_for_drain / resume /
  _monitor_loop / get_status to tools.lines.lifecycle impls
- end-to-end drain/resume handshake against a real LineMonitor
- paper-trade signal surface NOT widened (no 'live', no generate_paper_trade_signal)

No network, no live betting path.
"""

import asyncio
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
    """Minimal stand-in for LineMonitor's lifecycle-contract attributes."""

    def __init__(self, db=None):
        self._running = True
        self._paused = False
        self._pause_ack = asyncio.Event()
        self._in_flight_db = False
        self._snapshot_lock = asyncio.Lock()
        self._snapshots = {}
        self._alerts = deque(maxlen=100)
        self._db = db


# ── lifecycle.wait_for_drain ─────────────────────────────────────────────────


def test_wait_for_drain_returns_true_when_idle():
    from tools.lines.lifecycle import wait_for_drain

    m = FakeMonitor()
    m._pause_ack.set()  # already paused-ack'd, nothing in flight

    drained = run(wait_for_drain(m, timeout=5))

    assert drained is True
    assert m._paused is True
    assert m._snapshot_lock.locked() is True  # caller must hold the lock on success
    from tools.lines.lifecycle import resume_monitor
    resume_monitor(m)


def test_wait_for_drain_sets_paused_before_waiting():
    from tools.lines.lifecycle import wait_for_drain

    m = FakeMonitor()
    m._pause_ack.set()

    async def scenario():
        task = asyncio.create_task(wait_for_drain(m, timeout=5))
        await asyncio.sleep(0.01)
        # _paused must already be True while we're still waiting for drain
        assert m._paused is True
        return await task

    drained = run(scenario())
    assert drained is True
    from tools.lines.lifecycle import resume_monitor
    resume_monitor(m)


def test_wait_for_drain_times_out_when_never_acked():
    from tools.lines.lifecycle import wait_for_drain

    m = FakeMonitor()
    # _pause_ack never set -> can never drain
    start = time.monotonic()
    drained = run(wait_for_drain(m, timeout=1.5))
    elapsed = time.monotonic() - start

    assert drained is False
    assert elapsed >= 1.0
    # Lock must have been released on the retry path so others aren't starved
    assert m._snapshot_lock.locked() is False


def test_wait_for_drain_waits_for_in_flight_db_to_clear():
    from tools.lines.lifecycle import wait_for_drain

    m = FakeMonitor()

    async def scenario():
        m._pause_ack.set()
        m._in_flight_db = True
        result = {}

        async def clearer():
            await asyncio.sleep(0.3)
            m._in_flight_db = False

        task = asyncio.create_task(wait_for_drain(m, timeout=5))
        asyncio.create_task(clearer())
        result["drained"] = await task
        return result

    out = run(scenario())
    assert out["drained"] is True
    from tools.lines.lifecycle import resume_monitor
    resume_monitor(m)


async def _test_drain_excludes_concurrent_snapshot_writer():
    """Security audit C-8: while wait_for_drain holds the lock, no new snapshot."""
    from tools.line_monitor import LineMonitor
    from tools.lines.lifecycle import resume_monitor

    m = LineMonitor(db_path=":memory:")
    started = []

    async def fake_inner(sport, snap):
        started.append(sport)

    async def writer():
        await asyncio.sleep(0.05)
        await m._process_snapshot("basketball_nba", {"games": []})

    async def scenario():
        drain_task = asyncio.create_task(m.wait_for_drain(timeout=3))
        wtask = asyncio.create_task(writer())
        drained = await drain_task
        # Give the writer a chance; it must block on _snapshot_lock.
        await asyncio.sleep(0.2)
        blocked = len(started) == 0
        resume_monitor(m)
        await wtask
        return drained, blocked

    drained, blocked = run(scenario())
    assert drained is True
    assert blocked is True
    assert started == ["basketball_nba"]  # ran only after resume


# ── lifecycle.resume_monitor ─────────────────────────────────────────────────


def test_resume_releases_lock_and_unpauses():
    from tools.lines.lifecycle import resume_monitor, wait_for_drain

    m = FakeMonitor()
    m._pause_ack.set()
    run(wait_for_drain(m, timeout=5))
    assert m._snapshot_lock.locked() is True

    resume_monitor(m)

    assert m._paused is False
    assert m._snapshot_lock.locked() is False


def test_resume_is_safe_when_not_locked_or_not_owned():
    from tools.lines.lifecycle import resume_monitor

    async def scenario():
        m = FakeMonitor()
        await m._snapshot_lock.acquire()  # someone else holds it
        resume_monitor(m)  # must not raise / must not steal the foreign lock
        assert m._paused is False

        m2 = FakeMonitor()
        try:
            m2._snapshot_lock.release()  # simulate already-released state
        except RuntimeError:
            pass
        resume_monitor(m2)  # RuntimeError inside is swallowed
        assert m2._paused is False

    run(scenario())


# ── lifecycle.monitor_loop ───────────────────────────────────────────────────


def test_monitor_loop_delegates_each_cycle_and_sleeps_interval():
    from tools.lines import lifecycle

    m = FakeMonitor()
    intervals = iter([900])
    cycles = []

    async def fake_cycle(monitor, **kwargs):
        cycles.append(kwargs["monitored_sports"])
        return next(intervals)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        m._running = False  # exit after first full cycle

    with patch.object(lifecycle, "run_one_cycle", fake_cycle), \
         patch.object(lifecycle.asyncio, "sleep", fake_sleep):
        run(lifecycle.monitor_loop(
            m, monitored_sports=["basketball_nba"],
            snapshot_interval=900, get_credit_status=dict,
        ))

    assert cycles == [["basketball_nba"]]
    assert sleeps[-1] == 900


def test_monitor_loop_pause_handshake_sets_and_clears_ack():
    from tools.lines import lifecycle

    m = FakeMonitor()
    ack_states = []
    real_sleep = asyncio.sleep

    async def fake_cycle(monitor, **kwargs):
        ack_states.append(("cycle_entry", monitor._pause_ack.is_set()))
        return 10

    async def fake_sleep(seconds):
        if seconds == 5:  # paused-poll sleep
            ack_states.append(("paused_poll", m._pause_ack.is_set()))
            m._paused = False  # unpause externally after one poll
            m._pause_ack.clear()
        elif seconds == 10:
            m._running = False
        return await real_sleep(0)

    m._paused = True

    with patch.object(lifecycle, "run_one_cycle", fake_cycle), \
         patch.object(lifecycle.asyncio, "sleep", fake_sleep):
        run(lifecycle.monitor_loop(
            m, monitored_sports=[], snapshot_interval=10,
            get_credit_status=dict,
        ))

    assert ("paused_poll", True) in ack_states  # ack was SET while paused


def test_monitor_loop_mid_cycle_pause_skips_interval_sleep():
    from tools.lines import lifecycle

    m = FakeMonitor()
    events = []

    async def fake_cycle(monitor, **kwargs):
        events.append("cycle")
        monitor._paused = True  # pause starts mid-cycle
        return 900

    async def fake_sleep(seconds):
        events.append(f"sleep_{seconds}")

    def credit_status():
        events.append("credits")
        return {"api_key_set": False}

    with patch.object(lifecycle, "run_one_cycle", fake_cycle), \
         patch.object(lifecycle.asyncio, "sleep", fake_sleep):
        async def scenario():
            task = asyncio.create_task(lifecycle.monitor_loop(
                m, monitored_sports=["x"], snapshot_interval=900,
                get_credit_status=credit_status,
            ))
            await asyncio.sleep(0.05)
            m._running = False
            m._paused = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        run(scenario())

    assert "sleep_900" not in events  # skipped because paused mid-cycle


def test_monitor_loop_survives_cycle_exception_and_keeps_going():
    from tools.lines import lifecycle

    m = FakeMonitor()
    calls = []

    async def flaky(monitor, **kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("boom")
        m._running = False
        return 5

    async def fake_sleep(seconds):
        pass  # don't actually sleep 30s on error path

    with patch.object(lifecycle, "run_one_cycle", flaky), \
         patch.object(lifecycle.asyncio, "sleep", fake_sleep):
        run(lifecycle.monitor_loop(
            m, monitored_sports=[], snapshot_interval=5,
            get_credit_status=dict,
        ))

    assert calls == [0, 1]  # retried after the error


def test_monitor_loop_exits_on_cancelled_error():
    from tools.lines import lifecycle

    m = FakeMonitor()

    async def cancel_cycle(monitor, **kwargs):
        raise asyncio.CancelledError()

    async def fake_sleep(seconds):
        pass

    with patch.object(lifecycle, "run_one_cycle", cancel_cycle), \
         patch.object(lifecycle.asyncio, "sleep", fake_sleep):
        run(lifecycle.monitor_loop(
            m, monitored_sports=[], snapshot_interval=5,
            get_credit_status=dict,
        ))
    # Reaching here without hanging means the loop broke out cleanly.


def test_run_one_cycle_forwards_to_monitor_loop_impl():
    from tools.lines import lifecycle

    m = FakeMonitor()
    seen = {}

    async def fake_run_monitor_cycle(monitor, *, monitored_sports,
                                    snapshot_interval, get_credit_status):
        seen["monitor"] = monitor
        seen["sports"] = monitored_sports
        seen["interval"] = snapshot_interval
        seen["credits_fn"] = get_credit_status
        return 4242

    with patch("tools.lines.monitor_loop.run_monitor_cycle", fake_run_monitor_cycle):
        got = run(lifecycle.run_one_cycle(
            m, monitored_sports=["s1"], snapshot_interval=7,
            get_credit_status=dict,
        ))

    assert got == 4242
    assert seen["monitor"] is m
    assert seen["sports"] == ["s1"]
    assert seen["interval"] == 7


# ── lifecycle.build_status ──────────────────────────────────────────────────


def test_build_status_without_db_yields_zeroed_counts():
    from tools.lines.lifecycle import build_status

    m = FakeMonitor(db=None)
    m._snapshots = {"basketball_nba": {}, "icehockey_nhl": {}}
    m._alerts.append({"x": 1})
    m._alerts.append({"y": 2})

    status = run(build_status(
        m, monitored_sports=["basketball_nba"], snapshot_interval=900,
        get_credit_status=lambda: {"api_key_set": False},
    ))

    assert status["running"] is True
    assert status["monitored_sports"] == ["basketball_nba"]
    assert status["snapshot_interval_seconds"] == 900
    assert status["cached_snapshots"] == ["basketball_nba", "icehockey_nhl"]
    assert status["db_snapshots_total"] == 0
    assert status["db_movements_total"] == 0
    assert status["db_closing_lines"] == 0
    assert status["latest_snapshot_at"] is None
    assert status["recent_alerts_in_memory"] == 2
    assert status["credits"] == {"api_key_set": False}


async def _test_build_status_with_real_db_counts():
    import tempfile

    import aiosqlite

    from tools.lines.schema import connect_and_tag, ensure_line_schema

    with tempfile.TemporaryDirectory() as td:
        db = await connect_and_tag(os.path.join(td, "t.db"))
        try:
            await ensure_line_schema(db)
            ts = "2026-08-26T00:00:00Z"
            await db.execute(
                "INSERT INTO odds_snapshots (sport, timestamp, game_count, credits_remaining, snapshot_json)"
                " VALUES ('basketball_nba', ?, 3, 400, '{}')", (ts,))
            await db.execute(
                "INSERT INTO line_movements (sport, detected_at, team,"
                " market, direction, ev_analysis)"
                " VALUES ('basketball_nba', ?, 'Lakers', 'h2h', 'up', NULL)",
                (ts,))
            await db.commit()

            from tools.lines.lifecycle import build_status
            m = FakeMonitor(db=db)
            m._running = False
            status = await build_status(
                m, monitored_sports=["basketball_nba"], snapshot_interval=900,
                get_credit_status=lambda: {},
            )
            assert status["db_snapshots_total"] == 1
            assert status["db_movements_total"] == 1
            assert status["latest_snapshot_at"] == ts
        finally:
            await db.close()


def test_build_status_with_real_db_counts():
    run(_test_build_status_with_real_db_counts())


# ── config module ────────────────────────────────────────────────────────────


def test_config_module_exports_all_constants():
    from tools.lines import config as cfg

    assert isinstance(cfg.DB_PATH, str) and cfg.DB_PATH
    assert isinstance(cfg.SNAPSHOT_INTERVAL, int) and cfg.SNAPSHOT_INTERVAL > 0
    assert isinstance(cfg.MONITORED_SPORTS, list) and cfg.MONITORED_SPORTS
    assert isinstance(cfg.WS_SPORTS, str) and cfg.WS_SPORTS
    assert isinstance(cfg.WS_ENABLED, bool)
    assert isinstance(cfg.INCREMENTAL_ENABLED, bool)
    assert isinstance(cfg.INCREMENTAL_INTERVAL, int) and cfg.INCREMENTAL_INTERVAL > 0
    assert isinstance(cfg.REQUIRE_MODEL_AGREEMENT, bool)


def test_config_env_overrides_are_honored(monkeypatch):
    monkeypatch.setenv("ODDS_SNAPSHOT_INTERVAL", "1234")
    monkeypatch.setenv("CALLISTO_INCREMENTAL_INTERVAL_S", "45")
    monkeypatch.setenv("CALLISTO_WS_ENABLED", "0")
    monkeypatch.setenv("CALLISTO_REQUIRE_MODEL_AGREEMENT", "0")

    import importlib

    from tools.lines import config as cfg
    importlib.reload(cfg)

    try:
        assert cfg.SNAPSHOT_INTERVAL == 1234
        assert cfg.INCREMENTAL_INTERVAL == 45
        assert cfg.WS_ENABLED is False
        assert cfg.REQUIRE_MODEL_AGREEMENT is False
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


def test_facade_reexports_config_values_identically():
    import tools.line_monitor as lm
    from tools.lines import config as cfg

    assert lm.DB_PATH == cfg.DB_PATH
    assert lm.SNAPSHOT_INTERVAL == cfg.SNAPSHOT_INTERVAL
    assert lm.MONITORED_SPORTS == cfg.MONITORED_SPORTS
    assert lm.WS_SPORTS == cfg.WS_SPORTS
    assert lm.WS_ENABLED == cfg.WS_ENABLED
    assert lm.INCREMENTAL_ENABLED == cfg.INCREMENTAL_ENABLED
    assert lm.INCREMENTAL_INTERVAL == cfg.INCREMENTAL_INTERVAL
    assert lm.REQUIRE_MODEL_AGREEMENT == cfg.REQUIRE_MODEL_AGREEMENT


# ── Facade stability ────────────────────────────────────────────────────────


def test_facade_import_path_stable_after_slice6():
    from tools.line_monitor import LineMonitor

    assert LineMonitor.__name__ == "LineMonitor"
    for meth in (
        "initialize", "start", "stop", "_monitor_loop", "wait_for_drain",
        "resume", "get_status", "get_edge_report", "force_snapshot",
        "_snapshot_sport", "_process_snapshot",
    ):
        assert hasattr(LineMonitor, meth), f"facade lost method: {meth}"


def test_facade_methods_delegate_to_lifecycle():
    import inspect

    import tools.line_monitor as lm

    src = inspect.getsource(lm.LineMonitor.wait_for_drain)
    assert "_wait_for_drain_impl" in src
    src = inspect.getsource(lm.LineMonitor.resume)
    assert "_resume_monitor_impl" in src
    src = inspect.getsource(lm.LineMonitor._monitor_loop)
    assert "_monitor_loop_body" in src
    src = inspect.getsource(lm.LineMonitor.get_status)
    assert "_build_status_impl" in src


def test_facade_module_reexports_lifecycle_names():
    import tools.line_monitor as lm

    for name in (
        "_wait_for_drain_impl", "_resume_monitor_impl",
        "_monitor_loop_body", "_build_status_impl",
    ):
        assert hasattr(lm, name), f"facade lost re-export: {name}"


async def _test_facade_drain_resume_round_trip():
    from tools.line_monitor import LineMonitor

    m = LineMonitor(db_path=":memory:")
    m._pause_ack.set()
    try:
        drained = await m.wait_for_drain(timeout=5)
        assert drained is True
        assert m._paused is True
        assert m._snapshot_lock.locked() is True
    finally:
        m.resume()

    assert m._paused is False
    assert m._snapshot_lock.locked() is False


def test_facade_drain_resume_round_trip():
    run(_test_facade_drain_resume_round_trip())


async def _test_facade_get_status_matches_legacy_shape():
    from tools.line_monitor import LineMonitor

    m = LineMonitor(db_path=":memory:")
    status = await m.get_status()
    for key in (
        "running", "monitored_sports", "snapshot_interval_seconds",
        "cached_snapshots", "db_snapshots_total", "db_movements_total",
        "db_closing_lines", "latest_snapshot_at", "recent_alerts_in_memory",
        "credits",
    ):
        assert key in status, f"get_status lost key: {key}"
    assert status["running"] is False
    assert status["monitored_sports"] == lm_monitored()


def lm_monitored():
    import tools.line_monitor as lm
    return lm.MONITORED_SPORTS


def test_facade_get_status_matches_legacy_shape():
    run(_test_facade_get_status_matches_legacy_shape())


# ── Guardrails: paper-trade surface NOT widened ──────────────────────────────


def test_paper_trade_signal_statuses_do_not_include_live():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_generate_paper_trade_signal_signature_has_no_live_default():
    """The signal generator must remain paper-only — no live widening."""
    import inspect

    import tools.signals.paper as paper

    fn = getattr(paper, "generate_paper_trade_signal", None)
    if fn is not None:
        sig = inspect.signature(fn)
        for pname, p in sig.parameters.items():
            val = p.default
            if isinstance(val, str):
                assert val != "live", f"param {pname} defaults to 'live'!"


def test_line_monitor_module_mentions_no_live_betting_path():
    import tools.line_monitor as lm

    src_file = inspect_getsourcefile()
    assert src_file.endswith("line_monitor.py")
    # LineMonitor itself exposes no live-bet placement API
    from tools.line_monitor import LineMonitor
    forbidden = ("place_bet", "place_live_bet", "submit_bet")
    for name in dir(LineMonitor):
        assert name not in forbidden, f"forbidden live-betting method appeared: {name}"


def inspect_getsourcefile():
    import inspect
    import tools.line_monitor as lm
    return inspect.getsourcefile(lm)
