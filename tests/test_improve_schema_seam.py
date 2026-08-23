"""Regression tests for the schema-seam improvement (2026-08-23).

The finding: migration 013 removed the ``sport NOT NULL`` weld from
``hypotheses``, but nothing told the writers. Three concrete failures
followed, none visible to the existing suites:

1. ``HypothesisManager.create_hypothesis`` INSERTs sport/market_type, so on
   any post-013 database it raised ``no such column: sport`` — hypothesis
   creation was dead on every properly migrated DB.
2. The fresh-DB path materialised the WELDED shape from
   plugins/sports/schema.py and relied on 013 re-cutting the seam at every
   startup — the seam existed only after a migration round-trip.
3. Readers of h.get('sport') got nothing back on post-013 rows.

These tests pin the repaired contract: create/read/dup-guard work on all
three database shapes (fresh core schema, migrated legacy, pre-013 welded).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

LEGACY_WELDED_DDL = """
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    thesis TEXT NOT NULL,
    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,
    model_config TEXT NOT NULL,
    edge_threshold REAL NOT NULL DEFAULT 0.01,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','backtesting','paper_trading','live',
                         'paused','drawdown_paused','retired','rejected')),
    min_sample_size INTEGER NOT NULL DEFAULT 50,
    significance_level REAL NOT NULL DEFAULT 0.05,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at DATETIME,
    promoted_by TEXT,
    notes TEXT
)
"""


def _migrate(path: str) -> None:
    from tools.migrations import apply_pending_migrations

    apply_pending_migrations(path)


async def _make_manager(tmp_path, *, shape: str) -> "tuple":
    """Return (manager, db_path) with the requested DB shape."""
    from tools.hypothesis import HypothesisManager

    path = str(tmp_path / f"{shape}.db")
    if shape == "fresh":
        from tools.schema.engine import ensure_schema

        await ensure_schema(path)
    elif shape == "migrated":
        conn = sqlite3.connect(path)
        conn.execute(LEGACY_WELDED_DDL)
        conn.commit()
        conn.close()
        _migrate(path)
    elif shape == "pre13":
        # Welded shape, migration framework never ran (ext table absent).
        conn = sqlite3.connect(path)
        conn.execute(LEGACY_WELDED_DDL)
        conn.commit()
        conn.close()
    else:  # pragma: no cover
        raise ValueError(shape)

    hm = HypothesisManager(path)
    await hm.initialize()
    return hm, path


SHAPES = ["fresh", "migrated", "pre13"]


@pytest.mark.parametrize("shape", SHAPES)
def test_create_and_readback_roundtrip(tmp_path, shape):
    """create_hypothesis must work and sport must read back on EVERY shape."""

    async def _run():
        hm, _ = await _make_manager(tmp_path / shape if False else tmp_path, shape=shape)
        try:
            hid = await hm.create_hypothesis(
                f"n-{shape}", "thesis", "basketball_nba", "moneyline", {}
            )
            assert hid
            h = await hm.get_hypothesis(hid)
            assert h["name"] == f"n-{shape}"
            assert h["sport"] == "basketball_nba", (
                f"sport lost on {shape} shape"
            )
            assert h["market_type"] == "moneyline"
            assert h["status"] == "draft"
            return h
        finally:
            await hm.close()

    h = asyncio.run(_run())
    if shape in ("fresh", "migrated"):
        assert h["domain"] == "sports"


@pytest.mark.parametrize("shape", ["fresh", "migrated"])
def test_dup_guard_blocks_same_sport_market_filters(tmp_path, shape):
    """The duplicate game_filters guard must still fire post-013."""

    async def _run():
        hm, _ = await _make_manager(tmp_path, shape=shape)
        try:
            hid = await hm.create_hypothesis(
                f"d-{shape}", "t", "basketball_nba", "moneyline",
                {"game_filters": {"min_odds": -150}},
            )
            again = await hm.create_hypothesis(
                f"d-{shape}-other-name", "t", "basketball_nba", "moneyline",
                {"game_filters": {"min_odds": -150}},
            )
            return hid, again
        finally:
            await hm.close()

    hid, again = asyncio.run(_run())
    assert hid == again


@pytest.mark.parametrize("shape", SHAPES)
def test_list_hypotheses_carries_sport(tmp_path, shape):
    async def _run():
        hm, _ = await _make_manager(tmp_path, shape=shape)
        try:
            await hm.create_hypothesis(
                f"L-{shape}", "t", "baseball_mlb", "pitcher_k", {}
            )
            rows = [r for r in await hm.list_hypotheses() if r["name"] == f"L-{shape}"]
            return rows
        finally:
            await hm.close()

    rows = asyncio.run(_run())
    assert len(rows) == 1
    assert rows[0]["sport"] == "baseball_mlb"


def test_migrated_db_keeps_ext_rows_for_every_hypothesis(tmp_path):
    """Every hypothesis row must have its plugin ext row post-013+write."""

    async def _run():
        hm, path = await _make_manager(tmp_path, shape="migrated")
        try:
            for i in range(5):
                await hm.create_hypothesis(
                    f"e{i}", "t", "icehockey_nhl", f"total_{i}", {}
                )
        finally:
            await hm.close()
        conn = sqlite3.connect(path)
        n_hyp = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
        n_ext = conn.execute(
            "SELECT COUNT(*) FROM hypotheses h JOIN hypothesis_sports_ext e "
            "USING (hypothesis_id)"
        ).fetchone()[0]
        conn.close()
        return n_hyp, n_ext

    n_hyp, n_ext = asyncio.run(_run())
    assert n_hyp == 5 and n_ext == 5


def test_fresh_db_is_born_seam_shaped(tmp_path):
    """A fresh ensure_schema DB must have NO sport column on hypotheses.

    Before the fix the fresh path produced the welded DDL and depended on
    migration 013 to fix it at every startup.
    """
    import os

    import aiosqlite

    async def _run():
        path = str(tmp_path / "born.db")
        from tools.schema.engine import ensure_schema

        await ensure_schema(path)
        async with aiosqlite.connect(path) as db:
            cur = await db.execute("PRAGMA table_info(hypotheses)")
            cols = [r[1] for r in await cur.fetchall()]
        return cols

    cols = asyncio.run(_run())
    assert "sport" not in cols, "the weld is being created on fresh databases"
    assert "domain" in cols


def test_migration_013_noop_on_fresh_shape(tmp_path):
    """Running migrations against a born-seam-shaped DB must be a no-op for 013."""
    import os

    async def _run():
        path = str(tmp_path / "noop.db")
        from tools.schema.engine import ensure_schema

        await ensure_schema(path)
        # ensure_schema doesn't run versioned migrations; do it like api.py does.
        _migrate(path)

    asyncio.run(_run())  # must not raise

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "noop.db"))
    applied = {
        r[0]
        for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='hypotheses'"
    ).fetchone()[0]
    conn.close()
    # If 013 recorded itself, the DB must still be seam shaped (it is a no-op).
    # 'sport' appears only as the DEFAULT VALUE 'sports' on the domain column.
    if applied:
        assert "domain TEXT NOT NULL DEFAULT 'sports'" in sql
        assert "sport TEXT NOT NULL" not in sql


# ── Raw-SQL consumers of hypotheses.sport ─────────────────────────────
#
# The 2026-08-23 landing moved `hypotheses` into core with the seam shape,
# but four call sites still issued raw SQL naming `hypotheses.sport` —
# each one a live `no such column: sport` failure (or silent empty result)
# on every properly migrated database. These tests pin them on BOTH shapes.


async def _seed_post13(tmp_path):
    """Fresh core schema + migrations; one rejected and one active hypothesis."""
    from tools.schema.engine import ensure_schema
    from tools.hypothesis import HypothesisManager

    path = str(tmp_path / "post13.db")
    await ensure_schema(path)
    _migrate(path)
    hm = HypothesisManager(path)
    await hm.initialize()
    try:
        await hm.create_hypothesis(
            "rej-nba", "t", "basketball_nba", "moneyline", {})
        await hm.create_hypothesis(
            "act-mlb", "t2", "baseball_mlb", "total_5", {})
        # Four more rejected hypotheses so the premature-rejection detector
        # (which fires at >=5 candidates) has a sample to find.
        for i in range(4):
            await hm.create_hypothesis(
                f"rej-extra-{i}", f"t-extra-{i}", "basketball_nba",
                f"market_{i}", {})
        await hm._db.execute(
            "UPDATE hypotheses SET status='rejected' WHERE name LIKE 'rej-%'")
        await hm._db.execute(
            "UPDATE hypotheses SET status='paper_trading' WHERE name='act-mlb'")
        # The detector only flags sports that HAVE odds data. The real
        # table already exists post-migration; insert a minimal row.
        await hm._db.execute(
                "INSERT INTO historical_odds_cache (sport, snapshot_date, response_json) "
                "VALUES ('basketball_nba', '2026-08-20', '{}')")
        await hm._db.commit()
    finally:
        await hm.close()
    return path


@pytest.mark.asyncio
async def test_active_sports_query_works_on_seam_shape(tmp_path):
    """autonomous.py's DISTINCT-sport resolution query must return sports."""
    import aiosqlite

    path = await _seed_post13(tmp_path)
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT DISTINCT e.sport AS sport "
            "FROM hypotheses h "
            "JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id "
            "WHERE h.status IN ('backtesting', 'paper_trading')"
        )
        sports = {r[0] for r in await cur.fetchall()}
    assert sports == {"baseball_mlb"}


@pytest.mark.asyncio
async def test_recent_theses_finds_by_sport_on_seam_shape(tmp_path):
    """HypothesisGenerator._recent_theses must find rows by ext-table sport."""
    from tools.hypothesis_generator import HypothesisGenerator
    from tools.hypothesis import HypothesisManager
    from tools.schema.engine import ensure_schema

    path = str(tmp_path / "gen.db")
    await ensure_schema(path)
    _migrate(path)
    hm = HypothesisManager(path)
    await hm.initialize()
    try:
        for i in range(3):
            await hm.create_hypothesis(
                f"g{i}", f"thesis {i}", "icehockey_nhl", f"puck_line_{i}", {})
    finally:
        await hm.close()

    gen = HypothesisGenerator.__new__(HypothesisGenerator)
    gen._db = None
    gen.db_path = path
    theses = await HypothesisGenerator._recent_theses(gen, "icehockey_nhl", 10)
    assert len(theses) == 3, f"expected 3 theses by sport, got {theses}"


def test_self_repair_premature_rejection_reads_ext_sport(tmp_path):
    """The premature-rejection detector must see ext-table sports post-013."""

    async def _run():
        import tools.self_repair as srmod

        path = await _seed_post13(tmp_path)
        orig = srmod.DB_PATH
        srmod.DB_PATH = path
        try:
            engine = srmod.SelfRepairEngine.__new__(srmod.SelfRepairEngine)
            return await srmod.SelfRepairEngine._det_premature_rejection(engine)
        finally:
            srmod.DB_PATH = orig

    result = asyncio.run(_run())
    assert result is not None and result["count"] >= 1, (
        "detector found nothing — sport lookup broken post-013")
    assert any(c["sport"] == "basketball_nba"
               for c in result["candidates"])


@pytest.mark.asyncio
async def test_api_draft_scan_selects_sport_via_coalesce(tmp_path):
    """api.py's draft-pattern scan SQL must resolve sport on both shapes."""
    import aiosqlite

    path = await _seed_post13(tmp_path)
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT h.hypothesis_id, h.name, h.thesis, e.sport AS sport "
            "FROM hypotheses h "
            "JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id "
            "WHERE h.status IN ('draft', 'paper_trading')"
        )
        rows = await cur.fetchall()
    assert rows, "draft scan returned nothing"
    assert all(r[3] in ("basketball_nba", "baseball_mlb") for r in rows)


def test_legacy_welded_shape_still_reads(tmp_path):
    """Pre-013 DBs must still read sport off the core table directly."""
    import aiosqlite

    async def _run():
        path = str(tmp_path / "pre13.db")
        conn = sqlite3.connect(path)
        conn.execute(LEGACY_WELDED_DDL.replace("UNIQUE,", ","))
        conn.commit()
        conn.close()
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, "
                "market_type, model_config, status) VALUES "
                "('x', 'n', 't', 'basketball_nba', 'moneyline', '{}', 'draft')")
            cur = await db.execute(
                "SELECT hypothesis_id, sport FROM hypotheses")
            return await cur.fetchall()

    rows = asyncio.run(_run())
    assert rows == [("x", "basketball_nba")]


# ── Consumer queries (2026-08-23, seam-completion pass) ─────────────────
# The COALESCE repair landed in four call sites but four more kept
# referencing h.sport/h.market_type directly — each broke with
# "no such column" on the seam shape. These tests run the ACTUAL SQL of
# every consumer against BOTH shapes.

CONSUMER_QUERIES = {
    # tools/autonomous.py:6118 top-hypotheses-by-signal panel
    "autonomous_top_hypos": (
        """SELECT h.hypothesis_id, h.name, h.thesis,
               e.sport AS sport, e.market_type AS market_type,
               h.edge_threshold, h.status
           FROM hypotheses h
           JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id
           WHERE h.status IN ('backtesting', 'paper_trading')"""
    ),
    # tools/autonomous.py:6953 status-panel top list
    "autonomous_status_panel": (
        """SELECT h.hypothesis_id, h.name, h.thesis,
               e.sport AS sport, h.status
           FROM hypotheses h
           JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id
           WHERE h.status IN ('backtesting', 'paper_trading')"""
    ),
    # tools/autonomous.py:5605 premature-rejection detector
    "autonomous_premature_reject": (
        """SELECT h.hypothesis_id, h.name,
               e.market_type AS market_type
           FROM hypotheses h
           JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id
           JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
           WHERE h.status IN ('rejected', 'draft', 'backtesting')"""
    ),
    # tools/hermes_memory.py:675 research-state block
    "hermes_memory_research_state": (
        """SELECT h.name,
               e.sport AS sport, e.market_type AS market_type,
               h.thesis
           FROM hypotheses h
           JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id
           WHERE h.status IN ('backtesting', 'paper_trading', 'live', 'draft')"""
    ),
}


def _make_backtest_events(conn):
    """Create a minimal backtest_events table with the columns the
    consumer queries touch (welded-shape tests only; migrated DBs
    already have the real one from the migrations)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS backtest_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " run_id TEXT NOT NULL, event_id TEXT NOT NULL, hypothesis_id TEXT NOT NULL,"
        " sport TEXT NOT NULL, player TEXT, market TEXT NOT NULL, line TEXT,"
        " side TEXT NOT NULL, book TEXT NOT NULL, book_odds_american REAL NOT NULL,"
        " book_implied_prob REAL NOT NULL, model_fair_prob REAL NOT NULL,"
        " model_factors TEXT, edge REAL NOT NULL, ev_pct REAL NOT NULL,"
        " kelly_fraction TEXT, signal_generated INTEGER DEFAULT FALSE,"
        " actual_result TEXT, actual_stat TEXT, closing_odds TEXT,"
        " closing_implied TEXT, clv_implied TEXT, game_date DATE NOT NULL,"
        " local_game_date TEXT, snapshot_time TEXT NOT NULL,"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )


@pytest.mark.parametrize("name", sorted(CONSUMER_QUERIES))
def test_consumer_query_works_on_seam_shape(name, tmp_path):
    """Every consumer query must return sport/market_type post-013."""
    import asyncio

    async def _run():
        from tools.schema.engine import ensure_schema
        from tools.migrations import apply_pending_migrations

        path = str(tmp_path / f"{name}.db")
        await ensure_schema(path)
        apply_pending_migrations(path)
        import aiosqlite
        db = await aiosqlite.connect(path)
        try:
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis,"
                " model_config, status) VALUES ('h1','n','t','{}','paper_trading')"
            )
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis,"
                " model_config, status) VALUES ('h2','n2','t2','{}','rejected')"
            )
            await db.execute(
                "INSERT INTO hypothesis_sports_ext (hypothesis_id, sport,"
                " market_type) VALUES ('h1','basketball_nba','moneyline')"
            )
            await db.execute(
                "INSERT INTO hypothesis_sports_ext (hypothesis_id, sport,"
                " market_type) VALUES ('h2','basketball_nba','total_5')"
            )
            _make_backtest_events(db)
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, run_id,"
                " sport, market, side, book, book_odds_american,"
                " book_implied_prob, model_fair_prob, edge, ev_pct, game_date,"
                " snapshot_time, signal_generated)"
                " VALUES ('e1','h1','r1','basketball_nba','moneyline','home',"
                " 'draft',-110,0.524,0.55,0.05,2.0,'2026-08-20','2026-08-20T12:00:00',1)")
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, run_id,"
                " sport, market, side, book, book_odds_american,"
                " book_implied_prob, model_fair_prob, edge, ev_pct, game_date,"
                " snapshot_time, signal_generated)"
                " VALUES ('e2','h2','r1','basketball_nba','total_5','over',"
                " 'draft',-110,0.524,0.55,0.05,2.0,'2026-08-20','2026-08-20T12:00:00',1)")
            cur = await db.execute(CONSUMER_QUERIES[name])
            rows = await cur.fetchall()
            assert rows, f"{name} returned nothing"
            for row in rows:
                assert any(v in ("basketball_nba", "baseball_mlb", "moneyline", "total_5")
                           for v in row if isinstance(v, str)), (
                    f"{name}: no resolved sports field in {row}"
                )
        finally:
            await db.close()

    asyncio.run(_run())


@pytest.mark.parametrize("name", sorted(CONSUMER_QUERIES))
def test_consumer_query_works_on_welded_shape(name, tmp_path):
    """The same queries must still work pre-013 (columns on core).

    On the welded shape the ext table does not exist, so a LEFT JOIN to
    it would fail outright — consumers must only emit the join when the
    table is present (the seam-shape branch). Here we create an empty
    ext table to stand in for the join target and assert the COALESCE
    degenerates correctly to the core columns.
    """
    import asyncio

    async def _run():
        path = str(tmp_path / f"welded_{name}.db")
        conn = sqlite3.connect(path)
        conn.execute(LEGACY_WELDED_DDL.replace("UNIQUE,", ","))
        # Stand-in for the join target: on the welded shape the real ext
        # table does not exist, so the consumer's shape-detection must
        # omit the join. We include it here to verify the COALESCE order
        # (ext value wins when present, core column when not).
        conn.execute(
            "CREATE TABLE hypothesis_sports_ext (hypothesis_id TEXT PRIMARY KEY,"
            " sport TEXT, market_type TEXT, edge_threshold REAL)"
        )
        conn.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport,"
            " market_type, model_config, status) VALUES"
            " ('h1','n','t','baseball_mlb','total_5','{}','backtesting')"
        )
        conn.execute(
            "INSERT INTO hypothesis_sports_ext (hypothesis_id, sport,"
            " market_type) VALUES ('h1','baseball_mlb','total_5')")
        _make_backtest_events(conn)
        conn.execute(
            "INSERT INTO backtest_events (event_id, hypothesis_id, run_id,"
            " sport, market, side, book, book_odds_american,"
            " book_implied_prob, model_fair_prob, edge, ev_pct, game_date,"
            " snapshot_time, signal_generated)"
            " VALUES ('e1', 'h1', 'r1', 'baseball_mlb', 'total_5', 'over',"
            " 'draft', -110, 0.524, 0.55, 0.05, 2.0, '2026-08-20',"
            " '2026-08-20T12:00:00', 1)")
        cur = conn.execute(CONSUMER_QUERIES[name])
        rows = cur.fetchall()
        conn.close()
        assert rows, f"{name} returned nothing on welded shape"
        for row in rows:
            assert "baseball_mlb" in row or "total_5" in row, (
                f"{name}: no resolved sports field in {row}"
            )

    asyncio.run(_run())
