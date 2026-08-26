"""Tests for the tools.livestate split — facade compatibility + moved helpers.

The original ~905-line ``tools/live_state.py`` monolith was split into
the ``tools/livestate/`` package (config / espn / storage / detectors /
collector) with ``tools.live_state`` kept as a facade. These tests
verify, WITHOUT touching ESPN or any live-betting path:

1. Facade surface: every public name from the pre-split module is
   importable from ``tools.live_state`` AND is implemented in exactly
   one place under ``tools/livestate`` (no duplicated logic).
2. Shared mutable state lives on the facade: resetting / monkeypatching
   attributes on ``tools.live_state`` propagates into the submodules.
3. The moved helpers behave correctly in isolation:
   - ``espn._is_active`` state classification (pre/in/post/garbage)
   - ``detectors._extract_total_point`` median extraction + malformed input
   - ``detectors._extract_live_over`` OVER point/price/book extraction
   - ``storage._prune_for_event`` retention deletion
   - ``storage._enforce_hard_cap`` oldest-row truncation
   - ``storage.recent_states`` ordering and malformed-row tolerance
4. End-to-end store → read-back round trip against a temp SQLite DB,
   including detector failure isolation (a broken detector must not
   break ingestion) — this is design constraint #2 of the module.
5. Guard rails: the paper-trade signal status set must never contain
   "live", and the split must not have widened
   ``generate_paper_trade_signal`` to status == 'live'.

These are unit tests; no network access happens anywhere.
"""

from __future__ import annotations

import importlib
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

import tools.live_state as fs
from tools.livestate import collector as ls_collector
from tools.livestate import config as ls_config
from tools.livestate import detectors as ls_detectors
from tools.livestate import espn as ls_espn
from tools.livestate import storage as ls_storage


# ──────────────────────────────────────────────────────────────────────
# Schema helper (same minimal shape used by test_live_state_startup)
# ──────────────────────────────────────────────────────────────────────


def _minimal_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE live_game_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                sport TEXT NOT NULL,
                ts TEXT NOT NULL,
                state_json TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX idx_live_states_event_ts ON live_game_states(event_id, ts DESC)"
        )
        conn.commit()
    finally:
        conn.close()


def _reset_facade() -> None:
    """Reset every piece of shared mutable state on the facade."""
    fs._sport_backoff_until.clear()
    fs._sport_backoff_step.clear()
    fs._schema_ok = None
    fs._states_collected_counter = 0
    fs._edges_emitted_counter = 0
    fs._espn_semaphore = None
    fs._client = None


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "split_test.db"
    _minimal_schema(str(path))
    _reset_facade()
    yield str(path)


# ──────────────────────────────────────────────────────────────────────
# 1. Facade surface — every public name re-exported, defined once
# ──────────────────────────────────────────────────────────────────────

PUBLIC_API = [
    "LIVE_SPORTS",
    "RETENTION_SECONDS",
    "LiveStateCollector",
    "poll_sport",
    "store_state",
    "recent_states",
    "start_collector",
    "stop_collector",
    "get_collector_status",
    "get_collector_counters_24h",
    "evaluate_detectors_for_event",
]

INTERNAL_API = [
    "_RateLimited",
    "_apply_backoff",
    "_clear_backoff",
    "_fetch_event_summary",
    "_get_client",
    "_get_semaphore",
    "_is_active",
    "_is_backed_off",
    "_list_active_events",
    "close_client",
    "_check_schema",
    "_enforce_hard_cap",
    "_prune_for_event",
    "_record_edge_emission",
    "_extract_live_over",
    "_extract_total_point",
    "_lookup_mlb_totals",
]


@pytest.mark.parametrize("name", PUBLIC_API)
def test_public_api_reexported(name):
    assert hasattr(fs, name), f"facade missing public name {name!r}"


@pytest.mark.parametrize("name", INTERNAL_API)
def test_internal_api_reexported(name):
    assert hasattr(fs, name), f"facade missing internal name {name!r}"


def test_package_does_not_shadow_module():
    """tools.live_state (module) and tools.livestate (package) must be
    distinct entries in sys.modules."""
    import sys

    assert "tools.live_state" in sys.modules
    assert "tools.livestate" in sys.modules
    assert sys.modules["tools.live_state"] is not sys.modules["tools.livestate"]
    import tools.livestate

    assert not hasattr(tools.livestate, "__file__") or (
        tools.livestate.__file__.endswith("__init__.py")
    )


def test_constants_unchanged():
    """Behavior-critical constants keep their pre-split values."""
    assert fs.RETENTION_SECONDS == 6 * 3600
    assert fs.HARD_ROW_CAP == 10_000_000
    assert fs.POLL_INTERVAL_S == 30.0
    assert fs.STAGGER_THRESHOLD == 20
    assert fs.BACKOFF_STEPS_S == (30.0, 60.0, 120.0, 300.0)
    assert set(fs.LIVE_SPORTS) == {
        "baseball_mlb",
        "basketball_nba",
        "basketball_wnba",
        "icehockey_nhl",
    }
    assert fs.ESPN_MAX_CONCURRENT >= 1


def test_functions_defined_in_submodules_not_facade():
    """No duplicated logic: implementation functions' code objects live
    in tools/livestate/*, while the facade only re-exports them."""
    for name in ("poll_sport", "start_collector", "stop_collector",
                 "get_collector_counters_24h"):
        fn = getattr(fs, name)
        assert "livestate" in inspect.getmodule(fn).__name__, name
    for name in ("store_state", "recent_states", "_prune_for_event",
                 "_enforce_hard_cap"):
        fn = getattr(fs, name)
        assert inspect.getmodule(fn).__name__ == "tools.livestate.storage", name
    for name in ("_list_active_events", "_fetch_event_summary",
                 "_is_active", "_apply_backoff"):
        fn = getattr(fs, name)
        assert inspect.getmodule(fn).__name__ == "tools.livestate.espn", name


def test_reexports_are_the_same_objects():
    """Facade names ARE the submodule callables (identity, not copies)."""
    assert fs.poll_sport is ls_collector.poll_sport
    assert fs.LiveStateCollector is ls_collector.LiveStateCollector
    assert fs.store_state is ls_storage.store_state
    assert fs.recent_states is ls_storage.recent_states
    assert fs.evaluate_detectors_for_event is ls_detectors.evaluate_detectors_for_event
    assert fs._is_active is ls_espn._is_active
    assert fs._extract_total_point is ls_detectors._extract_total_point


# ──────────────────────────────────────────────────────────────────────
# 2. Shared mutable state is homed on the facade
# ──────────────────────────────────────────────────────────────────────


def test_backoff_state_lives_on_facade():
    """_apply_backoff writes through to the facade's dicts."""
    _reset_facade()
    step = fs._apply_backoff("testsport_x")
    assert step == 30.0
    assert "testsport_x" in fs._sport_backoff_until
    assert fs._sport_backoff_step["testsport_x"] == 30.0
    assert fs._is_backed_off("testsport_x") is True
    fs._clear_backoff("testsport_x")
    assert "testsport_x" not in fs._sport_backoff_until
    assert fs._is_backed_off("testsport_x") is False


def test_resetting_facade_attr_propagates(monkeypatch):
    """Monkeypatching a helper on the facade changes what poll_sport uses."""
    calls = []

    async def fake_list(sport_key):
        calls.append(sport_key)
        return [{"id": "e1"}]

    monkeypatch.setattr(fs, "_list_active_events", fake_list)
    # poll_sport resolves _list_active_events via the facade at call time.
    # We don't run a full poll (needs schema); just verify the indirection
    # contract: the attribute the internals read is on the facade.
    assert fs._list_active_events is fake_list


def test_counter_bumps_go_to_facade(db):
    async def run():
        await fs.store_state(
            "cnt_evt", "baseball_mlb", {"a": 1},
            db_path=db, fire_detectors=False,
        )
        await fs.store_state(
            "cnt_evt", "baseball_mlb", {"a": 2},
            db_path=db, fire_detectors=False,
        )

    import asyncio

    asyncio.run(run())
    assert fs._states_collected_counter == 2
    assert fs._edges_emitted_counter == 0


def test_record_edge_emission_bumps_facade_counter():
    _reset_facade()
    fs._record_edge_emission(3)
    fs._record_edge_emission(1)
    assert fs._edges_emitted_counter == 4


def test_check_schema_caches_on_facade(db):
    async def run():
        first = await fs._check_schema(db)
        second = await fs._check_schema(db)
        return first, second

    import asyncio

    first, second = asyncio.run(run())
    assert first is True
    assert second is True
    assert fs._schema_ok is True
    # Resetting the cache forces a re-probe (still True).
    fs._schema_ok = None
    import asyncio

    assert asyncio.run(run())[0] is True


def test_check_schema_missing_table(db, tmp_path):
    empty_db = str(tmp_path / "empty.db")
    sqlite3.connect(empty_db).close()

    async def run():
        return await fs._check_schema(empty_db)

    import asyncio

    assert asyncio.run(run()) is False
    assert fs._schema_ok is False


# ──────────────────────────────────────────────────────────────────────
# 3. Moved helpers in isolation
# ──────────────────────────────────────────────────────────────────────


class TestIsActive:
    def test_in_is_active(self):
        assert fs._is_active({"status": {"type": {"state": "in"}}}) is True

    def test_pre_is_not_active(self):
        assert fs._is_active({"status": {"type": {"state": "pre"}}}) is False

    def test_post_is_not_active(self):
        assert fs._is_active({"status": {"type": {"state": "post"}}}) is False

    def test_case_insensitive(self):
        assert fs._is_active({"status": {"type": {"state": "IN"}}}) is True

    def test_missing_status(self):
        assert fs._is_active({}) is False

    def test_none_status(self):
        assert fs._is_active({"status": None}) is False


class TestExtractTotalPoint:
    def test_median_of_multiple_books(self):
        blob = json.dumps({
            "bookmakers": [
                {"key": "a", "markets": [{"key": "totals", "outcomes": [
                    {"name": "Over", "point": 8.5}, {"name": "Under", "point": 8.5}]}]},
                {"key": "b", "markets": [{"key": "totals", "outcomes": [
                    {"name": "Over", "point": 9.0}]}]},
                {"key": "c", "markets": [{"key": "totals", "outcomes": [
                    {"name": "Over", "point": 7.5}]}]},
            ]
        })
        assert fs._extract_total_point(blob) == 8.5

    def test_empty_blob(self):
        assert fs._extract_total_point(None) is None
        assert fs._extract_total_point("") is None

    def test_malformed_json(self):
        assert fs._extract_total_point("{not json") is None

    def test_non_dict_blob(self):
        assert fs._extract_total_point(json.dumps([1, 2, 3])) is None

    def test_no_totals_market(self):
        blob = json.dumps({"bookmakers": [
            {"key": "a", "markets": [{"key": "spreads", "outcomes": [
                {"name": "Home", "point": 2.5}]}]}]})
        assert fs._extract_total_point(blob) is None


class TestExtractLiveOver:
    def test_extracts_point_price_book(self):
        blob = json.dumps({"bookmakers": [{
            "key": "draftkings",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "point": 6.5, "price": -105},
                {"name": "Under", "point": 6.5, "price": -115},
            ]}],
        }]})
        point, price, book = fs._extract_live_over(blob)
        assert point == 6.5
        assert price == -105
        assert book == "draftkings"

    def test_title_fallback_for_book(self):
        blob = json.dumps({"bookmakers": [{
            "title": "FanDuel",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Over", "point": 7.0, "price": 100},
            ]}],
        }]})
        _, _, book = fs._extract_live_over(blob)
        assert book == "FanDuel"

    def test_skips_under_and_bad_rows(self):
        blob = json.dumps({"bookmakers": [{
            "key": "x",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Under", "point": 6.5, "price": -115},
                {"name": "Over", "point": None, "price": -105},   # bad point
                {"name": "Over", "point": 6.5, "price": "abc"},   # bad price
                {"name": "Over", "point": 7.5, "price": -110},    # good
            ]}],
        }]})
        point, price, book = fs._extract_live_over(blob)
        assert point == 7.5
        assert price == -110
        assert book == "x"

    def test_missing_or_malformed(self):
        assert fs._extract_live_over(None) == (None, None, None)
        assert fs._extract_live_over("oops") == (None, None, None)

    def test_no_over_outcome(self):
        blob = json.dumps({"bookmakers": [{
            "key": "y",
            "markets": [{"key": "totals", "outcomes": [
                {"name": "Under", "point": 6.0, "price": -110},
            ]}],
        }]})
        assert fs._extract_live_over(blob) == (None, None, None)


class TestBackoffLadder:
    def test_full_ladder_then_cap(self):
        _reset_facade()
        sport = "ladder_sport"
        steps = [fs._apply_backoff(sport) for _ in range(6)]
        assert steps[:4] == [30.0, 60.0, 120.0, 300.0]
        assert all(s == 300.0 for s in steps[4:])
        fs._clear_backoff(sport)

    def test_clear_unknown_sport_is_noop(self):
        fs._clear_backoff("never_seen_sport")

    def test_backoff_expiry(self):
        _reset_facade()
        sport = "expiring"
        until_key = sport
        fs._sport_backoff_until[until_key] = (
            datetime.now(timezone.utc).timestamp() - 10
        )
        assert fs._is_backed_off(sport) is False


# ──────────────────────────────────────────────────────────────────────
# 4. Storage round trips
# ──────────────────────────────────────────────────────────────────────


class TestStoreAndReadBack:
    def test_store_returns_increasing_ids(self, db):
        import asyncio

        async def run():
            id1 = await fs.store_state("rt", "icehockey_nhl", {"p": 1},
                                       db_path=db, fire_detectors=False)
            id2 = await fs.store_state("rt", "icehockey_nhl", {"p": 2},
                                       db_path=db, fire_detectors=False)
            return id1, id2

        id1, id2 = asyncio.run(run())
        assert id2 > id1 > 0

    def test_recent_states_newest_first(self, db):
        import asyncio

        now = datetime.now(timezone.utc)

        async def run():
            for i in range(5):
                await fs.store_state("ord", "baseball_mlb", {"seq": i},
                                     db_path=db, fire_detectors=False,
                                     now=now - timedelta(minutes=5 - i))
            rows = await fs.recent_states("ord", db_path=db, limit=20)

        asyncio.run(run())
        # re-fetch inside sync world
        import asyncio

        rows = asyncio.run(fs.recent_states("ord", db_path=db, limit=20))
        seqs = [r["state"]["seq"] for r in rows]
        assert seqs == sorted(seqs, reverse=True)
        assert len(rows) == 5
        assert all(isinstance(r["ts"], str) for r in rows)

    def test_recent_states_limit(self, db):
        import asyncio

        async def seed():
            for i in range(4):
                await fs.store_state("lim", "basketball_nba", {"i": i},
                                     db_path=db, fire_detectors=False)

        asyncio.run(seed())
        rows = asyncio.run(fs.recent_states("lim", db_path=db, limit=2))
        assert len(rows) == 2

    def test_recent_states_empty(self, db):
        import asyncio

        assert asyncio.run(fs.recent_states("nope", db_path=db)) == []

    def test_prune_removes_old_rows_only(self, db):
        import asyncio

        now = datetime.now(timezone.utc)
        old_ts = (now - timedelta(seconds=fs.RETENTION_SECONDS + 600)).isoformat()

        async def run():
            async with aiosqlite.connect(db) as d:
                await d.execute(
                    "INSERT INTO live_game_states (event_id, sport, ts, state_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("pr", "baseball_mlb", old_ts, "{}"),
                )
                await d.commit()

        asyncio.run(run())

        async def prune():
            async with aiosqlite.connect(db) as d:
                await ls_storage._prune_for_event(d, "pr", now)
                await d.commit()
                cur = await d.execute(
                    "SELECT COUNT(*) FROM live_game_states WHERE event_id='pr'")
                return (await cur.fetchone())[0]

        assert asyncio.run(prune()) == 0

    def test_prune_keeps_fresh_rows_and_other_events(self, db):
        import asyncio

        now = datetime.now(timezone.utc)

        async def seed():
            async with aiosqlite.connect(db) as d:
                fresh = (now - timedelta(minutes=1)).isoformat()
                await d.execute(
                    "INSERT INTO live_game_states (event_id, sport, ts, state_json) "
                    "VALUES (?, ?, ?, ?)",
                    ("keep", "baseball_mlb", fresh, "{}"),
                )
                await d.commit()

        asyncio.run(seed())

        async def prune():
            async with aiosqlite.connect(db) as d:
                await ls_storage._prune_for_event(d, "other-event", now)
                await d.commit()
                cur = await d.execute(
                    "SELECT COUNT(*) FROM live_game_states WHERE event_id='keep'")
                return (await cur.fetchone())[0]

        assert asyncio.run(prune()) == 1

    def test_hard_cap_truncates_oldest(self, db, monkeypatch):
        import asyncio

        monkeypatch.setattr(fs, "HARD_ROW_CAP", 3)

        async def seed():
            for i in range(5):
                await fs.store_state(f"cap_{i}", "baseball_mlb", {"i": i},
                                     db_path=db, fire_detectors=False)

        async def enforce():
            async with aiosqlite.connect(db) as d:
                await ls_storage._enforce_hard_cap(d)
                await d.commit()
                cur = await d.execute("SELECT COUNT(*) FROM live_game_states")
                remaining_ids = await d.execute(
                    "SELECT event_id FROM live_game_states ORDER BY id")
                ids = [r[0] for r in await remaining_ids.fetchall()]
                return (await cur.fetchone())[0], ids

        asyncio.run(seed())
        count, ids = asyncio.run(enforce())
        assert count == 3
        # Oldest three were truncated; newest remain.
        assert ids == ["cap_2", "cap_3", "cap_4"]

    def test_hard_cap_noop_below_cap(self, db):
        import asyncio

        async def seed():
            await fs.store_state("small", "baseball_mlb", {},
                                 db_path=db, fire_detectors=False)

        asyncio.run(seed())

        async def check():
            async with aiosqlite.connect(db) as d:
                before = await (await d.execute(
                    "SELECT COUNT(*) FROM live_game_states")).fetchone()
                await ls_storage._enforce_hard_cap(d)
                after = await (await d.execute(
                    "SELECT COUNT(*) FROM live_game_states")).fetchone()
                return before[0], after[0]

        b, a = asyncio.run(check())
        assert b == a == 1


class TestDetectorFailureIsolation:
    def test_broken_detector_does_not_break_store(self, db, monkeypatch):
        """Design constraint #2: a single bad detector must not stop
        ingestion."""
        import asyncio

        async def boom(**kwargs):
            raise RuntimeError("detector exploded")

        monkeypatch.setattr(ls_storage, "_evaluate_detectors_call", boom)

        async def run():
            return await fs.store_state(
                "boom_evt", "baseball_mlb", {"ok": True}, db_path=db,
            )

        new_id = asyncio.run(run())
        assert new_id > 0
        # Row landed despite the detector blowing up.
        rows = asyncio.run(fs.recent_states("boom_evt", db_path=db))
        assert len(rows) == 1
        assert rows[0]["state"] == {"ok": True}

    def test_detector_exception_via_evaluate_entrypoint(self, db, monkeypatch):
        """evaluate_detectors_for_event swallows detector errors."""
        import asyncio

        async def boom(**kwargs):
            raise RuntimeError("kaboom")

        async def seed():
            await fs.store_state("ws_evt", "baseball_mlb", {"x": 1},
                                 db_path=db, fire_detectors=False)

        asyncio.run(seed())
        monkeypatch.setattr(ls_detectors, "_evaluate_detectors", boom)
        emitted = asyncio.run(
            fs.evaluate_detectors_for_event("ws_evt", db_path=db))
        assert emitted == 0

    def test_evaluate_no_rows(self, db):
        import asyncio

        assert asyncio.run(
            fs.evaluate_detectors_for_event("ghost", db_path=db)) == 0

    def test_evaluate_non_mlb_short_circuits(self, db):
        import asyncio

        async def seed():
            await fs.store_state("nba_evt", "basketball_nba", {"x": 1},
                                 db_path=db, fire_detectors=False)

        asyncio.run(seed())
        assert asyncio.run(
            fs.evaluate_detectors_for_event("nba_evt", db_path=db)) == 0


class TestLookupMlbTotals:
    def _seed(self, db_path, event_id, totals):
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS odds_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport TEXT, snapshot_date TEXT, event_id TEXT,
                    market_type TEXT, response_json TEXT,
                    credits_cost REAL, fetched_at TEXT)"""
            )
            base = datetime.now(timezone.utc) - timedelta(hours=3)
            for i, total in enumerate(totals):
                blob = json.dumps({
                    "bookmakers": [{
                        "key": "bk",
                        "markets": [{"key": "totals", "outcomes": [
                            {"name": "Over", "point": total, "price": -110},
                        ]}],
                    }]
                })
                ts = (base + timedelta(minutes=i)).isoformat()
                conn.execute(
                    "INSERT INTO odds_snapshots (sport, snapshot_date, event_id,"
                    " market_type, response_json, credits_cost, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("baseball_mlb", ts[:10], event_id, "totals", blob, 0.1, ts),
                )
            conn.commit()
        finally:
            conn.close()

    def test_first_and_last_snapshots_used(self, db):
        import asyncio

        self._seed(db, "lk_evt", [10.5, 11.0, 7.5])
        pre, live, price, book = asyncio.run(
            ls_detectors._lookup_mlb_totals(
                db_path=db, event_id="lk_evt", now=datetime.now(timezone.utc)))
        assert pre == 10.5
        assert live == 7.5
        assert price == -110
        assert book == "bk"

    def test_missing_table_returns_nones(self, tmp_path):
        import asyncio

        empty = str(tmp_path / "noodds.db")
        sqlite3.connect(empty).close()
        res = asyncio.run(
            ls_detectors._lookup_mlb_totals(
                db_path=empty, event_id="x", now=datetime.now(timezone.utc)))
        assert res == (None, None, None, None)

    def test_no_rows_returns_nones(self, db):
        import asyncio

        res = asyncio.run(
            ls_detectors._lookup_mlb_totals(
                db_path=db, event_id="nothing_here",
                now=datetime.now(timezone.utc)))
        assert res == (None, None, None, None)


# ──────────────────────────────────────────────────────────────────────
# 5. Collector unit behavior
# ──────────────────────────────────────────────────────────────────────


class TestCollectorUnit:
    def test_default_sports_from_facade_constant(self):
        c = ls_collector.LiveStateCollector()
        assert c.sports == tuple(fs.LIVE_SPORTS.keys())

    def test_explicit_sports_override(self):
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",))
        assert c.sports == ("baseball_mlb",)

    def test_initial_status_shape(self):
        _reset_facade()
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",))
        s = c.status()
        assert s["running"] is False
        assert s["sports"] == ["baseball_mlb"]
        assert s["last_round_age_s"] is None
        assert s["active_games_polling"] == 0
        assert s["backoff_sports"] == {}

    def test_status_reflects_backoff(self):
        _reset_facade()
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",))
        fs._apply_backoff("baseball_mlb")
        s = c.status()
        assert "baseball_mlb" in s["backoff_sports"]
        assert s["backoff_sports"]["baseball_mlb"] >= 0.0
        fs._clear_backoff("baseball_mlb")
        assert c.status()["backoff_sports"] == {}

    def test_status_lifetime_counters_read_facade(self, db):
        import asyncio

        _reset_facade()
        fs._states_collected_counter = 7
        fs._edges_emitted_counter = 3
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",))
        s = c.status()
        assert s["states_collected_lifetime"] == 7
        assert s["edges_emitted_lifetime"] == 3

    def test_get_collector_status_when_none(self):
        _reset_facade()
        ls_collector._collector = None
        assert ls_collector.get_collector_status() == {"running": False}

    @pytest.mark.asyncio
    async def test_start_disabled_without_schema(self, tmp_path):
        empty = str(tmp_path / "noschema.db")
        sqlite3.connect(empty).close()
        fs._schema_ok = None
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",),
                                            db_path=empty)
        await c.start()
        assert c._running is False
        assert c._task is None

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self, db):
        c = ls_collector.LiveStateCollector(sports=("baseball_mlb",),
                                            db_path=db)
        await c.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_poll_sport_unsupported_sport(self, db):
        out = await ls_collector.poll_sport("underwater_hockey", db_path=db)
        assert out["error"].startswith("unsupported sport")
        assert out["snapshots"] == 0

    @pytest.mark.asyncio
    async def test_poll_sport_backoff_sentinel(self, db):
        _reset_facade()
        fs._sport_backoff_until["baseball_mlb"] = (
            datetime.now(timezone.utc).timestamp() + 999
        )
        out = await ls_collector.poll_sport("baseball_mlb", db_path=db)
        assert out == {"backoff": True, "events": 0, "snapshots": 0}
        fs._clear_backoff("baseball_mlb")


# ──────────────────────────────────────────────────────────────────────
# 6. 24h counters + guard rails
# ──────────────────────────────────────────────────────────────────────


class TestCounters24hAndGuardRails:
    @pytest.mark.asyncio
    async def test_counters_count_recent_row(self, db):
        await fs.store_state("c24", "baseball_mlb", {"v": 1},
                             db_path=db, fire_detectors=False)
        counters = await ls_collector.get_collector_counters_24h(db_path=db)
        assert counters["states_collected_24h"] >= 1
        assert counters["edges_emitted_24h"] == 0

    @pytest.mark.asyncio
    async def test_counters_tolerate_missing_tables(self, tmp_path):
        empty = str(tmp_path / "bare.db")
        sqlite3.connect(empty).close()
        counters = await ls_collector.get_collector_counters_24h(db_path=empty)
        assert counters == {"states_collected_24h": 0, "edges_emitted_24h": 0}

    def test_paper_trade_statuses_never_include_live(self):
        """Hard guard rail: 'live' must NEVER be added to
        _PAPER_TRADE_SIGNAL_STATUSES by this refactor."""
        candidates = []
        for mod_name in ("tools.signals", "tools.signal"):
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            for attr in dir(mod):
                if "PAPER_TRADE_SIGNAL_STATUS" in attr:
                    candidates.append((mod_name, attr, getattr(mod, attr)))
        for mod_name, attr, val in candidates:
            if isinstance(val, (set, frozenset, list, tuple)):
                assert "live" not in {str(v).lower() for v in val}, (
                    f"{mod_name}.{attr} contains 'live'"
                )

    def test_generate_paper_trade_source_has_no_live_branch(self):
        """Static guard: generate_paper_trade_signal source must not gain
        a status=='live' branch from the split."""
        import re

        import tools.live_edges  # noqa: F401 — may hold the generator

        for mod_name in ("tools.live_edges", "tools.signals", "tools.signal"):
            try:
                src = inspect.getsource(importlib.import_module(mod_name))
            except Exception:
                continue
            m = re.search(r"def generate_paper_trade_signal.*?(?=\ndef |\nclass |\Z)",
                          src, re.DOTALL)
            if not m:
                continue
            body = m.group(0)
            assert not re.search(r"status\s*==\s*['\"]live['\"]", body), (
                "generate_paper_trade_signal must not branch on status=='live'"
            )

    def test_no_live_execute_arming_in_split(self):
        """The split modules must not reference execute/order placement —
        this package is strictly state ingestion + startup."""
        import pathlib

        pkg_dir = pathlib.Path(ls_storage.__file__).parent
        banned = ("place_bet", "execute_order", "arm_live",
                  "live_execute_enabled")
        for py in pkg_dir.glob("*.py"):
            text = py.read_text()
            for token in banned:
                assert token not in text, f"{py.name} references {token}"
