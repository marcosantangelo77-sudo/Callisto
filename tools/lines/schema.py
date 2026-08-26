"""tools.lines.schema — DDL bootstrap for the line monitor's odds tables.

Extracted from tools/line_monitor.py (slice 4) so LineMonitor.initialize()
stays a thin call. Per-statement DDL avoids EXCLUSIVE lock contention
(security audit C-6).
"""

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        game_count INTEGER DEFAULT 0,
        credits_remaining INTEGER,
        -- fetched_at records our ingest time (not the book's
        -- last_update). Used by edge_scanner.weighted_sharp_consensus
        -- to decay stale lines. See schema.py migration for details.
        fetched_at TEXT,
        -- source tracks origin: 'interval' (default 15-min poll),
        -- 'ws' (WebSocket push), 'incremental' (/odds/updated),
        -- 'scraper_fallback'. Useful for debugging freshness tiers.
        source TEXT DEFAULT 'interval'
    )""",
    """CREATE TABLE IF NOT EXISTS line_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sport TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        team TEXT,
        market TEXT,
        bookmaker TEXT,
        old_price INTEGER,
        new_price INTEGER,
        price_movement INTEGER,
        old_point REAL,
        new_point REAL,
        point_movement REAL,
        direction TEXT,
        ev_analysis TEXT
    )""",
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
        -- source distinguishes signal provenance: 'line_movement' (default,
        -- from line_monitor edge scan), 'odds_api_io_pro' (value bets from
        -- the provider's pre-computed +EV feed), or 'arbitrage' (cross-book
        -- guaranteed-profit opportunities). Added 2026-04-18 to unify the
        -- two writer paths (line_monitor + autonomous) on one schema.
        source TEXT DEFAULT 'line_movement'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_sport_ts ON odds_snapshots(sport, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_movements_sport ON line_movements(sport, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_ev_status ON ev_opportunities(status, detected_at)",
)


async def connect_and_tag(db_path: str):
    """Open an aiosqlite connection and tag it for WriteCoordinator routing."""
    import aiosqlite
    from tools.db_writer import tag_connection as _tag

    db = await aiosqlite.connect(db_path)
    _tag(db, db_path)
    await db.execute("PRAGMA busy_timeout = 120000")  # 2 min — 5 min caused cascading WAL stalls
    return db


async def ensure_line_schema(db) -> None:
    """Create the odds snapshot / movement / EV tables if missing."""
    for stmt in SCHEMA_STATEMENTS:
        await db.execute(stmt)
    await db.commit()
