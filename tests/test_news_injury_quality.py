"""Quality tests for news + injury ingestion and downstream wiring.

Covers the quality concerns the merged v1 didn't address:
  * Cross-feed headline-hash dedup (same headline, different provider)
  * 24h staleness decay + hard-drop of older rows
  * Relevance gating (no sport / no player+team => dropped at persist time)
  * End-to-end: fake article about a fake player produces a scored row
    in news_impact_scores that /news/impact/recent can return.
  * Bet executor sizing multiplier clamps and applies correctly.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    for mod in (
        "tools.news_ingestion",
        "tools.news_impact",
        "tools.ingestion_tracking",
    ):
        if mod in sys.modules:
            del sys.modules[mod]
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _init_all_schema(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                status TEXT NOT NULL,
                rows_ingested INTEGER DEFAULT 0,
                error_class TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                extra_json TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS news_events (
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
            CREATE TABLE IF NOT EXISTS line_movements (
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
            CREATE TABLE IF NOT EXISTS player_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL, event_id TEXT, game_date DATE NOT NULL,
                player_name TEXT NOT NULL, team TEXT NOT NULL,
                stat_type TEXT NOT NULL, stat_value REAL NOT NULL,
                minutes_played REAL, source TEXT DEFAULT 'espn',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


# ── 1. Headline-hash dedup ──────────────────────────────────────────────

def test_headline_hash_normalises_whitespace_and_punct():
    from tools.news_ingestion import headline_hash, normalize_headline

    a = "LeBron James OUT (ankle) - will miss the game!"
    b = "   lebron james out  ankle will miss the game  "
    # Different punctuation + whitespace + casing, same content → same hash
    assert headline_hash(a) == headline_hash(b)
    assert normalize_headline(a) != ""
    assert headline_hash(None) is None
    assert headline_hash("") is None


def test_dedupe_injuries_collapses_identical_headline_across_feeds():
    from tools.news_ingestion import InjuryEvent, dedupe_injuries

    shared_headline = {"headline": "Star Forward ruled out with ankle sprain"}
    ev_espn = InjuryEvent(
        sport="basketball_nba", player_name="Team X Star",
        team="Team X", body_part=None,
        status="out", severity="severe",
        first_seen_at="2026-04-23T12:00:00+00:00",
        source="espn.injuries", source_url=None,
        raw=shared_headline,
    )
    ev_roto = InjuryEvent(
        sport="basketball_nba", player_name="Team X Star Player",  # diff name
        team=None, body_part="lower_body",                        # diff body_part
        status="out", severity="severe",
        first_seen_at="2026-04-23T12:01:00+00:00",
        source="rotowire.news", source_url=None,
        raw=shared_headline,
    )
    deduped = dedupe_injuries([ev_espn, ev_roto])
    assert len(deduped) == 1, "identical headlines from two feeds must collapse"
    row = deduped[0]
    assert "espn.injuries" in row["source"] and "rotowire.news" in row["source"]
    assert row["confirmed_at"], "cross-source confirmation must set confirmed_at"


# ── 2. Staleness decay ──────────────────────────────────────────────────

def test_staleness_factor_shape():
    from tools.news_impact import staleness_factor, STALENESS_WINDOW_MINUTES

    # Brand new = full weight
    assert staleness_factor(0.0) == pytest.approx(1.0)
    # Half-window = half weight
    half = staleness_factor(STALENESS_WINDOW_MINUTES / 2)
    assert 0.45 <= half <= 0.55
    # Past window = zero
    assert staleness_factor(STALENESS_WINDOW_MINUTES) == pytest.approx(0.0)
    assert staleness_factor(STALENESS_WINDOW_MINUTES * 2) == pytest.approx(0.0)
    # Negative (clock skew) = zero
    assert staleness_factor(-5.0) == pytest.approx(0.0)


def test_score_news_event_applies_decay_to_impact(tmp_db):
    async def run():
        await _init_all_schema(tmp_db)
        from tools.news_impact import score_news_event

        # Seed player_stats so compute_expected_starter_impact returns > 0
        async with aiosqlite.connect(tmp_db) as db:
            # Star: 30 ppg; team avg stays low so ratio is high
            await db.executemany(
                "INSERT INTO player_stats "
                "(sport, game_date, player_name, team, stat_type, stat_value) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("basketball_nba", "2026-04-20", "Fake Star",
                     "Fake Team", "pts", 30.0),
                    ("basketball_nba", "2026-04-21", "Fake Star",
                     "Fake Team", "pts", 32.0),
                    ("basketball_nba", "2026-04-22", "Other", "Fake Team",
                     "pts", 5.0),
                ],
            )
            await db.commit()

            # Fresh: age 0min → decayed == projected
            now = datetime.now(timezone.utc)
            fresh_row = {
                "id": 1,
                "sport": "basketball_nba",
                "player_name": "Fake Star",
                "first_seen_at": now.isoformat(),
                "severity": "severe",
                "confirmed_at": now.isoformat(),
                "raw_json": json.dumps({"team": "Fake Team"}),
                "event_type": "injury",
            }
            r_fresh = await score_news_event(db, fresh_row, now=now)
            assert r_fresh.age_minutes < 1.0
            assert r_fresh.is_stale is False
            assert r_fresh.decayed_impact == pytest.approx(
                r_fresh.projected_impact, rel=1e-3
            )

            # Stale: 25h old → decayed == 0, is_stale=True
            old_ts = (now - timedelta(hours=25)).isoformat()
            stale_row = {**fresh_row, "id": 2, "first_seen_at": old_ts}
            r_stale = await score_news_event(db, stale_row, now=now)
            assert r_stale.is_stale is True
            assert r_stale.decayed_impact == pytest.approx(0.0)
            assert r_stale.is_under_reaction is False

    asyncio.run(run())


# ── 3. Relevance gating ─────────────────────────────────────────────────

def test_persist_drops_rows_without_sport_or_player(tmp_db):
    async def run():
        await _init_all_schema(tmp_db)
        from tools.news_ingestion import persist_news_rows

        rows = [
            # No sport → drop
            {"sport": None, "player_name": "Some Guy",
             "event_type": "injury", "severity": "severe",
             "first_seen_at": datetime.now(timezone.utc).isoformat(),
             "source": "espn.injuries", "raw_json": "{}"},
            # No player AND no team → drop
            {"sport": "basketball_nba", "player_name": None,
             "event_type": "injury", "severity": "severe",
             "first_seen_at": datetime.now(timezone.utc).isoformat(),
             "source": "espn.injuries", "raw_json": "{}"},
            # Valid: sport + player
            {"sport": "basketball_nba", "player_name": "Real Player",
             "body_part": "lower_body",
             "event_type": "injury", "severity": "severe",
             "first_seen_at": datetime.now(timezone.utc).isoformat(),
             "source": "espn.injuries",
             "raw_json": json.dumps({"headline": "Real Player hurts ankle"})},
            # Valid: sport + team (no player)
            {"sport": "basketball_nba", "player_name": None,
             "event_type": "coaching_decision", "severity": "severe",
             "first_seen_at": datetime.now(timezone.utc).isoformat(),
             "source": "espn.scoreboard.notes",
             "raw_json": json.dumps({"team": "Fake Team",
                                     "headline": "coach rests starters"})},
        ]
        inserted = await persist_news_rows(rows, db_path=tmp_db)
        assert inserted == 2, "expected 2 valid, 2 dropped"

        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM news_events "
                "WHERE sport IS NULL OR (player_name IS NULL AND "
                "(raw_json NOT LIKE '%team%' OR raw_json IS NULL))"
            )
            assert (await cur.fetchone())[0] == 0

    asyncio.run(run())


def test_persist_second_call_headline_hash_dedup(tmp_db):
    async def run():
        await _init_all_schema(tmp_db)
        from tools.news_ingestion import persist_news_rows

        now = datetime.now(timezone.utc).isoformat()
        # Same headline twice with slightly different structured fields
        row_a = {
            "sport": "basketball_nba", "player_name": "Player A",
            "body_part": "lower_body", "event_type": "injury",
            "severity": "severe", "first_seen_at": now,
            "source": "espn.injuries",
            "raw_json": json.dumps({"headline": "Identical wire copy"}),
        }
        row_b = {
            **row_a,
            "player_name": "Player A Jr",  # different player name
            "body_part": None,              # different body_part
            "source": "rotowire.news",
            # same headline text
            "raw_json": json.dumps({"headline": "Identical wire copy"}),
        }
        # First persist inserts 1
        assert await persist_news_rows([row_a], db_path=tmp_db) == 1
        # Second persist with new structured key but same headline hash →
        # dedup layer 2 blocks the insert
        assert await persist_news_rows([row_b], db_path=tmp_db) == 0

    asyncio.run(run())


# ── 4. End-to-end: fake article → impact row → endpoint retrieval ──────

def test_end_to_end_fake_player_produces_recent_impact_row(tmp_db):
    async def run():
        await _init_all_schema(tmp_db)
        from tools.news_ingestion import persist_news_rows
        from tools.news_impact import (
            process_news_events,
            get_recent_impact_scores,
        )

        # Seed stats so projected_impact > 0
        async with aiosqlite.connect(tmp_db) as db:
            await db.executemany(
                "INSERT INTO player_stats "
                "(sport, game_date, player_name, team, stat_type, stat_value) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("basketball_nba", "2026-04-22", "Mythical Hooper",
                     "Mythical Team", "pts", 28.0),
                    ("basketball_nba", "2026-04-22", "Bench Guy",
                     "Mythical Team", "pts", 4.0),
                ],
            )
            await db.commit()

        now = datetime.now(timezone.utc).isoformat()
        fake_article = [{
            "sport": "basketball_nba",
            "player_name": "Mythical Hooper",
            "body_part": "lower_body",
            "event_type": "injury",
            "severity": "severe",
            "status": "out",
            "first_seen_at": now,
            "confirmed_at": now,
            "source": "espn.injuries+rotowire.news",
            "source_url": None,
            "raw_json": json.dumps({
                "team": "Mythical Team",
                "headline": "Mythical Hooper out tonight with ankle sprain",
            }),
        }]

        assert await persist_news_rows(fake_article, db_path=tmp_db) == 1

        report = await process_news_events(db_path=tmp_db)
        assert report.get("scored", 0) >= 1
        assert report.get("persisted", 0) >= 1

        # Retrieve via the same function that /news/impact/recent uses
        rows = await get_recent_impact_scores(
            db_path=tmp_db, sport="basketball_nba",
        )
        assert len(rows) >= 1
        target = [r for r in rows if r["player_name"] == "Mythical Hooper"]
        assert target, "scored row must land in news_impact_scores"
        r = target[0]
        assert r["sport"] == "basketball_nba"
        assert r["team"] == "Mythical Team"
        assert r["is_stale"] == 0
        assert r["projected_impact"] > 0.0
        assert r["decayed_impact"] > 0.0

        # Team-filter also matches
        team_rows = await get_recent_impact_scores(
            db_path=tmp_db, team="Mythical",
        )
        assert any(r["player_name"] == "Mythical Hooper" for r in team_rows)

    asyncio.run(run())


# ── 5. Bet executor news multiplier ─────────────────────────────────────

def test_news_sizing_multiplier_clamped():
    """compute_stake must clamp the news multiplier to [0.75, 1.25]."""
    from tools.bet_executor import BetExecutor, MIN_BET_AMOUNT

    bx = BetExecutor()
    base = bx.compute_stake(
        edge=0.05, odds=-110, bankroll=10000.0, confidence=0.70,
    )
    assert base > MIN_BET_AMOUNT

    # Above-ceiling multiplier clamps to 1.25
    boosted = bx.compute_stake(
        edge=0.05, odds=-110, bankroll=10000.0, confidence=0.70,
        news_multiplier=5.0,
    )
    # boosted <= base * 1.25 plus rounding tolerance
    assert boosted <= base * 1.25 + 0.01
    assert boosted >= base  # should have grown

    # Below-floor multiplier clamps to 0.75
    haircut = bx.compute_stake(
        edge=0.05, odds=-110, bankroll=10000.0, confidence=0.70,
        news_multiplier=0.0,
    )
    # haircut >= base * 0.75 minus rounding tolerance
    assert haircut >= base * 0.75 - 0.01
    assert haircut < base  # should have shrunk


def test_news_sizing_multiplier_noop_when_one():
    from tools.bet_executor import BetExecutor

    bx = BetExecutor()
    base = bx.compute_stake(
        edge=0.04, odds=-110, bankroll=10000.0, confidence=0.65,
    )
    same = bx.compute_stake(
        edge=0.04, odds=-110, bankroll=10000.0, confidence=0.65,
        news_multiplier=1.0,
    )
    assert base == same
