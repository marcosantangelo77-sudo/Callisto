"""
Tests for tools.prop_resolver — the player-prop actual_result backfill.

Coverage:
  * OVER wins when actual > line; UNDER wins when actual < line; push on equality.
  * Missing player_stats → no state change (idempotent skip).
  * Fuzzy name match ("S. Ohtani" → "Shohei Ohtani") scores ≥ 0.90.
  * Second pass over the same DB makes zero writes.
  * Non-prop markets (h2h/totals) are ignored entirely.
  * Unknown market strings are reported, not silently mis-resolved.
  * Fallback stat_types cover the live-DB reality that MLB writes
    ``statcast_strikeouts`` instead of ``strikeouts``.
"""
from __future__ import annotations

import os

import aiosqlite
import pytest
import pytest_asyncio

from tools.player_name_index import PlayerNameIndex, fuzzy_match_score
from tools.prop_resolver import (
    ResolveReport,
    resolve_player_prop_backtest_events,
)
from tools.prop_stat_map import (
    fallback_stat_types,
    is_prop_market,
    market_to_stat_type,
)


# ── Minimal schema subset — we don't need the whole 1500-line schema.sql,
# just the three tables the resolver touches. ──
_MIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    player TEXT,
    market TEXT NOT NULL,
    line REAL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    book_odds_american INTEGER NOT NULL,
    book_implied_prob REAL NOT NULL,
    model_fair_prob REAL NOT NULL,
    model_factors TEXT,
    edge REAL NOT NULL,
    ev_pct REAL NOT NULL,
    kelly_fraction REAL,
    signal_generated BOOLEAN DEFAULT FALSE,
    actual_result TEXT,
    actual_stat REAL,
    closing_odds INTEGER,
    closing_implied REAL,
    clv_implied REAL,
    game_date DATE NOT NULL,
    local_game_date DATE,
    snapshot_time DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS player_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    game_date DATE NOT NULL,
    player_name TEXT NOT NULL,
    team TEXT NOT NULL,
    stat_type TEXT NOT NULL,
    stat_value REAL NOT NULL,
    minutes_played REAL,
    source TEXT DEFAULT 'espn',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, event_id, player_name, stat_type)
);

CREATE TABLE IF NOT EXISTS game_results (
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
    source TEXT DEFAULT 'espn'
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    rows_ingested INTEGER,
    duration_ms INTEGER,
    error_class TEXT,
    error_message TEXT,
    extra_json TEXT
);
"""


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    """Create a fresh SQLite DB with the minimal schema. Override
    CALLISTO_DB_PATH so the tracked_ingestion decorator writes its row
    into the SAME temp DB (not the real memory/callisto.db)."""
    path = str(tmp_path / "prop_resolver.db")
    # tracked_ingestion reads DB_PATH at function-def time, so we need
    # to patch the module-level constant where it actually lives.
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    import tools.ingestion_tracking as itrack
    monkeypatch.setattr(itrack, "DB_PATH", path, raising=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_MIN_SCHEMA)
        await db.commit()
    return path


async def _insert_bt_event(db_path: str, **kw) -> int:
    """Insert a backtest_events row with sensible defaults."""
    defaults = dict(
        run_id="r1", event_id="E1", hypothesis_id="H1",
        sport="baseball_mlb", player="Shohei Ohtani",
        market="pitcher_strikeouts", line=7.5, side="Over", book="draftkings",
        book_odds_american=-110, book_implied_prob=0.524,
        model_fair_prob=0.55, model_factors=None,
        edge=0.026, ev_pct=0.05, kelly_fraction=0.01,
        signal_generated=1, actual_result=None, actual_stat=None,
        game_date="2026-04-10", snapshot_time="2026-04-10T12:00:00",
    )
    defaults.update(kw)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO backtest_events "
            "(run_id, event_id, hypothesis_id, sport, player, market, line, "
            "side, book, book_odds_american, book_implied_prob, "
            "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
            "signal_generated, actual_result, actual_stat, game_date, "
            "snapshot_time) "
            "VALUES (:run_id, :event_id, :hypothesis_id, :sport, :player, "
            ":market, :line, :side, :book, :book_odds_american, "
            ":book_implied_prob, :model_fair_prob, :model_factors, :edge, "
            ":ev_pct, :kelly_fraction, :signal_generated, :actual_result, "
            ":actual_stat, :game_date, :snapshot_time)",
            defaults,
        )
        await db.commit()
        return cur.lastrowid


async def _insert_player_stat(db_path: str, **kw) -> None:
    defaults = dict(
        sport="baseball_mlb", event_id="E1", game_date="2026-04-10",
        player_name="Shohei Ohtani", team="LAD", stat_type="strikeouts",
        stat_value=9, minutes_played=None,
    )
    defaults.update(kw)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO player_stats "
            "(sport, event_id, game_date, player_name, team, stat_type, "
            "stat_value, minutes_played) "
            "VALUES (:sport, :event_id, :game_date, :player_name, :team, "
            ":stat_type, :stat_value, :minutes_played)",
            defaults,
        )
        await db.commit()


async def _insert_game_result(db_path: str, **kw) -> None:
    defaults = dict(
        sport="baseball_mlb", game_date="2026-04-10", home_team="LAD",
        away_team="SFG", home_score=5, away_score=3, total_score=8,
    )
    defaults.update(kw)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO game_results (sport, game_date, home_team, "
            "away_team, home_score, away_score, total_score) "
            "VALUES (:sport, :game_date, :home_team, :away_team, "
            ":home_score, :away_score, :total_score)",
            defaults,
        )
        await db.commit()


async def _fetch_event(db_path: str, ev_id: int) -> tuple:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT actual_result, actual_stat FROM backtest_events WHERE id = ?",
            (ev_id,),
        )
        return await cur.fetchone()


# ─────────────────────────────────────────────────────────────────────
# Unit tests — stat map + name index
# ─────────────────────────────────────────────────────────────────────


def test_market_to_stat_type_known():
    assert market_to_stat_type("pitcher_strikeouts") == "strikeouts"
    assert market_to_stat_type("batter_total_bases") == "total_bases"
    assert market_to_stat_type("player_shots_on_goal") == "shots_on_goal"
    assert market_to_stat_type("player_points") == "points"
    assert market_to_stat_type("player_rebounds_assists") == "rebounds_assists"
    assert market_to_stat_type("PLAYER_POINTS") == "points"  # case-insensitive


def test_market_to_stat_type_non_prop():
    assert market_to_stat_type("h2h") is None
    assert market_to_stat_type("totals") is None
    assert market_to_stat_type("spreads") is None
    assert market_to_stat_type("") is None
    assert market_to_stat_type(None) is None  # type: ignore[arg-type]


def test_market_to_stat_type_unknown_prop():
    # Unknown prop keys return None (not silently mis-mapped).
    assert market_to_stat_type("player_kitchen_sink") is None


def test_fallback_stat_types_mlb_strikeouts():
    # The live DB writes statcast_strikeouts; resolver must know to fall back.
    fb = fallback_stat_types("strikeouts")
    assert "statcast_strikeouts" in fb


def test_is_prop_market():
    assert is_prop_market("player_points")
    assert is_prop_market("pitcher_strikeouts")
    assert is_prop_market("batter_hits")
    assert not is_prop_market("h2h")
    assert not is_prop_market("spreads")
    assert not is_prop_market("")


def test_fuzzy_match_score_exact():
    assert fuzzy_match_score("Shohei Ohtani", "Shohei Ohtani") == 1.0


def test_fuzzy_match_score_initial_vs_full():
    # This is the headline contract from the task brief.
    score = fuzzy_match_score("S. Ohtani", "Shohei Ohtani")
    assert score >= 0.90, f"expected >= 0.90, got {score}"


def test_fuzzy_match_score_last_comma_first():
    score = fuzzy_match_score("Ohtani, Shohei", "Shohei Ohtani")
    assert score == 1.0


def test_fuzzy_match_score_jr_suffix():
    score = fuzzy_match_score("Ken Griffey Jr.", "Ken Griffey")
    assert score >= 0.95


def test_fuzzy_match_score_different_players():
    # "Mike Trout" vs "Aaron Judge" — must be LOW.
    score = fuzzy_match_score("Mike Trout", "Aaron Judge")
    assert score < 0.60


# ─────────────────────────────────────────────────────────────────────
# Integration tests — resolver against a fresh DB
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_over_wins_when_actual_above_line(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Over")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=9)
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 1
    assert report.scanned == 1
    result, actual_stat = await _fetch_event(db_path, ev_id)
    assert result == "won"
    assert actual_stat == 9.0


@pytest.mark.asyncio
async def test_over_loses_when_actual_below_line(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Over")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=5)
    await _insert_game_result(db_path)
    await resolve_player_prop_backtest_events(db_path=db_path, recalibrate=False)
    result, actual_stat = await _fetch_event(db_path, ev_id)
    assert result == "lost"
    assert actual_stat == 5.0


@pytest.mark.asyncio
async def test_under_wins_when_actual_below_line(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Under")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=5)
    await _insert_game_result(db_path)
    await resolve_player_prop_backtest_events(db_path=db_path, recalibrate=False)
    result, _ = await _fetch_event(db_path, ev_id)
    assert result == "won"


@pytest.mark.asyncio
async def test_under_loses_when_actual_above_line(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Under")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=11)
    await _insert_game_result(db_path)
    await resolve_player_prop_backtest_events(db_path=db_path, recalibrate=False)
    result, _ = await _fetch_event(db_path, ev_id)
    assert result == "lost"


@pytest.mark.asyncio
async def test_push_when_actual_equals_line(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.0, side="Over")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=7)
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 1
    result, actual = await _fetch_event(db_path, ev_id)
    assert result == "push"
    assert actual == 7.0


@pytest.mark.asyncio
async def test_missing_player_stat_skipped(db_path):
    ev_id = await _insert_bt_event(db_path)
    await _insert_game_result(db_path)
    # No player_stats row inserted!
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 0
    assert report.skipped_no_player_stat == 1
    result, actual = await _fetch_event(db_path, ev_id)
    assert result is None
    assert actual is None


@pytest.mark.asyncio
async def test_idempotent(db_path):
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Over")
    await _insert_player_stat(db_path, stat_type="strikeouts", stat_value=9)
    await _insert_game_result(db_path)
    r1 = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert r1.resolved == 1
    r2 = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    # Second pass — nothing new to resolve (already filled).
    assert r2.scanned == 0
    assert r2.resolved == 0
    # Row unchanged.
    result, actual = await _fetch_event(db_path, ev_id)
    assert result == "won"
    assert actual == 9.0


@pytest.mark.asyncio
async def test_fuzzy_name_match_resolves(db_path):
    """S. Ohtani (odds-API style) must match Shohei Ohtani (stats canonical)."""
    # Prop name is abbreviated.
    ev_id = await _insert_bt_event(db_path, player="S. Ohtani", line=7.5)
    # Stats row uses the canonical full name.
    await _insert_player_stat(
        db_path, player_name="Shohei Ohtani", stat_type="strikeouts",
        stat_value=9,
    )
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 1
    result, _ = await _fetch_event(db_path, ev_id)
    assert result == "won"


@pytest.mark.asyncio
async def test_statcast_fallback_stat_type(db_path):
    """MLB writes statcast_strikeouts today, not strikeouts — fallback must hit."""
    ev_id = await _insert_bt_event(db_path, line=7.5, side="Over")
    await _insert_player_stat(
        db_path, stat_type="statcast_strikeouts", stat_value=10,
    )
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 1
    result, actual = await _fetch_event(db_path, ev_id)
    assert result == "won"
    assert actual == 10.0


@pytest.mark.asyncio
async def test_non_prop_markets_ignored(db_path):
    """h2h/spreads/totals rows must never be touched by the prop resolver."""
    h2h_id = await _insert_bt_event(db_path, market="h2h", player=None)
    tot_id = await _insert_bt_event(db_path, market="totals", player=None)
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.scanned == 0
    assert report.resolved == 0
    for ev_id in (h2h_id, tot_id):
        result, _ = await _fetch_event(db_path, ev_id)
        assert result is None


@pytest.mark.asyncio
async def test_unknown_market_reported(db_path):
    ev_id = await _insert_bt_event(db_path, market="player_quantum_flux")
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.skipped_unknown_market == 1
    result, _ = await _fetch_event(db_path, ev_id)
    assert result is None


@pytest.mark.asyncio
async def test_missing_line_skipped(db_path):
    ev_id = await _insert_bt_event(db_path, line=None)
    await _insert_game_result(db_path)
    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.skipped_no_line == 1
    result, _ = await _fetch_event(db_path, ev_id)
    assert result is None


@pytest.mark.asyncio
async def test_name_index_seed_from_player_stats(db_path):
    await _insert_player_stat(db_path, player_name="LeBron James", stat_type="points", stat_value=32)
    async with aiosqlite.connect(db_path) as db:
        idx = PlayerNameIndex(db)
        await idx.ensure_schema()
        n = await idx.seed_from_player_stats()
        assert n >= 1
        # Fuzzy resolve "L. James"
        match = await idx.resolve("baseball_mlb", "L. James", threshold=0.90)
        assert match is not None
        canonical, score = match
        assert canonical == "LeBron James"
        assert score >= 0.90


@pytest.mark.asyncio
async def test_report_by_sport_breakdown(db_path):
    # Two sports, one resolvable each.
    await _insert_bt_event(db_path, sport="baseball_mlb", market="pitcher_strikeouts",
                           player="Shohei Ohtani", line=7.5, side="Over")
    await _insert_player_stat(db_path, sport="baseball_mlb",
                              player_name="Shohei Ohtani",
                              stat_type="strikeouts", stat_value=9)
    await _insert_bt_event(db_path, sport="basketball_nba", market="player_points",
                           player="LeBron James", line=25.5, side="Over",
                           event_id="E2", game_date="2026-04-10")
    await _insert_player_stat(db_path, sport="basketball_nba", event_id="E2",
                              player_name="LeBron James", stat_type="points",
                              stat_value=32)
    await _insert_game_result(db_path, sport="baseball_mlb")
    await _insert_game_result(db_path, sport="basketball_nba",
                              home_team="LAL", away_team="BOS",
                              home_score=112, away_score=108, total_score=220)

    report = await resolve_player_prop_backtest_events(
        db_path=db_path, recalibrate=False,
    )
    assert report.resolved == 2
    assert "baseball_mlb" in report.by_sport
    assert "basketball_nba" in report.by_sport
    assert report.by_sport["baseball_mlb"]["resolved"] == 1
    assert report.by_sport["basketball_nba"]["resolved"] == 1
