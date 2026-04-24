"""Tests for multi-sport live-state detector dispatch (MLB + NBA + NHL).

Before this change, ``tools.live_state._evaluate_detectors`` gated on
``sport == "baseball_mlb"`` and every other LIVE_SPORTS sport silently
emitted zero edges from the poll path. NBA/NHL detectors existed but
only fired on the WebSocket path, which meant polled state never
produced in-game edges.

These tests exercise the new per-sport dispatch registry:

1. NBA state extractor parses period / clock / scores from an ESPN-shaped
   summary payload.
2. NHL state extractor behaves the same way.
3. Polled NBA summary + seeded live spread snapshot -> detector fires ->
   row appears in ``ev_opportunities`` with ``thesis_tag='nba_late_overreaction'``.
4. Polled NHL summary + seeded puck-line snapshot -> detector fires ->
   ``thesis_tag='nhl_late_overreaction'``.
5. MLB regression test — the legacy path still emits quiet-innings edges.
6. Unknown sport returns 0 emissions cleanly (no exceptions).
7. Per-sport telemetry (``per_sport[...]['edges_emitted']``) updates after
   a fire and the dispatch registry is exposed via status().
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
import pytest

import tools.live_state as live_state_mod
from tools.live_edges import nba_extract_state, nhl_extract_state
from tools.live_state import (
    LIVE_SPORTS,
    LiveStateCollector,
    _SPORT_DETECTOR_REGISTRY,
    _evaluate_detectors,
    evaluate_detectors_for_event,
    store_state,
)


# ──────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────


def _schema(db_path: str) -> None:
    """Create the minimal set of tables the live detectors depend on.

    We intentionally avoid running the full schema.py — these tests must
    stay fast and isolated from migration ordering.
    """
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


def _reset_module() -> None:
    live_state_mod._sport_backoff_until.clear()
    live_state_mod._sport_backoff_step.clear()
    live_state_mod._schema_ok = None
    live_state_mod._collector = None
    live_state_mod._states_collected_counter = 0
    live_state_mod._edges_emitted_counter = 0
    live_state_mod._espn_semaphore = None
    live_state_mod._per_sport_telemetry.clear()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "callisto_multisport.db"
    _schema(str(path))
    _reset_module()
    yield str(path)


# ──────────────────────────────────────────────────────────────────────
# Fixture payloads — ESPN-shaped JSON
# ──────────────────────────────────────────────────────────────────────


def _espn_nba_summary(
    *,
    period: int = 3,
    clock: str = "0.0",
    home_score: int = 90,
    away_score: int = 72,
) -> dict:
    """Return an ESPN-shaped NBA summary payload.

    The real ESPN payload has far more fields (drives, plays, boxscore)
    but the detector only needs period / clock / scores, so we keep the
    fixture minimal. The ``clock`` field is stringy because that's how
    ESPN serializes it on the summary endpoint.
    """
    return {
        "header": {
            "competitions": [
                {
                    "status": {"period": period, "clock": clock,
                               "displayClock": clock},
                    "competitors": [
                        {"homeAway": "home", "score": str(home_score)},
                        {"homeAway": "away", "score": str(away_score)},
                    ],
                }
            ]
        }
    }


def _espn_nhl_summary(
    *,
    period: int = 2,
    clock: str = "00:30",
    home_score: int = 4,
    away_score: int = 1,
) -> dict:
    """ESPN NHL summary fixture — same shape as NBA, different sport
    slug and different clock serialization (mm:ss)."""
    return {
        "header": {
            "competitions": [
                {
                    "status": {"period": period, "clock": clock,
                               "displayClock": clock},
                    "competitors": [
                        {"homeAway": "home", "score": str(home_score)},
                        {"homeAway": "away", "score": str(away_score)},
                    ],
                }
            ]
        }
    }


def _espn_mlb_summary(
    *,
    inning: int = 4,
    home_runs: int = 0,
    away_runs: int = 0,
) -> dict:
    """ESPN MLB summary fixture — inning is encoded as period."""
    return {
        "header": {
            "competitions": [
                {
                    "status": {"period": inning},
                    "competitors": [
                        {"homeAway": "home", "score": str(home_runs)},
                        {"homeAway": "away", "score": str(away_runs)},
                    ],
                }
            ]
        }
    }


def _odds_snapshot_spreads(
    *,
    home_team: str,
    home_point: float,
    home_price: int,
    away_team: str = "AWAY_TEAM",
    away_price: int = -110,
) -> dict:
    """Return an odds-api-shaped snapshot blob with a single 'spreads'
    market. Used for both NBA spreads and NHL puck lines — the schema
    is identical, only the numeric scale differs."""
    return {
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": home_team, "point": home_point,
                             "price": home_price},
                            {"name": away_team, "point": -home_point,
                             "price": away_price},
                        ],
                    }
                ],
            }
        ],
    }


def _odds_snapshot_totals(point: float, over_price: int = -110) -> dict:
    """Shape an odds-api snapshot for MLB totals regression test."""
    return {
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "point": point, "price": over_price},
                            {"name": "Under", "point": point, "price": -110},
                        ],
                    }
                ],
            }
        ],
    }


def _seed_odds_snapshot(
    db_path: str,
    *,
    sport: str,
    event_id: str,
    market_type: str,
    blob: dict,
    fetched_at: str,
) -> None:
    """Insert a row into odds_snapshots with the given blob."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO odds_snapshots "
            "(sport, snapshot_date, event_id, market_type, response_json, "
            " credits_cost, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sport,
                fetched_at.split("T")[0],
                event_id,
                market_type,
                json.dumps(blob),
                0,
                fetched_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# 1 & 2. State extractors
# ──────────────────────────────────────────────────────────────────────


def test_nba_extract_state_parses_period_clock_scores():
    payload = _espn_nba_summary(period=3, clock="0.0", home_score=90, away_score=72)
    s = nba_extract_state(payload)
    assert s["period"] == 3
    assert s["time_remaining_s"] == 0
    assert s["home_score"] == 90
    assert s["away_score"] == 72


def test_nba_extract_state_parses_mm_ss_clock():
    payload = _espn_nba_summary(period=3, clock="1:30", home_score=90, away_score=72)
    s = nba_extract_state(payload)
    assert s["time_remaining_s"] == 90


def test_nba_extract_state_missing_fields_default_safely():
    """Missing period / scores / clock => 0 / None without raising."""
    s = nba_extract_state({})
    assert s["period"] == 0
    assert s["time_remaining_s"] is None
    assert s["home_score"] == 0
    assert s["away_score"] == 0


def test_nhl_extract_state_parses_mm_ss_clock():
    payload = _espn_nhl_summary(period=2, clock="00:30",
                                home_score=4, away_score=1)
    s = nhl_extract_state(payload)
    assert s["period"] == 2
    assert s["time_remaining_s"] == 30
    assert s["home_score"] == 4
    assert s["away_score"] == 1


# ──────────────────────────────────────────────────────────────────────
# 3. NBA polled path — detector fires -> edge emitted
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nba_poll_fires_detector_and_emits_edge(db):
    event_id = "NBA_TEST_1"
    now = datetime(2026, 4, 22, 2, 30, tzinfo=timezone.utc)

    # Seed a live spread — home -5.0 on a 18-point actual lead is the
    # compression pattern the detector is hunting. Fixture mirrors the
    # unit-test case that's already validated in test_live_reactivity.
    _seed_odds_snapshot(
        db, sport="basketball_nba", event_id=event_id, market_type="spreads",
        blob=_odds_snapshot_spreads(
            home_team="LAKERS", home_point=-5.0, home_price=-130,
        ),
        fetched_at=now.isoformat(),
    )

    summary = _espn_nba_summary(period=3, clock="0.0",
                                home_score=90, away_score=72)

    # store_state writes the row AND invokes _evaluate_detectors.
    inserted = await store_state(event_id, "basketball_nba", summary,
                                 db_path=db, now=now)
    assert inserted > 0

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT thesis_tag, is_live, market, sport FROM ev_opportunities"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "nba_late_overreaction"
    assert rows[0][1] == 1
    assert rows[0][2] == "spread"
    assert rows[0][3] == "basketball_nba"

    # Per-sport telemetry updated.
    tel = live_state_mod._per_sport_telemetry.get("basketball_nba")
    assert tel is not None
    assert int(tel.get("edges_emitted") or 0) >= 1
    assert int(tel.get("detector_fires") or 0) >= 1


@pytest.mark.asyncio
async def test_nba_poll_without_odds_does_not_fire(db):
    """If there's no live spread snapshot, the detector must skip."""
    event_id = "NBA_NO_ODDS"
    now = datetime(2026, 4, 22, 2, 30, tzinfo=timezone.utc)
    summary = _espn_nba_summary(period=3, clock="0.0",
                                home_score=90, away_score=72)
    await store_state(event_id, "basketball_nba", summary,
                      db_path=db, now=now)

    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM ev_opportunities"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


# ──────────────────────────────────────────────────────────────────────
# 4. NHL polled path — detector fires -> edge emitted
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nhl_poll_fires_detector_and_emits_edge(db):
    event_id = "NHL_TEST_1"
    now = datetime(2026, 4, 22, 2, 30, tzinfo=timezone.utc)

    # Home up 4-1 end of P2. Live puck line -0.5 (home favored) implies
    # ≈ 0.5 final-goal diff; expected end diff = 0.6*3 = 1.8; overextend
    # ≈ 1.3 goals > NHL_SPREAD_OVEREXTEND_GOALS (0.75) => fire HOME.
    _seed_odds_snapshot(
        db, sport="icehockey_nhl", event_id=event_id, market_type="spreads",
        blob=_odds_snapshot_spreads(
            home_team="BRUINS", home_point=-0.5, home_price=-135,
        ),
        fetched_at=now.isoformat(),
    )

    summary = _espn_nhl_summary(period=2, clock="00:30",
                                home_score=4, away_score=1)
    inserted = await store_state(event_id, "icehockey_nhl", summary,
                                 db_path=db, now=now)
    assert inserted > 0

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT thesis_tag, is_live, market, sport FROM ev_opportunities"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "nhl_late_overreaction"
    assert rows[0][1] == 1
    assert rows[0][2] == "puck_line"
    assert rows[0][3] == "icehockey_nhl"


# ──────────────────────────────────────────────────────────────────────
# 5. MLB regression — existing behavior must survive the refactor
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mlb_regression_quiet_innings_still_fires(db):
    event_id = "MLB_REG_1"
    t0 = datetime(2026, 4, 22, 19, 0, tzinfo=timezone.utc)

    # Pre-game totals = 9.5 (earliest fetched_at), live totals = 5.5.
    _seed_odds_snapshot(
        db, sport="baseball_mlb", event_id=event_id, market_type="totals",
        blob=_odds_snapshot_totals(9.5, over_price=-105),
        fetched_at=t0.isoformat(),
    )
    t1 = t0 + timedelta(minutes=30)
    _seed_odds_snapshot(
        db, sport="baseball_mlb", event_id=event_id, market_type="totals",
        blob=_odds_snapshot_totals(5.5, over_price=+105),
        fetched_at=t1.isoformat(),
    )

    summary = _espn_mlb_summary(inning=3, home_runs=0, away_runs=0)
    inserted = await store_state(event_id, "baseball_mlb", summary,
                                 db_path=db, now=t1)
    assert inserted > 0

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT thesis_tag, is_live, market FROM ev_opportunities"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "mlb_quiet_innings"
    assert row[1] == 1
    assert row[2] == "totals"


# ──────────────────────────────────────────────────────────────────────
# 6. Unknown sport must not raise
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_sport_returns_zero_cleanly(db):
    now = datetime(2026, 4, 22, 2, 30, tzinfo=timezone.utc)
    # Call _evaluate_detectors directly with a sport that isn't in the
    # registry. It must return 0 and not raise.
    n = await _evaluate_detectors(
        event_id="X", sport="cricket_ipl", state={}, prev_state=None,
        db_path=db, now=now,
    )
    assert n == 0


# ──────────────────────────────────────────────────────────────────────
# 7. Registry / telemetry surface
# ──────────────────────────────────────────────────────────────────────


def test_detector_registry_covers_three_sports():
    """Before this change the gate was hard-coded to MLB only. After,
    the registry must contain MLB + NBA + NHL."""
    assert "baseball_mlb" in _SPORT_DETECTOR_REGISTRY
    assert "basketball_nba" in _SPORT_DETECTOR_REGISTRY
    assert "icehockey_nhl" in _SPORT_DETECTOR_REGISTRY
    assert len(_SPORT_DETECTOR_REGISTRY) >= 3


@pytest.mark.asyncio
async def test_collector_status_exposes_per_sport_and_detectors_wired(db):
    _reset_module()
    c = LiveStateCollector(
        sports=("baseball_mlb", "basketball_nba", "icehockey_nhl"),
        db_path=db,
    )
    # Don't start the background loop — just verify status() shape is
    # dashboard-ready even before any poll happens.
    s = c.status()
    assert set(s["detectors_wired"]) >= {
        "baseball_mlb", "basketball_nba", "icehockey_nhl",
    }
    assert set(s["per_sport"].keys()) == {
        "baseball_mlb", "basketball_nba", "icehockey_nhl",
    }
    for sport, row in s["per_sport"].items():
        assert row["detector_wired"] is True
        assert "games_observed" in row
        assert "states_collected" in row
        assert "edges_emitted" in row
        assert "last_eval_ts" in row
