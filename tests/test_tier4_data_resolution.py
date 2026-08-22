"""Tier 4 data-plane characterization tests — resolution & snapshot logic.

Pins paper-trade resolution semantics in data_collector (the path that writes
`actual_result`, which feeds CLV, which gates real money per ROADMAP §3.3) and
line_monitor's WS-delta merge. Uses an in-memory aiosqlite DB where the code
under test accepts one; no live network.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
import pytest_asyncio

from tools.data_collector import DataCollector


SCHEMA = """
CREATE TABLE game_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    game_date TEXT NOT NULL,
    local_game_date DATE,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    total_score INTEGER,
    spread_result REAL,
    winner TEXT,
    source TEXT DEFAULT 'espn',
    UNIQUE(sport, game_date, home_team, away_team)
);
CREATE TABLE game_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    game_date TEXT,
    local_game_date TEXT,
    home_team TEXT,
    away_team TEXT,
    home_score INTEGER,
    away_score INTEGER,
    context_json TEXT
);
CREATE TABLE paper_trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT, game_date TEXT, event_id TEXT,
    market TEXT, line REAL, side TEXT,
    player TEXT,
    home_team TEXT, away_team TEXT,
    actual_result TEXT, actual_stat REAL,
    closing_odds REAL, closing_implied REAL, clv_implied REAL,
    signal_implied_prob REAL
);
CREATE TABLE player_stats (
    sport TEXT, event_id TEXT, game_date TEXT,
    player_name TEXT, team TEXT, stat_type TEXT,
    stat_value REAL, minutes_played REAL
);
CREATE TABLE closing_lines (
    event_id TEXT, market TEXT, team TEXT,
    closing_odds REAL, closing_implied REAL,
    source TEXT, captured_at TEXT
);
CREATE TABLE odds_snapshots (
    sport TEXT, timestamp TEXT, snapshot_json TEXT
);
"""


@pytest_asyncio.fixture
async def collector(tmp_path):
    db_path = str(tmp_path / "t.db")
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    c = DataCollector(db_path=db_path)
    import aiosqlite
    c._db = await aiosqlite.connect(db_path)
    yield c
    await c._db.close()


def _mk_trade(**kw):
    base = dict(sport="basketball_nba", game_date="2026-08-20",
                market="totals", side="Over", line=220.5,
                home_team="Lakers", away_team="Celtics")
    base.update(kw)
    cols = ", ".join(base.keys())
    qs = ",".join("?" for _ in base)
    return cols, qs, tuple(base.values())


class TestSpreadResolution:
    """data_collector.resolve_game_level_outcomes spread arithmetic."""

    @pytest.mark.asyncio
    async def test_home_favorite_covers(self, collector):
        await collector._db.execute(
            "INSERT INTO game_results (sport, game_date, home_team, away_team,"
            " home_score, away_score, total_score, spread_result, winner)"
            " VALUES ('basketball_nba','2026-08-20','Los Angeles Lakers',"
            " 'Boston Celtics',112,100,212,12,'Los Angeles Lakers')")
        # Home -3.5, wins by 12 -> margin+(-3.5)=+8.5 -> won
        cols, qs, vals = _mk_trade(market="spreads", side="Los Angeles Lakers", line=-3.5)
        await collector._db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({qs})", vals)
        await collector._db.commit()

        r = await collector.resolve_game_level_outcomes("basketball_nba", "2026-08-20")
        cur = await collector._db.execute(
            "SELECT actual_result FROM paper_trades WHERE trade_id=1")
        assert (await cur.fetchone())[0] == "won"
        assert r["resolved"] == 1

    @pytest.mark.asyncio
    async def test_away_underdog_push(self, collector):
        await collector._db.execute(
            "INSERT INTO game_results (sport, game_date, home_team, away_team,"
            " home_score, away_score, total_score, spread_result, winner)"
            " VALUES ('basketball_nba','2026-08-20','A Home','B Away',105,98,203,-7,'A Home')")
        # Away +7, loses by 7 -> margin(away)= -7 + 7 = 0 -> push
        cols, qs, vals = _mk_trade(market="spreads", side="B Away", line=7.0)
        await collector._db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({qs})", vals)
        await collector._db.commit()
        await collector.resolve_game_level_outcomes("basketball_nba", "2026-08-20")
        cur = await collector._db.execute(
            "SELECT actual_result FROM paper_trades WHERE trade_id=1")
        assert (await cur.fetchone())[0] == "push"

    @pytest.mark.asyncio
    async def test_total_over_resolution(self, collector):
        await collector._db.execute(
            "INSERT INTO game_results (sport, game_date, home_team, away_team,"
            " home_score, away_score, total_score, spread_result, winner)"
            " VALUES ('basketball_nba','2026-08-20','Lakers','Celtics',110,108,218,2,'Lakers')")
        cols, qs, vals = _mk_trade(market="totals", side="Over", line=215.5)
        await collector._db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({qs})", vals)
        await collector._db.commit()
        await collector.resolve_game_level_outcomes("basketball_nba", "2026-08-20")
        cur = await collector._db.execute(
            "SELECT actual_result FROM paper_trades WHERE trade_id=1")
        assert (await cur.fetchone())[0] == "won"


class TestPropResolution:
    @pytest.mark.asyncio
    async def test_exact_then_fuzzy(self, collector):
        await collector._db.execute(
            "INSERT INTO player_stats VALUES ('basketball_nba','e1','2026-08-20',"
            "'LeBron James','Lakers','points',31,NULL)")
        cols, qs, vals = _mk_trade(market="player_points", side="Over", line=29.5,
                                   player="Lebron James")  # case differs -> fuzzy path
        await collector._db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({qs})", vals)
        await collector._db.commit()
        r = await collector.resolve_prop_outcomes("basketball_nba", "2026-08-20")
        assert r["resolved"] == 1
        cur = await collector._db.execute(
            "SELECT actual_result, actual_stat FROM paper_trades WHERE trade_id=1")
        row = await cur.fetchone()
        assert row[0] == "won" and row[1] == 31

    @pytest.mark.asyncio
    async def test_unresolved_stays_null_not_won(self, collector):
        # No stats collected: trade must remain unresolved (NULL), NOT default to lost
        cols, qs, vals = _mk_trade(market="player_points", side="Over", line=10.5,
                                   player="Nobody")
        await collector._db.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({qs})", vals)
        await collector._db.commit()
        await collector.resolve_prop_outcomes("basketball_nba", "2026-08-20")
        cur = await collector._db.execute(
            "SELECT actual_result FROM paper_trades WHERE trade_id=1")
        assert (await cur.fetchone())[0] is None


class TestClosingLineImpliedProb:
    """_closing_from_snapshot computes implied prob from American price.
    Pin the formula: +price -> 100/(p+100), -price -> |p|/(|p|+100)."""

    @pytest.mark.asyncio
    async def test_sharp_book_preferred(self, collector):
        snap = {"games": [{"id": "e1", "bookmakers": [
            {"title": "DraftKings", "markets": [
                {"key": "totals", "outcomes": [{"name": "Over", "price": -110}]}]},
            {"title": "Pinnacle", "markets": [
                {"key": "totals", "outcomes": [{"name": "Over", "price": -105}]}]},
        ]}]}
        for bm in snap["games"][0]["bookmakers"]:
            pass
        await collector._db.execute(
            "INSERT INTO odds_snapshots VALUES ('basketball_nba', "
            "'2026-08-20T23:50:00+00:00', ?)",
            (json.dumps(snap),))
        out = await collector._closing_from_snapshot(
            "basketball_nba", "2026-08-20", "e1", "totals", "Over")
        assert out is not None
        price, imp = out
        assert price == -105                      # sharp book preferred over DK
        assert abs(imp - (105/205)) < 0.001       # devig NOWHERE — raw implied

    @pytest.mark.asyncio
    async def test_no_snapshot_returns_none(self, collector):
        out = await collector._closing_from_snapshot(
            "basketball_nba", "2026-08-20", "missing", "totals", "Over")
        assert out is None


# ── line_monitor pure helpers ───────────────────────────────────────────────
from tools.line_monitor import (
    _merge_delta_into_snapshot,
    _stamp_snapshot_fetched_at,
    _ws_update_to_snapshot,
)


class TestWsDeltaMerge:
    def test_merge_replaces_only_that_book(self):
        base = {"sport": "basketball_nba", "game_count": 1, "games": [{
            "id": "g1", "home_team": "A", "away_team": "B",
            "bookmakers": [
                {"key": "draftkings", "title": "DK",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -110}]}]},
                {"key": "fanduel", "title": "FD",
                 "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -108}]}]},
            ]}]}
        delta = {"sport": "basketball_nba", "game_count": 1, "ingest_source": "ws",
                 "games": [{"id": "g1", "bookmakers": [
                     {"key": "draftkings", "title": "DK",
                      "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -130}]}]}]}]}
        merged = _merge_delta_into_snapshot(base, delta, "2026-01-01T00:00:00+00:00")
        g = merged["games"][0]
        assert len(g["bookmakers"]) == 2  # consensus preserved
        dk = [b for b in g["bookmakers"] if b["key"] == "draftkings"][0]
        fd = [b for b in g["bookmakers"] if b["key"] == "fanduel"][0]
        assert dk["markets"][0]["outcomes"][0]["price"] == -130   # fresh
        assert fd["markets"][0]["outcomes"][0]["price"] == -108   # aged but kept

    def test_new_event_appended(self):
        base = {"sport": "s", "game_count": 0, "games": []}
        delta = {"sport": "s", "games": [{"id": "new", "bookmakers": []}]}
        merged = _merge_delta_into_snapshot(base, delta, "now")
        assert merged["game_count"] == 1


class TestFetchedAtStamp:
    def test_does_not_overwrite_ws_stamp(self):
        snap = {"games": [{"bookmakers": [
            {"fetched_at": "EARLIER", "last_update": "LATER", "markets": [
                {"outcomes": [{"fetched_at": "EARLIEST"}]}]}]}]}
        _stamp_snapshot_fetched_at(snap, "NOW")
        bm = snap["games"][0]["bookmakers"][0]
        oc = bm["markets"][0]["outcomes"][0]
        assert bm["fetched_at"] == "EARLIER"      # earlier stamp preserved
        assert oc["fetched_at"] == "EARLIEST"

    def test_backfills_missing(self):
        snap = {"games": [{"bookmakers": [
            {"last_update": "LU", "markets": [{"outcomes": [{"name": "x"}]}]}]}]}
        _stamp_snapshot_fetched_at(snap, "NOW")
        bm = snap["games"][0]["bookmakers"][0]
        assert bm["fetched_at"] == "LU"
        assert bm["markets"][0]["outcomes"][0]["fetched_at"] == "LU"


class TestWsUpdateRouting:
    def test_routes_league_to_sport_key(self):
        msg = {"id": "42", "sport": "basketball", "league": "NCAA W",
               "bookie": "DraftKings",
               "markets": [{"name": "ML", "outcomes": [{"name": "A", "price": -120}]}]}
        mapped = _ws_update_to_snapshot(msg)
        assert mapped is not None
        sport_key, snap = mapped
        assert sport_key == "basketball_ncaaw"

    def test_missing_bookie_dropped(self):
        assert _ws_update_to_snapshot({"id": "1", "sport": "basketball"}) is None
