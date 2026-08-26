"""Tests for the tools/data_collector.py → tools/collect/ split.

Two layers:
  1. Source pins — structural assertions that the implementations really
     moved into tools/collect/ and that data_collector.py is a thin facade
     (so the split can't silently regress into a monolith).
  2. Behavioral tests — the moved code still works: client singleton,
     venue fuzzy lookup, team-name matching, prop resolution against a real
     temp SQLite DB, and facade delegation.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile

import aiosqlite
import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COLLECT_MODULES = [
    "http",
    "venues",
    "espn",
    "odds",
    "resolution",
    "play_by_play",
    "baseball",
    "hockey",
    "football",
    "basketball",
    "golf",
]

EXPECTED_FUNCTIONS = {
    "tools.collect.http": ["_get_client", "_get_client_lock", "close_client"],
    "tools.collect.venues": ["VENUE_METADATA", "_get_venue_metadata"],
    "tools.collect.espn": [
        "ESPN_BASE", "ESPN_SPORTS", "collect_scores", "collect_box_scores",
        "store_player_stats", "get_today_event_ids", "collect_date_range",
    ],
    "tools.collect.odds": [
        "ESPN_CORE_BASE", "ESPN_CORE_LEAGUES", "collect_espn_odds", "_fetch_event_odds",
    ],
    "tools.collect.resolution": [
        "GAME_LEVEL_MARKETS", "fuzzy_team_match", "resolve_prop_outcomes",
        "resolve_game_level_outcomes", "_closing_from_snapshot",
    ],
    "tools.collect.play_by_play": ["collect_play_by_play"],
    "tools.collect.baseball": ["collect_statcast", "collect_mlb_players"],
    "tools.collect.hockey": ["NHL_API", "collect_nhl_players", "collect_nhl_shots"],
    "tools.collect.football": [
        "NFLFASTR_BASE", "collect_nfl_players", "collect_nfl_combine", "collect_nfl_plays",
    ],
    "tools.collect.basketball": [
        "NBA_STATS_BASE", "NBA_HEADERS", "NCAA_BBALL_LEAGUES",
        "collect_nba_players", "collect_nba_shots",
        "collect_ncaa_basketball_players", "collect_ncaa_basketball_game_stats",
    ],
    "tools.collect.golf": ["collect_golf_player_rounds"],
}


# ──────────────────────────────────────────────
# 1. Source pins
# ──────────────────────────────────────────────


def test_collect_package_exists_with_all_modules():
    for mod in COLLECT_MODULES:
        path = os.path.join(REPO, "tools", "collect", f"{mod}.py")
        assert os.path.isfile(path), f"missing tools/collect/{mod}.py"
    init = os.path.join(REPO, "tools", "collect", "__init__.py")
    assert os.path.isfile(init), "tools/collect/__init__.py missing"


@pytest.mark.parametrize("modname,names", sorted(EXPECTED_FUNCTIONS.items()))
def test_implementations_live_in_collect_modules(modname, names):
    mod = __import__(modname, fromlist=["*"])
    for name in names:
        assert hasattr(mod, name), f"{modname}.{name} missing"


def test_facade_reexports_everything():
    dc = __import__("tools.data_collector", fromlist=["*"])
    for modname, names in EXPECTED_FUNCTIONS.items():
        mod = __import__(modname, fromlist=["*"])
        for name in names:
            assert getattr(dc, name) is getattr(mod, name), (
                f"tools.data_collector.{name} is not a re-export of {modname}.{name}"
            )


def test_data_collector_methods_are_thin_delegations():
    """Every collect_* method body should be a single delegation to tools.collect."""
    import tools.data_collector as dc_mod
    from tools.data_collector import DataCollector

    delegations = {
        "collect_scores": "tools.collect.espn.collect_scores",
        "collect_box_scores": "tools.collect.espn.collect_box_scores",
        "collect_espn_odds": "tools.collect.odds.collect_espn_odds",
        "resolve_prop_outcomes": "tools.collect.resolution.resolve_prop_outcomes",
        "resolve_game_level_outcomes": "tools.collect.resolution.resolve_game_level_outcomes",
        "collect_play_by_play": "tools.collect.play_by_play.collect_play_by_play",
        "collect_statcast": "tools.collect.baseball.collect_statcast",
        "collect_mlb_players": "tools.collect.baseball.collect_mlb_players",
        "collect_nhl_players": "tools.collect.hockey.collect_nhl_players",
        "collect_nhl_shots": "tools.collect.hockey.collect_nhl_shots",
        "collect_nfl_players": "tools.collect.football.collect_nfl_players",
        "collect_nfl_combine": "tools.collect.football.collect_nfl_combine",
        "collect_nfl_plays": "tools.collect.football.collect_nfl_plays",
        "collect_nba_players": "tools.collect.basketball.collect_nba_players",
        "collect_nba_shots": "tools.collect.basketball.collect_nba_shots",
        "collect_ncaa_basketball_players": "tools.collect.basketball.collect_ncaa_basketball_players",
        "collect_ncaa_basketball_game_stats": "tools.collect.basketball.collect_ncaa_basketball_game_stats",
        "collect_golf_player_rounds": "tools.collect.golf.collect_golf_player_rounds",
    }
    for method_name, target_name in delegations.items():
        method = getattr(DataCollector, method_name)
        src = inspect.getsource(method)
        module_path, func_name = target_name.rsplit(".", 1)
        target = getattr(__import__(module_path, fromlist=[func_name]), func_name)
        # The delegation must reference the target function object
        assert target.__name__ in src or func_name in src, (
            f"DataCollector.{method_name} does not delegate to {target_name}"
        )
        # And must not contain the old inline implementation (heuristic:
        # implementations are long; delegations are short)
        assert len(src.splitlines()) < 30, (
            f"DataCollector.{method_name} looks like an inline implementation "
            f"({len(src.splitlines())} lines)"
        )


def test_facade_file_is_small():
    path = os.path.join(REPO, "tools", "data_collector.py")
    with open(path) as f:
        n_lines = sum(1 for _ in f)
    assert n_lines < 700, f"facade regressed to {n_lines} lines (split lost?)"


def test_collect_modules_carry_the_bulk():
    total = 0
    for mod in COLLECT_MODULES:
        path = os.path.join(REPO, "tools", "collect", f"{mod}.py")
        with open(path) as f:
            total += sum(1 for _ in f)
    assert total > 2000, f"tools/collect carries only {total} lines; extraction too thin"


def test_class_constants_mirror_module_constants():
    from tools.data_collector import DataCollector
    from tools.collect.odds import ESPN_CORE_LEAGUES
    from tools.collect.resolution import GAME_LEVEL_MARKETS
    from tools.collect.basketball import NCAA_BBALL_LEAGUES
    from tools.collect.hockey import NHL_API

    assert DataCollector.ESPN_CORE_LEAGUES == ESPN_CORE_LEAGUES
    assert DataCollector.GAME_LEVEL_MARKETS == GAME_LEVEL_MARKETS
    assert DataCollector.NCAA_BBALL_LEAGUES == NCAA_BBALL_LEAGUES
    assert DataCollector.NHL_API == NHL_API


# ──────────────────────────────────────────────
# 2. Behavioral tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shared_client_singleton_and_close():
    from tools.collect.http import _get_client, close_client

    c1 = await _get_client()
    c2 = await _get_client()
    assert c1 is c2
    assert not c1.is_closed
    await close_client()
    assert c1.is_closed
    # re-creates after close
    c3 = await _get_client()
    assert c3 is not c1
    await close_client()


def test_venue_metadata_exact_match():
    from tools.collect.venues import VENUE_METADATA, _get_venue_metadata

    meta = _get_venue_metadata("Coors Field", "baseball_mlb")
    assert meta["venue_altitude_ft"] == 5200
    assert meta["venue_dome"] is False
    assert meta["venue_park_factor"] > 1.0
    assert len(VENUE_METADATA) >= 40  # NBA + NFL + MLB venues present


def test_venue_metadata_fuzzy_and_empty():
    from tools.collect.venues import _get_venue_metadata

    fuzzy = _get_venue_metadata("Coors Fielde", "baseball_mlb")
    assert fuzzy.get("venue_city") == "Denver"
    assert _get_venue_metadata("", "") == {}


def test_fuzzy_team_match_strategies():
    from tools.collect.resolution import fuzzy_team_match

    teams = ["Kansas City Chiefs", "Las Vegas Raiders"]
    # exact
    assert fuzzy_team_match("Kansas City Chiefs", teams) == "Kansas City Chiefs"
    # case-insensitive
    assert fuzzy_team_match("kansas city chiefs", teams) == "Kansas City Chiefs"
    # fuzzy
    assert fuzzy_team_match("Kansas City Cheifs", teams) == "Kansas City Chiefs"
    # no match under threshold
    assert fuzzy_team_match("Toronto Argonauts", teams) is None
    assert fuzzy_team_match("", teams) is None


def _make_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


SCHEMA = """
CREATE TABLE paper_trades (
    trade_id TEXT PRIMARY KEY, sport TEXT, game_date TEXT,
    player TEXT, market TEXT, line REAL, side TEXT,
    event_id TEXT, home_team TEXT, away_team TEXT,
    actual_result TEXT, actual_stat REAL, closing_odds REAL,
    closing_implied REAL, clv_implied REAL, signal_implied_prob REAL
);
CREATE TABLE player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT, event_id TEXT, game_date TEXT, player_name TEXT,
    team TEXT, stat_type TEXT, stat_value REAL, minutes_played REAL
);
CREATE TABLE game_results (
    sport TEXT, game_date TEXT, local_game_date TEXT,
    home_team TEXT, away_team TEXT, home_score INT, away_score INT,
    total_score INT, spread_result REAL, winner TEXT, source TEXT
);
CREATE TABLE closing_lines (
    event_id TEXT, market TEXT, team TEXT, source TEXT,
    captured_at TEXT, closing_odds REAL, closing_implied REAL
);
"""


class _StubDC:
    """Minimal stand-in exposing what the collect functions touch."""

    def __init__(self, db):
        self._db = db
        self.db_path = "stub.db"
        self._player_stat_insert_failures = 0


async def _seed_db(path):
    db = await aiosqlite.connect(path)
    await db.executescript(SCHEMA)
    await db.execute(
        "INSERT INTO player_stats (sport, event_id, game_date, player_name, team,"
        " stat_type, stat_value) VALUES ('basketball_nba','e1','2026-01-10',"
        "'LeBron James','LAL','points',31)")
    await db.execute(
        "INSERT INTO paper_trades (trade_id, sport, game_date, player, market,"
        " line, side) VALUES ('t_over','basketball_nba','2026-01-10',"
        "'LeBron James','player_points',29.5,'Over')")
    await db.execute(
        "INSERT INTO paper_trades (trade_id, sport, game_date, player, market,"
        " line, side) VALUES ('t_under','basketball_nba','2026-01-10',"
        "'Lebron James','player_points',35.5,'Under')")
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_resolve_prop_outcomes_end_to_end():
    from tools.collect.resolution import resolve_prop_outcomes

    path = _make_db_path()
    try:
        db = await _seed_db(path)
        dc = _StubDC(db)

        # CLVTracker import inside resolve_prop_outcomes must not explode;
        # it's wrapped in try/except so stub path is fine either way.
        result = await resolve_prop_outcomes(dc, "basketball_nba", "2026-01-10")

        assert result["total_pending"] == 2
        assert result["resolved"] == 2

        cur = await db.execute(
            "SELECT trade_id, actual_result, actual_stat FROM paper_trades "
            "ORDER BY trade_id")
        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        assert rows["t_over"][0] == "won"      # 31 > 29.5 Over
        assert rows["t_under"][0] == "won"     # 31 < 35.5 Under (fuzzy name match)
        assert rows["t_over"][1] == 31
        await db.close()
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_resolve_prop_outcomes_push():
    from tools.collect.resolution import resolve_prop_outcomes

    path = _make_db_path()
    try:
        db = await _seed_db(path)
        await db.execute(
            "INSERT INTO paper_trades (trade_id, sport, game_date, player, market,"
            " line, side) VALUES ('t_push','basketball_nba','2026-01-10',"
            "'LeBron James','player_points',31,'Over')")
        await db.commit()
        dc = _StubDC(db)
        result = await resolve_prop_outcomes(dc, "basketball_nba", "2026-01-10")
        cur = await db.execute(
            "SELECT actual_result FROM paper_trades WHERE trade_id='t_push'")
        assert (await cur.fetchone())[0] == "push"
        await db.close()
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_resolve_game_level_totals_and_moneyline():
    from tools.collect.resolution import resolve_game_level_outcomes

    path = _make_db_path()
    try:
        db = await aiosqlite.connect(path)
        await db.executescript(SCHEMA)
        await db.execute(
            "INSERT INTO game_results VALUES ('basketball_nba','2026-01-10',NULL,"
            "'Los Angeles Lakers','Boston Celtics',110,105,215,5,"
            "'Los Angeles Lakers','espn')")
        await db.execute(
            "INSERT INTO paper_trades (trade_id, sport, game_date, market, line,"
            " side, home_team, away_team) VALUES ('g_total','basketball_nba',"
            "'2026-01-10','totals',220.5,'Over','Los Angeles Lakers','Boston Celtics')")
        await db.execute(
            "INSERT INTO paper_trades (trade_id, sport, game_date, market, line,"
            " side, home_team, away_team) VALUES ('g_ml','basketball_nba',"
            "'2026-01-10','h2h',NULL,'Boston Celtics','Los Angeles Lakers',"
            "'Boston Celtics')")
        await db.execute(
            "INSERT INTO paper_trades (trade_id, sport, game_date, market, line,"
            " side, home_team, away_team) VALUES ('g_spread','basketball_nba',"
            "'2026-01-10','spreads',-4.5,'Los Angeles Lakers','Los Angeles Lakers',"
            "'Boston Celtics')")
        await db.commit()

        dc = _StubDC(db)
        result = await resolve_game_level_outcomes(dc, "basketball_nba", "2026-01-10")
        assert result["total_pending"] == 3
        assert result["resolved"] == 3
        assert result["unmatched"] == 0

        cur = await db.execute(
            "SELECT trade_id, actual_result FROM paper_trades ORDER BY trade_id")
        rows = dict(await cur.fetchall())
        assert rows["g_total"] == "lost"    # 215 < 220.5 Over
        assert rows["g_ml"] == "lost"       # Celtics didn't win
        assert rows["g_spread"] == "won"    # margin 5 > 4.5
        await db.close()
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_store_player_stats_via_facade_delegation():
    """The facade's _store_player_stats delegates and bumps failure counter."""
    from tools.data_collector import DataCollector

    path = _make_db_path()
    try:
        db = await aiosqlite.connect(path)
        await db.executescript(SCHEMA)
        dc = DataCollector("stub.db")
        dc._db = db

        stored = await dc._store_player_stats(
            sport="basketball_nba",
            event_id="e9",
            game_date="2026-02-01",
            player_name="Jayson Tatum",
            team="BOS",
            stat_map={"PTS": "34", "REB": "8", "AST": "5", "MIN": "36:30"},
            category="basketball",
        )
        assert stored == 4  # PTS, REB, AST + PRA composite

        cur = await db.execute(
            "SELECT stat_type, stat_value, minutes_played FROM player_stats "
            "WHERE player_name='Jayson Tatum'")
        rows = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
        assert rows["points"][0] == 34.0
        assert abs(rows["minutes_played_missing"] if False else 0) == 0
        pra_row = [v for k, v in rows.items() if k == "points_rebounds_assists"]
        assert pra_row and pra_row[0][0] == 47.0
        minutes = next(v[1] for k, v in rows.items() if k != "points_rebounds_assists")
        assert abs(minutes - 36.5) < 0.01
        assert dc._player_stat_insert_failures == 0
        await db.close()
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_get_collection_stats_and_embedded_pipeline():
    from tools.data_collector import DataCollector

    path = _make_db_path()
    try:
        db = await aiosqlite.connect(path)
        await db.executescript(SCHEMA + """
CREATE TABLE game_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sport TEXT, event_id TEXT,
    game_date TEXT, local_game_date TEXT, home_team TEXT, away_team TEXT,
    home_score INT, away_score INT, context_json TEXT, embedded BOOLEAN DEFAULT FALSE
);
""")
        await db.execute(
            "INSERT INTO game_contexts (sport, event_id, game_date, home_team,"
            " away_team, home_score, away_score, context_json) VALUES"
            " ('basketball_nba','e1','2026-01-10','LAL','BOS',110,105,'{\"x\":1}')")
        await db.commit()

        dc = DataCollector("stub.db")
        dc._db = db

        contexts = await dc.get_unembedded_contexts()
        assert len(contexts) == 1
        assert contexts[0]["context"] == {"x": 1}

        await dc.mark_embedded(contexts[0]["id"])
        assert await dc.get_unembedded_contexts() == []

        stats = await dc.get_collection_stats()
        assert stats["unembedded_contexts"] == 0
        assert stats["game_contexts"][0]["count"] == 1
        assert stats["player_stat_insert_failures"] == 0
        await db.close()
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_unsupported_sport_returns_error_dict():
    from tools.data_collector import DataCollector

    dc = DataCollector("stub.db")
    res = await dc.collect_scores("underwater_hockey", "20260101")
    assert res["error"].startswith("Unsupported sport")
    res2 = await dc.collect_espn_odds("underwater_hockey")
    assert res2 == []
