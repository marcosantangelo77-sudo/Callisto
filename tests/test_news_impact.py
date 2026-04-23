"""Tests for tools.news_impact.

Covers:
  * News event + large line move within window → line_moved=True, not an edge
  * News event + NO line move within window, high projected impact → edge
    emitted as ev_opportunities row with thesis_tag='news_reaction'
  * Severity gate: minor single-source events never emit edges
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    for mod in ("tools.news_impact", "tools.news_ingestion", "tools.ingestion_tracking"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _init_schema(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE news_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT, event_id TEXT, player_name TEXT,
                event_type TEXT, severity TEXT, body_part TEXT, status TEXT,
                first_seen_at TIMESTAMP, confirmed_at TIMESTAMP,
                source TEXT, source_url TEXT, raw_json TEXT,
                local_game_date DATE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE line_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                team TEXT, market TEXT, bookmaker TEXT,
                old_price INTEGER, new_price INTEGER, price_movement INTEGER,
                old_point REAL, new_point REAL, point_movement REAL,
                direction TEXT, ev_analysis TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE player_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL, event_id TEXT, game_date DATE NOT NULL,
                player_name TEXT NOT NULL, team TEXT NOT NULL,
                stat_type TEXT NOT NULL, stat_value REAL NOT NULL,
                minutes_played REAL, source TEXT DEFAULT 'espn',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE ev_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                sport TEXT, game_id TEXT, team TEXT, market TEXT,
                bookmaker TEXT, american_odds INTEGER,
                implied_probability REAL, estimated_true_prob REAL,
                edge REAL, expected_value REAL, kelly_fraction REAL,
                status TEXT DEFAULT 'open',
                source TEXT DEFAULT 'line_movement',
                steam_only INTEGER DEFAULT 0,
                is_live INTEGER DEFAULT 0,
                thesis_tag TEXT,
                expires_at TEXT
            )
            """
        )
        await db.commit()


async def _seed_player_stats(db_path: str, sport: str, player: str) -> None:
    """Seed 10 recent stats for ``player`` + some team baseline for others."""
    async with aiosqlite.connect(db_path) as db:
        now = datetime.now(timezone.utc)
        # Star player: 30 pts/game
        for i in range(10):
            await db.execute(
                "INSERT INTO player_stats "
                "(sport, game_date, player_name, team, stat_type, stat_value) "
                "VALUES (?, ?, ?, ?, 'points', ?)",
                (sport, (now - timedelta(days=i)).date().isoformat(),
                 player, "TeamA", 30.0),
            )
        # Baseline team: many players, 12 pts avg
        for i in range(30):
            await db.execute(
                "INSERT INTO player_stats "
                "(sport, game_date, player_name, team, stat_type, stat_value) "
                "VALUES (?, ?, ?, ?, 'points', ?)",
                (sport, (now - timedelta(days=i % 10)).date().isoformat(),
                 f"Baseline{i}", "TeamA", 12.0),
            )
        await db.commit()


# ── Tests ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_line_moved_after_news_is_not_under_reaction(tmp_db):
    await _init_schema(tmp_db)
    await _seed_player_stats(tmp_db, "basketball_nba", "Jayson Tatum")
    from tools.news_impact import score_news_event

    t0 = datetime.now(timezone.utc)
    # News: Tatum out, moderate severity, confirmed
    news_row = {
        "id": 1,
        "sport": "basketball_nba",
        "player_name": "Jayson Tatum",
        "event_type": "injury",
        "severity": "severe",
        "body_part": "lower_body",
        "status": "out",
        "first_seen_at": t0.isoformat(),
        "confirmed_at": (t0 + timedelta(minutes=5)).isoformat(),
        "source": "espn.injuries+rotowire.news",
        "raw_json": None,
    }
    async with aiosqlite.connect(tmp_db) as db:
        # Line movement 10 min after news: +20 price, significant
        await db.execute(
            "INSERT INTO line_movements "
            "(sport, detected_at, team, market, bookmaker, "
            " old_price, new_price, price_movement, old_point, new_point, "
            " point_movement, direction) "
            "VALUES (?, ?, ?, 'spread', 'fanduel', ?, ?, ?, ?, ?, ?, 'up')",
            (
                "basketball_nba",
                (t0 + timedelta(minutes=10)).isoformat(),
                "Boston Celtics", -110, -130, 20, 0.0, -1.5, -1.5,
            ),
        )
        await db.commit()

        report = await score_news_event(db, news_row)

    assert report.line_moved is True
    assert report.is_under_reaction is False
    assert report.is_actionable is False


@pytest.mark.asyncio
async def test_no_line_move_high_impact_emits_edge(tmp_db):
    await _init_schema(tmp_db)
    await _seed_player_stats(tmp_db, "basketball_nba", "Jayson Tatum")
    from tools.news_impact import (
        score_news_event,
        emit_news_reaction_edge,
        IMPACT_EDGE_THRESHOLD,
    )

    t0 = datetime.now(timezone.utc)
    news_row = {
        "id": 2,
        "sport": "basketball_nba",
        "player_name": "Jayson Tatum",
        "event_type": "injury",
        "severity": "severe",
        "body_part": "lower_body",
        "status": "out",
        "first_seen_at": t0.isoformat(),
        "confirmed_at": (t0 + timedelta(minutes=5)).isoformat(),
        "source": "espn.injuries+rotowire.news",
        "raw_json": None,
    }

    async with aiosqlite.connect(tmp_db) as db:
        # NO line movement rows — book hasn't priced it
        report = await score_news_event(db, news_row)
        assert report.line_moved is False
        # With 30pt player vs 12pt baseline and the compute formula:
        #   player_avg / (team_avg * 5) = 30 / (12 * 5) = 0.50 → clamped 0.5
        # Should clear the 0.10 threshold comfortably.
        assert report.projected_impact >= IMPACT_EDGE_THRESHOLD
        assert report.is_under_reaction is True
        assert report.is_actionable is True

        rid = await emit_news_reaction_edge(db, report, news_row)
        assert rid is not None

        cur = await db.execute(
            "SELECT thesis_tag, is_live, expires_at, team, source, status "
            "FROM ev_opportunities WHERE id = ?",
            (rid,),
        )
        row = await cur.fetchone()

    thesis_tag, is_live, expires_at, team, source, status = row
    assert thesis_tag == "news_reaction"
    assert is_live == 1
    assert source == "news_reaction"
    assert status == "open"
    # Expires within ~60min
    expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    assert timedelta(minutes=55) <= (expires - now) <= timedelta(minutes=65)


@pytest.mark.asyncio
async def test_minor_unconfirmed_news_is_not_actionable(tmp_db):
    await _init_schema(tmp_db)
    await _seed_player_stats(tmp_db, "basketball_nba", "Jayson Tatum")
    from tools.news_impact import score_news_event

    t0 = datetime.now(timezone.utc)
    news_row = {
        "id": 3,
        "sport": "basketball_nba",
        "player_name": "Jayson Tatum",
        "event_type": "injury",
        "severity": "minor",         # below severity gate
        "body_part": "lower_body",
        "status": "probable",
        "first_seen_at": t0.isoformat(),
        "confirmed_at": None,          # single-source
        "source": "espn.injuries",
        "raw_json": None,
    }
    async with aiosqlite.connect(tmp_db) as db:
        report = await score_news_event(db, news_row)
    # Even if under-reaction, gate stops emission
    assert report.is_actionable is False


@pytest.mark.asyncio
async def test_process_news_events_smoke(tmp_db):
    """End-to-end smoke: seed news_events + no odds movement → process emits."""
    await _init_schema(tmp_db)
    await _seed_player_stats(tmp_db, "basketball_nba", "Jayson Tatum")
    from tools.news_impact import process_news_events

    t0 = datetime.now(timezone.utc)
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute(
            "INSERT INTO news_events "
            "(sport, player_name, event_type, severity, body_part, status, "
            " first_seen_at, confirmed_at, source) "
            "VALUES (?, ?, 'injury', 'severe', 'lower_body', 'out', ?, ?, ?)",
            (
                "basketball_nba", "Jayson Tatum", t0.isoformat(),
                (t0 + timedelta(minutes=2)).isoformat(),
                "espn.injuries+rotowire.news",
            ),
        )
        await db.commit()

    report = await process_news_events(db_path=tmp_db, since_minutes=60)
    assert report["scored"] == 1
    assert report["under_reactions"] >= 1
    assert report["emitted"] >= 1

    # Verify the ev_opportunities row exists with is_live=1
    async with aiosqlite.connect(tmp_db) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM ev_opportunities WHERE thesis_tag='news_reaction'"
        )
        (cnt,) = await cur.fetchone()
    assert cnt >= 1
