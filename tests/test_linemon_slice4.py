"""Tests for tools/lines/ws_stream + tools/lines/schema (slice-4 extraction).

Covers the last large blocks extracted from tools/line_monitor.py:

ws_stream:
- start_ws                 — wires OddsWebSocket with monitor callback + sports
- stop_ws_and_incremental  — isolated teardown of WS client + incremental task
- handle_ws_update         — snapshot routing, ingest_source tag, live-edge
                             detector piggy-back, failure isolation
- ws_status_fields         — telemetry shape, client status merge
- incremental_loop         — since-cursor tracking, pause handling, delta
                             conversion through the shared pipeline

schema:
- SCHEMA_STATEMENTS / ensure_line_schema — real DDL against aiosqlite
- connect_and_tag — connection opens and is tagged for WriteCoordinator

Also pins the LineMonitor facade: import path stability, back-compat
wrapper delegation, and that the module still re-exports its public names.

No network, no live betting path, no paper-signal changes are touched.
"""

import asyncio
import sys
import time
from collections import deque

sys.path.insert(0, ".")

import aiosqlite
import pytest


def run(coro):
    return asyncio.run(coro)


# ── Fake collaborator objects ────────────────────────────────────────────────


class FakeWSClient:
    instances = []

    def __init__(self, on_update=None, sports=None):
        self.on_update = on_update
        self.sports = sports
        self.started = False
        self.stopped = False
        FakeWSClient.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def get_status(self):
        return {"connected": not self.stopped}


class FakeMonitor:
    """Mirrors the LineMonitor state surface used by ws_stream helpers."""

    # Class-level config knobs read by ws_stream
    WS_SPORTS = "basketball,ice-hockey"
    WS_ENABLED = True
    INCREMENTAL_ENABLED = True
    INCREMENTAL_INTERVAL = 0  # don't sleep in tests
    REQUIRE_MODEL_AGREEMENT = True

    def __init__(self):
        self._running = True
        self._paused = False
        self._ws_client = None
        self._incremental_task = None
        self._ws_updates_received = 0
        self._ws_last_update_at = None
        self._last_incremental_since = {}
        self.processed = []          # (sport_key, snap) tuples from _process_snapshot
        self.detector_events = []    # event ids passed to live detectors
        self.fail_process_for = set()  # sport keys that raise in _process_snapshot

    async def _process_snapshot(self, sport, snap):
        if sport in self.fail_process_for:
            raise RuntimeError(f"boom {sport}")
        self.processed.append((sport, snap))

    async def _eval_detectors(self, event_id):
        self.detector_events.append(event_id)


class FakeIncrementalFetch:
    """Callable standing in for odds_api_io.get_odds_updated."""

    def __init__(self, results=None, error_once_for=None):
        self.calls = []              # (since, sport)
        self.results = results or {}  # sport -> list[update]
        self.error_once_for = error_once_for or set()
        self.failed_sports = set()

    async def __call__(self, since, sport=None):
        self.calls.append((since, sport))
        if sport in self.error_once_for and sport not in self.failed_sports:
            self.failed_sports.add(sport)
            raise RuntimeError("transient")
        return {"updates": self.results.get(sport, [])}


# ── ws_update payloads ──────────────────────────────────────────────────────


def make_ws_payload(event_id="g1"):
    """Minimal odds-api.io WS message shape understood by ingest."""
    return {
        "id": event_id,
        "sport": "basketball",
        "league": "NBA",
        "home": "Lakers",
        "away": "Celtics",
        "bookie": "draftkings",
        "markets": [
            {
                "name": "ML",
                "outcomes": [
                    {"name": "Lakers", "price": -110},
                    {"name": "Celtics", "price": 105},
                ],
            }
        ],
    }


# ── schema ───────────────────────────────────────────────────────────────────


def _table_names(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {row[0] for row in cur.fetchall()}


async def _atable_names(db):
    return [r[0] for r in await db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def test_schema_statements_are_valid_ddl():
    run(_test_schema_statements_are_valid_ddl())


async def _test_schema_statements_are_valid_ddl():
    from tools.lines.schema import SCHEMA_STATEMENTS

    assert len(SCHEMA_STATEMENTS) == 6  # 3 tables + 3 indexes
    db = await aiosqlite.connect(":memory:")
    try:
        for stmt in SCHEMA_STATEMENTS:
            await db.execute(stmt)
        await db.commit()
        tables = set(await _atable_names(db))
        assert {"odds_snapshots", "line_movements", "ev_opportunities"} <= tables
        # Defaults survive round-trip
        cur = await db.execute(
            "INSERT INTO ev_opportunities (detected_at) VALUES ('t') "
            "RETURNING status, source"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row == ("open", "line_movement")
    finally:
        await db.close()


def test_ensure_line_schema_is_idempotent():
    run(_test_ensure_line_schema_is_idempotent())


async def _test_ensure_line_schema_is_idempotent():
    from tools.lines.schema import ensure_line_schema

    db = await aiosqlite.connect(":memory:")
    try:
        await ensure_line_schema(db)
        await ensure_line_schema(db)  # IF NOT EXISTS — second pass must not raise
        tables = set(await _atable_names(db))
        assert {"odds_snapshots", "line_movements", "ev_opportunities"} <= tables
    finally:
        await db.close()


def test_connect_and_tag_opens_usable_connection():
    run(_test_connect_and_tag_opens_usable_connection())


async def _test_connect_and_tag_opens_usable_connection():
    from tools.lines.schema import connect_and_tag

    db = await connect_and_tag(":memory:")
    try:
        cur = await db.execute("PRAGMA busy_timeout")
        (val,) = await cur.fetchone()
        await cur.close()
        assert val == 120000
        await db.execute("CREATE TABLE t (x)")
        await db.commit()
    finally:
        await db.close()


# ── ws_stream.start_ws / stop_ws_and_incremental ────────────────────────────


def test_start_ws_wires_client(monkeypatch):
    import types
    import sys
    import tools.lines.ws_stream as wss

    fake_mod = types.ModuleType("tools.odds_ws")
    fake_odds_ws_cls = FakeWSClient
    # Mirror OddsWebSocket's constructor signature via *args/**kwargs shim
    class Shim:
        def __init__(self, on_update=None, sports=None):
            self._fake = fake_odds_ws_cls(on_update=on_update, sports=sports)
            self.sports = sports

        async def start(self):
            await self._fake.start()

    fake_mod.OddsWebSocket = Shim
    monkeypatch.setitem(sys.modules, "tools.odds_ws", fake_mod)

    class M(FakeMonitor):
        async def _handle_ws_update(self, data):
            pass

    m = M()
    run(wss.start_ws(m))
    assert m._ws_client is not None
    assert m._ws_client.sports == FakeMonitor.WS_SPORTS


def test_stop_ws_tolerates_errors_and_clears_state():
    run(_test_stop_ws_tolerates_errors_and_clears_state())


async def _test_stop_ws_tolerates_errors_and_clears_state():
    import tools.lines.ws_stream as wss

    m = FakeMonitor()

    class BadClient:
        async def stop(self):
            raise RuntimeError("already dead")

    m._ws_client = BadClient()

    async def hang():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(hang())
    m._incremental_task = task

    # Must NOT raise despite BadClient.stop failing; must cancel incremental.
    await wss.stop_ws_and_incremental(m)
    assert m._ws_client is None
    assert m._incremental_task is None
    assert task.cancelled()


# ── ws_stream.handle_ws_update ───────────────────────────────────────────────


def test_handle_ws_update_routes_through_pipeline():
    run(_test_handle_ws_update_routes_through_pipeline())


async def _test_handle_ws_update_routes_through_pipeline():
    import tools.lines.ws_stream as wss

    m = FakeMonitor()
    await wss.handle_ws_update(
        m, make_ws_payload(),
        process_snapshot=m._process_snapshot,
        evaluate_live_detectors=m._eval_detectors,
    )
    assert len(m.processed) == 1
    sport, snap = m.processed[0]
    assert isinstance(sport, str) and sport
    assert snap["ingest_source"] == "ws"
    # Live-edge detectors fired for the event id in the payload
    assert m.detector_events == ["g1"]


def test_handle_ws_update_ignores_unmappable_payload():
    run(_test_handle_ws_update_ignores_unmappable_payload())


async def _test_handle_ws_update_ignores_unmappable_payload():
    import tools.lines.ws_stream as wss

    m = FakeMonitor()
    await wss.handle_ws_update(
        m, {},  # nothing usable
        process_snapshot=m._process_snapshot,
        evaluate_live_detectors=m._eval_detectors,
    )
    assert m.processed == []
    assert m.detector_events == []


def test_handle_ws_update_detector_failure_does_not_break_ingestion():
    run(_test_handle_ws_update_detector_failure_does_not_break_ingestion())


async def _test_handle_ws_update_detector_failure_does_not_break_ingestion():
    import tools.lines.ws_stream as wss

    m = FakeMonitor()

    async def bad_detector(event_id):
        raise RuntimeError("detector down")

    await wss.handle_ws_update(
        m, make_ws_payload(),
        process_snapshot=m._process_snapshot,
        evaluate_live_detectors=bad_detector,
    )
    # Snapshot was still ingested even though every detector call raised.
    assert len(m.processed) == 1


def test_handle_ws_update_propagates_pipeline_failure_to_wrapper():
    run(_test_handle_ws_update_propagates_pipeline_failure_to_wrapper())


async def _test_handle_ws_update_propagates_pipeline_failure_to_wrapper():
    import tools.lines.ws_stream as wss

    m = FakeMonitor()
    m.fail_process_for = {s for s in ["basketball_nba"]}

    payload = make_ws_payload()
    mapped_sport = None
    try:
        await wss.handle_ws_update(
            m, payload,
            process_snapshot=m._process_snapshot,
        )
    except RuntimeError as e:
        mapped_sport = "raised"
        assert "boom" in str(e)
    assert mapped_sport == "raised"  # impl surfaces errors; LineMonitor wrapper swallows


def test_facade_handle_ws_update_swallows_and_counts():
    run(_test_facade_handle_ws_update_swallows_and_counts())


async def _test_facade_handle_ws_update_swallows_and_counts():
    """LineMonitor._handle_ws_update increments counters and never raises."""
    from tools.line_monitor import LineMonitor

    lm = LineMonitor(db_path=":memory:")
    lm._process_snapshot = _failing_process  # type: ignore[method-assign]
    before = time.time()
    await lm._handle_ws_update(make_ws_payload())
    assert lm._ws_updates_received == 1
    assert lm._ws_last_update_at is not None
    assert before - 5 <= lm._ws_last_update_at <= time.time() + 5


async def _failing_process(sport, snap):
    raise RuntimeError("pipeline exploded")


# ── ws_stream.ws_status_fields ───────────────────────────────────────────────


def test_ws_status_fields_shape_without_client():
    m = FakeMonitor()
    m._ws_last_update_at = time.time() - 12.0
    m._ws_updates_received = 7

    import tools.lines.ws_stream as wss
    status = wss.ws_status_fields(m)
    assert status["ws_enabled"] is True
    assert status["incremental_enabled"] is True
    assert status["require_model_agreement"] is True
    assert status["ws_updates_received"] == 7
    assert status["ws_last_update_ago_s"] == pytest.approx(12.0, abs=2.0)
    assert "ws_client" not in status


def test_ws_status_fields_merges_client_status():
    m = FakeMonitor()
    m._ws_client = FakeWSClient()

    import tools.lines.ws_stream as wss
    status = wss.ws_status_fields(m)
    assert status["ws_client"] == {"connected": True}


def test_ws_status_fields_survives_broken_client_get_status():
    class Broken:
        def get_status(self):
            raise RuntimeError("nope")

    m = FakeMonitor()
    m._ws_client = Broken()

    import tools.lines.ws_stream as wss
    status = wss.ws_status_fields(m)
    assert "ws_client" not in status
    assert status["ws_updates_received"] == 0


# ── ws_stream.incremental_loop ───────────────────────────────────────────────


def test_incremental_loop_polls_converts_and_tracks_cursor(monkeypatch):
    run(_test_incremental_loop_polls_converts_and_tracks_cursor(monkeypatch))


async def _test_incremental_loop_polls_converts_and_tracks_cursor(monkeypatch):
    import tools.lines.ws_stream as wss
    import tools.odds_api_io as io

    fetch = FakeIncrementalFetch(results={
        "basketball_nba": [make_ws_payload("e1"), make_ws_payload("e2")],
        "icehockey_nhl": [],
    })
    monkeypatch.setattr(io, "get_odds_updated", fetch, raising=False)
    # ws_stream imports get_odds_updated inside the loop from tools.odds_api_io
    monkeypatch.setitem(sys.modules, "tools.odds_api_io", io)

    m = FakeMonitor()
    m.INCREMENTAL_INTERVAL = 0
    stop_after = {"n": 0}

    orig_sleep = asyncio.sleep

    async def counting_sleep(_):
        stop_after["n"] += 1
        if stop_after["n"] >= 1:
            m._running = False  # checked at next while-top; this pass polls once
        await orig_sleep(0)

    monkeypatch.setattr(wss.asyncio, "sleep", counting_sleep)

    await wss.incremental_loop(m, monitored_sports=["basketball_nba", "icehockey_nhl"])

    sports_called = {sport for _, sport in fetch.calls}
    assert sports_called == {"basketball_nba", "icehockey_nhl"}
    # One polling pass × two updates converted through the shared pipeline
    assert len(m.processed) == 2
    for sport, snap in m.processed:
        assert snap["ingest_source"] == "incremental"
    # Cursor advanced per-sport to the poll-time unix seconds
    assert set(m._last_incremental_since) == {"basketball_nba", "icehockey_nhl"}
    now_unix = int(time.time())
    for since in m._last_incremental_since.values():
        assert now_unix - 120 <= since <= now_unix + 5


def test_incremental_loop_continues_past_fetch_error(monkeypatch):
    run(_test_incremental_loop_continues_past_fetch_error(monkeypatch))


async def _test_incremental_loop_continues_past_fetch_error(monkeypatch):
    import tools.lines.ws_stream as wss
    import tools.odds_api_io as io

    fetch = FakeIncrementalFetch(
        results={"basketball_nba": [make_ws_payload()]},
        error_once_for={"icehockey_nhl"},
    )
    monkeypatch.setattr(io, "get_odds_updated", fetch, raising=False)

    m = FakeMonitor()
    passes = {"n": 0}
    orig_sleep = asyncio.sleep

    async def counting_sleep(_):
        passes["n"] += 1
        if passes["n"] >= 2:
            m._running = False
        await orig_sleep(0)

    monkeypatch.setattr(wss.asyncio, "sleep", counting_sleep)

    await wss.incremental_loop(m, monitored_sports=["icehockey_nhl", "basketball_nba"])
    # The errored sport didn't kill the loop; the healthy one still ingested.
    assert len(fetch.failed_sports) == 1
    assert any(s == "basketball_nba" for s, _ in m.processed)


def test_incremental_loop_paused_skips_polling(monkeypatch):
    run(_test_incremental_loop_paused_skips_polling(monkeypatch))


async def _test_incremental_loop_paused_skips_polling(monkeypatch):
    import tools.lines.ws_stream as wss
    import tools.odds_api_io as io

    fetch = FakeIncrementalFetch(results={"basketball_nba": [make_ws_payload()]})
    monkeypatch.setattr(io, "get_odds_updated", fetch, raising=False)

    m = FakeMonitor()
    m._paused = True
    passes = {"n": 0}

    orig_sleep = asyncio.sleep

    async def flip_pause(_):
        passes["n"] += 1
        if passes["n"] == 2:
            m._paused = False
            m._running = False  # finish after the current pass's single poll
        await orig_sleep(0)

    monkeypatch.setattr(wss.asyncio, "sleep", flip_pause)

    await wss.incremental_loop(m, monitored_sports=["basketball_nba"])
    # Exactly one polling pass happened (after unpause), not one per tick while paused.
    assert len(fetch.calls) == len({"basketball_nba"}) * 1


def test_incremental_loop_missing_dependency_disables_cleanly(monkeypatch):
    run(_test_incremental_loop_missing_dependency_disables_cleanly(monkeypatch))


async def _test_incremental_loop_missing_dependency_disables_cleanly(monkeypatch):
    import tools.lines.ws_stream as wss

    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def fake_import(name, *a, **kw):
        if name == "tools.odds_api_io":
            raise ImportError("gone")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)

    m = FakeMonitor()
    await wss.incremental_loop(m, monitored_sports=["basketball_nba"])  # returns immediately
    assert m.processed == []
    assert m._running is True  # loop exited via return, not via killing _running


# ── Facade pins ──────────────────────────────────────────────────────────────


def test_facade_import_path_stable():
    from tools.line_monitor import (
        DB_PATH,
        MONITORED_SPORTS,
        SNAPSHOT_INTERVAL,
        LineMonitor,
    )
    assert callable(LineMonitor)
    assert isinstance(MONITORED_SPORTS, list) and MONITORED_SPORTS
    assert SNAPSHOT_INTERVAL > 0
    assert isinstance(DB_PATH, str) and DB_PATH


def test_facade_backcompat_wrappers_delegate():
    from tools.line_monitor import (
        _merge_delta_into_snapshot,
        _stamp_snapshot_fetched_at,
        _ws_sport_to_monitored,
        _ws_update_to_snapshot,
    )

    assert _ws_sport_to_monitored("basketball", "nba") == ws_map_expected()
    assert _ws_update_to_snapshot({}) is None or isinstance(
        _ws_update_to_snapshot(make_ws_payload()), tuple
    )
    base = {"games": [{"id": "g", "bookmakers": []}]}
    merged = _merge_delta_into_snapshot(base, {"games": []}, "now")
    assert merged["games"][0]["id"] == "g"
    snap = {"games": [{"id": "g", "bookmakers": [
        {"markets": [{"outcomes": [{"price": -110}]}]}]}]}
    _stamp_snapshot_fetched_at(snap, "ts")
    oc = snap["games"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]
    assert oc.get("fetched_at") == "ts"


def ws_map_expected():
    from tools.lines.ingest import ws_sport_to_monitored
    return ws_sport_to_monitored("basketball", "nba")


def test_facade_reexports_internals():
    import tools.line_monitor as lm

    for name in (
        "KLDivergenceTracker", "MovementEvaluator", "filter_significant",
        "run_monitor_cycle", "insert_snapshot_record",
        "_capture_closing_lines_impl", "_record_movement_impl",
        "handle_sharp_signals", "collect_status_counts",
        "enrich_with_scraper", "merge_delta_into_snapshot",
        "default_closing_window", "store_market_microstructure",
    ):
        assert hasattr(lm, name), f"facade lost re-export: {name}"


def test_facade_initialize_creates_schema_via_tools_lines():
    run(_test_facade_initialize_creates_schema_via_tools_lines())


async def _test_facade_initialize_creates_schema_via_tools_lines(tmp=None):
    import os
    import tempfile

    from tools.line_monitor import LineMonitor

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "monitor.db")
        lm = LineMonitor(db_path=db_path)
        await lm.initialize()
        try:
            db = lm._db
            assert {"odds_snapshots", "line_movements", "ev_opportunities"} <= set(await _atable_names(db))
            # Second initialize on a fresh monitor against the same file is idempotent
            lm2 = LineMonitor(db_path=db_path)
            await lm2.initialize()
            await lm2._db.close()  # type: ignore[union-attr]
        finally:
            await db.close()


def test_facade_stop_clears_ws_state():
    run(_test_facade_stop_clears_ws_state())


async def _test_facade_stop_clears_ws_state():
    from tools.line_monitor import LineMonitor

    lm = LineMonitor(db_path=":memory:")
    lm._running = True
    lm._ws_client = FakeWSClient()

    async def noop():
        pass

    lm._task = asyncio.ensure_future(noop())
    await lm.stop()
    assert lm._running is False
    assert lm._ws_client is None  # ws_stream teardown ran
    assert lm._task.cancelled() or lm._task.done()

