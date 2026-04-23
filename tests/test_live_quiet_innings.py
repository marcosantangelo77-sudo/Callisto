"""Tests for the MLB quiet-innings live detector.

Exercises the pure-signal function (no SQLite / ESPN) across the
inning boundary, drop threshold, and residual-runs gap.

Also integration-tests the emit_edge path to ensure rate-limiting and
the ev_opportunities row shape are correct.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from tools.live_edges import (
    LIVE_EDGE_TTL_S,
    emit_edge,
    mlb_extract_state,
    mlb_quiet_innings_signal,
    register_live_hypotheses,
)


def _schema_sql_for_test() -> list[str]:
    """Return the minimal schema the live detector needs.

    We don't run the full ensure_schema to keep the test fast and
    isolated from the rest of the schema's many migrations.
    """
    return [
        """CREATE TABLE IF NOT EXISTS ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            sport TEXT,
            game_id TEXT,
            team TEXT,
            market TEXT,
            bookmaker TEXT,
            american_odds INTEGER,
            implied_probability REAL,
            estimated_true_prob REAL,
            edge REAL,
            expected_value REAL,
            kelly_fraction REAL,
            status TEXT DEFAULT 'open',
            source TEXT DEFAULT 'line_movement',
            steam_only INTEGER DEFAULT 0,
            is_live INTEGER DEFAULT 0,
            thesis_tag TEXT,
            expires_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS live_edge_emissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            sport TEXT NOT NULL,
            market TEXT NOT NULL,
            thesis_tag TEXT NOT NULL,
            emitted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ev_opp_id INTEGER,
            notes TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS clv_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            clv_prob_bp REAL
        )""",
        """CREATE TABLE IF NOT EXISTS hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            thesis TEXT NOT NULL,
            sport TEXT NOT NULL,
            market_type TEXT NOT NULL,
            model_config TEXT NOT NULL,
            edge_threshold REAL NOT NULL DEFAULT 0.01,
            status TEXT NOT NULL DEFAULT 'draft',
            min_sample_size INTEGER NOT NULL DEFAULT 50,
            significance_level REAL NOT NULL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME,
            promoted_by TEXT,
            notes TEXT
        )""",
    ]


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    for stmt in _schema_sql_for_test():
        conn.execute(stmt)
    conn.commit()
    conn.close()
    return path


# ── Pure signal tests ─────────────────────────────────────────────────


def test_does_not_fire_before_third_inning():
    """Two scoreless innings alone is noise — not enough data to claim
    over-reaction."""
    out = mlb_quiet_innings_signal(
        inning=2,
        total_runs=0,
        pregame_total=8.5,
        live_total=7.0,
        live_over_price=-110,
    )
    assert out is None


def test_does_not_fire_when_not_quiet():
    """If 4 runs are in through 3 innings, not a quiet start."""
    out = mlb_quiet_innings_signal(
        inning=3,
        total_runs=4,
        pregame_total=8.5,
        live_total=7.0,
        live_over_price=-110,
    )
    assert out is None


def test_does_not_fire_without_line_drop():
    """Market hasn't meaningfully dropped → no over-reaction to lean on."""
    out = mlb_quiet_innings_signal(
        inning=4,
        total_runs=1,
        pregame_total=8.5,
        live_total=8.2,
        live_over_price=-110,
    )
    assert out is None


def test_fires_on_strong_over_reaction():
    """Crisper case: pre=9.5, live=5.5 through 3 scoreless innings.
    expected_residual = 9.5 * 6/9 ≈ 6.33; live_implied = 5.5; gap = +0.83."""
    out = mlb_quiet_innings_signal(
        inning=3,
        total_runs=0,
        pregame_total=9.5,
        live_total=5.5,
        live_over_price=+105,
    )
    assert out is not None
    assert out.side == "OVER"
    assert out.edge > 0
    assert 0 < out.implied_probability < 1


def test_extract_state_from_espn_like_payload():
    """mlb_extract_state should read ESPN summary JSON without crashing
    when fields are missing."""
    fake = {
        "header": {
            "competitions": [{
                "status": {"period": 5},
                "competitors": [
                    {"homeAway": "home", "score": "2"},
                    {"homeAway": "away", "score": "1"},
                ],
            }]
        }
    }
    st = mlb_extract_state(fake)
    assert st["inning"] == 5
    assert st["home_runs"] == 2
    assert st["away_runs"] == 1
    assert st["total_runs"] == 3


# ── Integration: emit_edge + DB ───────────────────────────────────────


def test_emit_edge_writes_rows_and_enforces_rate_limit():
    """emit_edge inserts into both tables with is_live + thesis_tag +
    expires_at. A second emission of the same (event, market, thesis)
    within the cooldown is rejected."""
    db_path = _make_db()
    try:
        now = datetime(2026, 4, 22, 19, 0, tzinfo=timezone.utc)

        edge = mlb_quiet_innings_signal(
            inning=3, total_runs=0, pregame_total=9.5,
            live_total=5.5, live_over_price=+105,
        )
        assert edge is not None
        edge.event_id = "MLB_TEST_1"
        edge.bookmaker = "DraftKings"

        ev_id = asyncio.run(emit_edge(edge, db_path=db_path, now=now))
        assert ev_id is not None and ev_id > 0

        # Verify row shape.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT is_live, thesis_tag, expires_at, market, source, bookmaker "
            "FROM ev_opportunities WHERE id=?",
            (ev_id,),
        ).fetchone()
        conn.close()
        assert row == (1, "mlb_quiet_innings",
                       (now + timedelta(seconds=LIVE_EDGE_TTL_S)).isoformat(),
                       "totals", "live_ingame", "draftkings")

        # Second emission inside cooldown → rejected.
        ev_id2 = asyncio.run(emit_edge(
            edge, db_path=db_path,
            now=now + timedelta(seconds=30),
        ))
        assert ev_id2 is None

        # After cooldown passes → accepted.
        ev_id3 = asyncio.run(emit_edge(
            edge, db_path=db_path,
            now=now + timedelta(seconds=150),
        ))
        assert ev_id3 is not None and ev_id3 != ev_id
    finally:
        os.unlink(db_path)


def test_register_live_hypotheses_inserts_four_rows():
    db_path = _make_db()
    try:
        n = asyncio.run(register_live_hypotheses(db_path=db_path))
        assert n == 4
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT hypothesis_id, notes FROM hypotheses WHERE name LIKE 'live_ingame.%'"
        ).fetchall()
        conn.close()
        assert len(rows) == 4
        for _, notes in rows:
            assert '"category": "live_ingame"' in (notes or "")

        # Idempotent — second call inserts 0.
        n2 = asyncio.run(register_live_hypotheses(db_path=db_path))
        assert n2 == 0
    finally:
        os.unlink(db_path)
