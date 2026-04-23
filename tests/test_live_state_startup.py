"""Tests for the live-state collector startup integration.

Covers four things the spec demands:

1. The collector is constructed and its task is cancelled cleanly on
   shutdown when ``CALLISTO_LIVE_STATE_ENABLED`` is on.
2. ESPN 403/429 on one sport escalates backoff for THAT sport only —
   other sports continue polling unaffected.
3. ``store_state`` fires the MLB quiet-innings detector when pre-game +
   live totals snapshots are present in the DB.
4. Missing ``live_game_states`` table → startup still succeeds and the
   collector self-disables.

These are unit tests that DO NOT hit ESPN — the HTTP client is
monkey-patched. They run against a temp SQLite file with the minimum
schema the detector needs (same pattern used by
test_live_quiet_innings).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import httpx
import pytest

import tools.live_state as live_state_mod
from tools.live_state import (
    LIVE_SPORTS,
    LiveStateCollector,
    _apply_backoff,
    _clear_backoff,
    _is_backed_off,
    _sport_backoff_until,
    _sport_backoff_step,
    evaluate_detectors_for_event,
    get_collector_counters_24h,
    poll_sport,
    store_state,
)


# ──────────────────────────────────────────────────────────────────────
# Shared schema helper
# ──────────────────────────────────────────────────────────────────────


def _minimal_schema(db_path: str, *, include_live_states: bool = True) -> None:
    """Create just enough tables for the live-state collector + detector."""
    conn = sqlite3.connect(db_path)
    try:
        if include_live_states:
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
        conn.execute(
            """CREATE TABLE live_edge_emissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                sport TEXT NOT NULL,
                market TEXT NOT NULL,
                thesis_tag TEXT NOT NULL,
                emitted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ev_opp_id INTEGER,
                notes TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE ev_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                sport TEXT,
                game_id TEXT,
                team TEXT,
                market TEXT,
                bookmaker TEXT,
                implied_probability REAL,
                estimated_true_prob REAL,
                edge REAL,
                expected_value REAL,
                source TEXT,
                is_live INTEGER DEFAULT 0,
                thesis_tag TEXT,
                expires_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT,
                snapshot_date TEXT,
                event_id TEXT,
                market_type TEXT,
                response_json TEXT,
                credits_cost REAL,
                fetched_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE clv_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT,
                clv_prob_bp REAL
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _reset_state_module() -> None:
    """Clear the module-level caches between tests so state doesn't leak."""
    live_state_mod._sport_backoff_until.clear()
    live_state_mod._sport_backoff_step.clear()
    live_state_mod._schema_ok = None
    live_state_mod._collector = None
    live_state_mod._states_collected_counter = 0
    live_state_mod._edges_emitted_counter = 0
    live_state_mod._espn_semaphore = None


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "callisto_live_test.db"
    _minimal_schema(str(path))
    _reset_state_module()
    yield str(path)


@pytest.fixture
def db_no_live_states(tmp_path):
    path = tmp_path / "callisto_nostates.db"
    _minimal_schema(str(path), include_live_states=False)
    _reset_state_module()
    yield str(path)


# ──────────────────────────────────────────────────────────────────────
# 1. Lifespan task lifecycle — collector starts + cancels cleanly
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collector_starts_and_stops_cleanly(db, monkeypatch):
    """Collector task should be created on start() and cancelled on stop()."""
    # Patch the ESPN list so _count_active_per_sport returns zero events
    # — we only care that the background task lives and dies.
    async def fake_list(sport_key):
        return []

    monkeypatch.setattr(live_state_mod, "_list_active_events", fake_list)

    c = LiveStateCollector(sports=("baseball_mlb",), db_path=db)
    await c.start()
    assert c._running is True
    assert c._task is not None
    assert not c._task.done()

    await c.stop()
    assert c._running is False
    assert c._task.done() or c._task.cancelled()


@pytest.mark.asyncio
async def test_startup_skipped_when_live_game_states_missing(db_no_live_states):
    """Fresh DB with no migration → collector self-disables, no crash."""
    c = LiveStateCollector(sports=("baseball_mlb",), db_path=db_no_live_states)
    await c.start()
    assert c._running is False
    assert c._task is None
    # status() should still return something sensible, not crash
    s = c.status()
    assert s["running"] is False


# ──────────────────────────────────────────────────────────────────────
# 2. Per-sport backoff on 403/429 isolation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_one_sport_leaves_others_alone(db, monkeypatch):
    """One sport raising 403 must apply backoff for IT only."""
    async def fake_list(sport_key):
        if sport_key == "baseball_mlb":
            raise live_state_mod._RateLimited("HTTP 429")
        return []

    monkeypatch.setattr(live_state_mod, "_list_active_events", fake_list)

    result_mlb = await poll_sport("baseball_mlb", db_path=db)
    result_nba = await poll_sport("basketball_nba", db_path=db)

    assert result_mlb.get("rate_limited")
    # MLB should now be in backoff; NBA should not be.
    assert _is_backed_off("baseball_mlb") is True
    assert _is_backed_off("basketball_nba") is False

    # Sanity: next call for MLB returns the backoff sentinel, no fetch.
    result2 = await poll_sport("baseball_mlb", db_path=db)
    assert result2.get("backoff") is True


def test_backoff_ladder_escalates():
    """Consecutive _apply_backoff calls walk 30→60→120→300 and cap."""
    _reset_state_module()
    sport = "testsport"
    steps = []
    for _ in range(6):
        steps.append(_apply_backoff(sport))
    # Should be non-decreasing and cap at 300.
    assert steps[0] == 30.0
    assert steps[-1] == 300.0
    assert all(a <= b for a, b in zip(steps, steps[1:]))
    _clear_backoff(sport)


# ──────────────────────────────────────────────────────────────────────
# 3. Detector fires on store_state for an MLB quiet-innings scenario
# ──────────────────────────────────────────────────────────────────────


def _seed_totals_snapshots(
    db_path: str,
    event_id: str,
    *,
    pregame_total: float,
    live_total: float,
    live_over_price: int,
) -> None:
    """Write one pregame + one live totals snapshot so _lookup_mlb_totals
    can find both ends of the bracket."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=2)

    def snap(total, price):
        return json.dumps({
            "bookmakers": [{
                "key": "draftkings",
                "markets": [{
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "point": total, "price": price},
                        {"name": "Under", "point": total, "price": -price},
                    ],
                }],
            }]
        })

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO odds_snapshots "
            "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("baseball_mlb", earlier.date().isoformat(), event_id, "totals",
             snap(pregame_total, -110), 0.1, earlier.isoformat()),
        )
        conn.execute(
            "INSERT INTO odds_snapshots "
            "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("baseball_mlb", now.date().isoformat(), event_id, "totals",
             snap(live_total, live_over_price), 0.1, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _mock_espn_mlb_state(inning: int, home: int, away: int) -> dict:
    """Shape matches what mlb_extract_state reads from ESPN's summary."""
    return {
        "header": {
            "competitions": [{
                "status": {"period": inning, "type": {"state": "in"}},
                "competitors": [
                    {"homeAway": "home", "score": home},
                    {"homeAway": "away", "score": away},
                ],
            }],
        },
    }


@pytest.mark.asyncio
async def test_detector_fires_on_mlb_quiet_innings_store(db, monkeypatch):
    """Full path: seed odds snapshots, call store_state with a quiet-
    innings live state, verify an ev_opportunities row lands with
    thesis_tag='mlb_quiet_innings' and is_live=1."""
    event_id = "mlb_test_evt_1"
    # Inning 3, runs 0-0. Pregame 10.0 dropped to 6.0 (−4). Expected
    # residual = 10 * 6/9 = 6.67; live_implied_residual = 6.0. Gap =
    # 0.67 → clears 0.5 threshold → quiet-innings OVER fires.
    _seed_totals_snapshots(
        db, event_id,
        pregame_total=10.0,
        live_total=6.0,
        live_over_price=-105,
    )

    state = _mock_espn_mlb_state(inning=3, home=0, away=0)
    await store_state(event_id, "baseball_mlb", state, db_path=db)

    # Check ev_opportunities for the new row.
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT thesis_tag, is_live, source, market FROM ev_opportunities "
            "WHERE game_id = ? ORDER BY id DESC LIMIT 1",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "expected an ev_opportunities row after quiet-innings trigger"
    thesis, is_live, source, market = row
    assert thesis == "mlb_quiet_innings"
    assert is_live == 1
    assert source == "live_ingame"
    assert market == "totals"


@pytest.mark.asyncio
async def test_detector_does_not_fire_without_signal(db):
    """Control: pregame=8.5 live=8.5 (no drop), no edge should emerge."""
    event_id = "mlb_control_evt"
    _seed_totals_snapshots(
        db, event_id,
        pregame_total=8.5,
        live_total=8.5,
        live_over_price=-110,
    )
    state = _mock_espn_mlb_state(inning=3, home=0, away=0)
    await store_state(event_id, "baseball_mlb", state, db_path=db)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ev_opportunities WHERE game_id = ?",
            (event_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# ──────────────────────────────────────────────────────────────────────
# 4. Observability counters
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_24h_counters_from_db(db):
    """After storing a state, get_collector_counters_24h returns ≥1."""
    state = _mock_espn_mlb_state(inning=1, home=0, away=0)
    # fire_detectors=False avoids needing odds snapshots.
    await store_state("counter_test", "baseball_mlb", state,
                      db_path=db, fire_detectors=False)
    counters = await get_collector_counters_24h(db_path=db)
    assert counters["states_collected_24h"] >= 1
    assert counters["edges_emitted_24h"] == 0


@pytest.mark.asyncio
async def test_evaluate_detectors_for_event_ws_path(db):
    """Directly invoking evaluate_detectors_for_event after a
    quiet-innings state has been stored should (re-)fire the detector."""
    event_id = "mlb_ws_path"
    # Inning 4, runs 0-0. Pregame 11.0 dropped to 6.0. Expected residual
    # = 11 * 5/9 = 6.11; live_implied_residual = 6.0 → gap ~0.11. Too
    # small. Use bigger drop: pregame 12, live 5 → expected_residual =
    # 6.67, live_implied_residual = 5.0 → gap 1.67 → fires.
    _seed_totals_snapshots(
        db, event_id,
        pregame_total=12.0,
        live_total=5.0,
        live_over_price=+100,
    )
    state = _mock_espn_mlb_state(inning=4, home=0, away=0)
    # Store with detectors OFF so the WS-path call is the one that fires.
    await store_state(event_id, "baseball_mlb", state,
                      db_path=db, fire_detectors=False)

    emitted = await evaluate_detectors_for_event(event_id, db_path=db)
    assert emitted >= 1

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT thesis_tag, is_live FROM ev_opportunities "
            "WHERE game_id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "mlb_quiet_innings"
    assert row[1] == 1
