"""
Database schema for Callisto — normalized odds storage, CLV tracking, boosts.

Run ensure_schema() at startup to create/upgrade all tables.
Backward-compatible: existing tables are preserved, new columns added safely.
"""

import logging
import os

import aiosqlite
from dotenv import load_dotenv

load_dotenv()


async def open_db(db_path: str = None) -> aiosqlite.Connection:
    """Open a DB connection with WAL mode and busy_timeout.

    Use this instead of raw aiosqlite.connect() everywhere to avoid
    "database is locked" errors from concurrent async writers.
    """
    if db_path is None:
        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA busy_timeout = 10000")
    return db

logger = logging.getLogger("callisto.schema")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ──────────────────────────────────────────────────────────────
# Regime classification — maps (sport, date) → regime name.
# Backtests use this to filter data by rule era.
# ──────────────────────────────────────────────────────────────
REGIME_BOUNDARIES: dict[str, list[tuple[str, str, str | None]]] = {
    # (regime_name, start_date, end_date_or_None)
    "baseball_mlb": [
        ("mlb_pre_pitch_clock", "2020-01-01", "2023-03-29"),
        ("mlb_post_pitch_clock", "2023-03-30", None),
    ],
    "americanfootball_nfl": [
        ("nfl_pre_new_kickoff", "2020-01-01", "2024-09-04"),
        ("nfl_post_new_kickoff", "2024-09-05", None),
    ],
    "basketball_nba": [
        ("nba_pre_cup", "2020-01-01", "2023-10-23"),
        ("nba_cup_era", "2023-10-24", None),
    ],
    "icehockey_nhl": [
        ("nhl_modern", "2020-01-01", None),
    ],
    "basketball_ncaab": [
        ("ncaab_modern", "2020-01-01", None),
    ],
    "basketball_ncaaw": [
        ("ncaaw_quarter_era", "2015-01-01", None),
    ],
}


def classify_regime(sport: str, game_date: str) -> str:
    """Return the regime name for a (sport, date) pair."""
    boundaries = REGIME_BOUNDARIES.get(sport, [])
    for regime_name, start, end in reversed(boundaries):
        if game_date >= start and (end is None or game_date <= end):
            return regime_name
    return "unknown"


SCHEMA_SQL = """
-- ──────────────────────────────────────────
-- BOOK REGISTRY: categorize by tier
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS books (
    book_id TEXT PRIMARY KEY,
    book_name TEXT NOT NULL,
    tier TEXT NOT NULL CHECK(tier IN ('sharp', 'retail', 'reference'))
);

-- Pre-populate known books
INSERT OR IGNORE INTO books VALUES ('draftkings', 'DraftKings', 'retail');
INSERT OR IGNORE INTO books VALUES ('fanduel', 'FanDuel', 'retail');
INSERT OR IGNORE INTO books VALUES ('fanatics', 'Fanatics', 'retail');
INSERT OR IGNORE INTO books VALUES ('betmgm', 'BetMGM', 'retail');
INSERT OR IGNORE INTO books VALUES ('pinnacle', 'Pinnacle', 'reference');
INSERT OR IGNORE INTO books VALUES ('betonlineag', 'BetOnline', 'sharp');
INSERT OR IGNORE INTO books VALUES ('bookmaker', 'Bookmaker', 'sharp');
INSERT OR IGNORE INTO books VALUES ('circa', 'Circa', 'sharp');
INSERT OR IGNORE INTO books VALUES ('lowvig', 'LowVig.ag', 'sharp');

-- ──────────────────────────────────────────
-- MARKETS: normalized event + market type
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    event_id TEXT,
    event_name TEXT NOT NULL,
    home_team TEXT,
    away_team TEXT,
    commence_time DATETIME NOT NULL,
    market_type TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_markets_sport_time ON markets(sport, commence_time);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets(event_id);

-- ──────────────────────────────────────────
-- ODDS SNAPSHOTS: normalized by market+book+outcome
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS odds_snapshots_v2 (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    price_american INTEGER NOT NULL,
    price_decimal REAL NOT NULL,
    point REAL,
    snapshot_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (market_id) REFERENCES markets(market_id),
    FOREIGN KEY (book_id) REFERENCES books(book_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_v2_lookup
    ON odds_snapshots_v2(market_id, book_id, outcome_name, snapshot_time);

-- ──────────────────────────────────────────
-- CLOSING LINES: last price change before game start
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS closing_lines_v2 (
    market_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    point REAL,
    closing_price_american INTEGER NOT NULL,
    closing_price_decimal REAL NOT NULL,
    is_last_change BOOLEAN DEFAULT FALSE,
    recorded_at DATETIME NOT NULL,
    reliable BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (market_id, book_id, outcome_name)
);

-- ──────────────────────────────────────────
-- CLV LOG: comprehensive bet tracking
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clv_log (
    bet_id TEXT PRIMARY KEY,
    event TEXT,
    outcome TEXT,
    point REAL,
    book TEXT,
    our_odds_decimal REAL,
    pinnacle_close_fair_prob REAL,
    pinnacle_close_fair_decimal REAL,
    clv_cents REAL,
    actual_result TEXT,
    actual_pnl REAL,
    close_reliable BOOLEAN DEFAULT TRUE,
    logged_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_clv_log_result ON clv_log(actual_result, logged_at);

-- ──────────────────────────────────────────
-- BOOSTS: daily profit boost tracking
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS boosts (
    boost_id TEXT PRIMARY KEY,
    book TEXT NOT NULL,
    description TEXT NOT NULL,
    boost_type TEXT NOT NULL,
    boosted_odds_american INTEGER,
    original_odds_american INTEGER,
    max_stake REAL,
    sport TEXT,
    date TEXT,
    evaluated BOOLEAN DEFAULT FALSE,
    ev_percent REAL,
    fair_probability REAL,
    recommendation TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_boosts_date ON boosts(date, book);

-- ──────────────────────────────────────────
-- SENTINEL FLAGS: validation warnings
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentinel_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_type TEXT NOT NULL,
    description TEXT NOT NULL,
    sport TEXT,
    market TEXT,
    severity TEXT DEFAULT 'warning',
    resolved BOOLEAN DEFAULT 0,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

-- ──────────────────────────────────────────
-- SIMULATION CACHE: store sim results for reuse
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sim_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    sim_type TEXT NOT NULL,
    parameters_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    n_sims INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(event_id, sim_type, parameters_hash)
);

CREATE INDEX IF NOT EXISTS idx_sim_cache_event ON sim_cache(event_id, sim_type);

-- ──────────────────────────────────────────
-- SIGNALS: actionable bet recommendations
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,
    sport TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    team TEXT,
    market TEXT NOT NULL,
    book TEXT NOT NULL,
    odds_american INTEGER NOT NULL,
    fair_probability REAL NOT NULL,
    fair_prob_source TEXT NOT NULL,
    edge_pct REAL NOT NULL,
    ev_pct REAL NOT NULL,
    confidence TEXT NOT NULL,
    kelly_fraction REAL,
    recommended_stake REAL,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    acted_on BOOLEAN DEFAULT FALSE,
    bet_id INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_signals_sport ON signals(sport, created_at);

-- ──────────────────────────────────────────
-- HYPOTHESES: testable betting theses
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    thesis TEXT NOT NULL,
    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,
    model_config TEXT NOT NULL,
    edge_threshold REAL NOT NULL DEFAULT 0.01,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','backtesting','paper_trading','live','retired','rejected')),
    min_sample_size INTEGER NOT NULL DEFAULT 50,
    significance_level REAL NOT NULL DEFAULT 0.05,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at DATETIME,
    promoted_by TEXT,
    notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name ON hypotheses(name);

-- ──────────────────────────────────────────
-- BACKTEST RUNS: metadata per execution
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    date_range_start DATE NOT NULL,
    date_range_end DATE NOT NULL,
    total_events INTEGER DEFAULT 0,
    signals_generated INTEGER DEFAULT 0,
    actual_win INTEGER DEFAULT 0,
    actual_loss INTEGER DEFAULT 0,
    actual_push INTEGER DEFAULT 0,
    unresolved INTEGER DEFAULT 0,
    hit_rate REAL,
    avg_edge REAL,
    avg_ev REAL,
    avg_clv REAL,
    roi_pct REAL,
    p_value_binomial REAL,
    p_value_ttest REAL,
    z_score REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    kelly_growth REAL,
    is_significant BOOLEAN DEFAULT FALSE,
    run_config TEXT,
    started_at DATETIME NOT NULL,
    completed_at DATETIME,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_bt_runs_hypo ON backtest_runs(hypothesis_id, completed_at);

-- ──────────────────────────────────────────
-- BACKTEST EVENTS: individual predictions
-- ──────────────────────────────────────────
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
    snapshot_time DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id),
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_bt_events_run ON backtest_events(run_id, game_date);
CREATE INDEX IF NOT EXISTS idx_bt_events_signal ON backtest_events(hypothesis_id, signal_generated, actual_result);

-- ──────────────────────────────────────────
-- PAPER TRADES: forward-test without wagering
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    event_id TEXT,
    sport TEXT NOT NULL,
    player TEXT,
    market TEXT NOT NULL,
    line REAL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    signal_time DATETIME NOT NULL,
    signal_odds_american INTEGER NOT NULL,
    signal_implied_prob REAL NOT NULL,
    model_fair_prob REAL NOT NULL,
    edge REAL NOT NULL,
    ev_pct REAL NOT NULL,
    kelly_fraction REAL,
    recommended_stake REAL,
    closing_odds INTEGER,
    closing_implied REAL,
    clv_implied REAL,
    actual_result TEXT,
    actual_stat REAL,
    hypothetical_pnl REAL,
    game_date DATE NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_hypo ON paper_trades(hypothesis_id, game_date);
CREATE INDEX IF NOT EXISTS idx_paper_result ON paper_trades(hypothesis_id, actual_result);

-- ──────────────────────────────────────────
-- HYPOTHESIS STATS: rolling aggregates
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hypothesis_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    computed_at DATETIME NOT NULL,
    total_n INTEGER NOT NULL,
    signals_n INTEGER NOT NULL,
    win INTEGER DEFAULT 0,
    loss INTEGER DEFAULT 0,
    push_ INTEGER DEFAULT 0,
    hit_rate REAL,
    avg_edge REAL,
    avg_ev REAL,
    avg_clv REAL,
    positive_clv_rate REAL,
    roi_pct REAL,
    sharpe REAL,
    max_drawdown REAL,
    p_value REAL,
    is_significant BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

-- ──────────────────────────────────────────
-- HISTORICAL ODDS CACHE: avoid re-fetching
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS historical_odds_cache (
    cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    event_id TEXT,
    market_type TEXT,
    response_json TEXT NOT NULL,
    credits_cost INTEGER DEFAULT 1,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, snapshot_date, event_id, market_type)
);

CREATE INDEX IF NOT EXISTS idx_hist_cache_lookup
    ON historical_odds_cache(sport, snapshot_date, event_id);

-- ──────────────────────────────────────────
-- EMBEDDINGS: semantic vector store
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_text TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    metadata_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(collection, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_embeddings_collection ON embeddings(collection);

-- ──────────────────────────────────────────
-- GAME CONTEXTS: structured game data for hypothesis generation
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS game_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    game_date DATE NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    context_json TEXT NOT NULL,
    embedded BOOLEAN DEFAULT FALSE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, event_id)
);

CREATE INDEX IF NOT EXISTS idx_game_ctx_sport_date ON game_contexts(sport, game_date);

-- ──────────────────────────────────────────
-- PLAYER STATS: post-game stats for prop resolution
-- ──────────────────────────────────────────
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

CREATE INDEX IF NOT EXISTS idx_player_stats_lookup
    ON player_stats(sport, player_name, stat_type, game_date);

-- ──────────────────────────────────────────
-- GAME RESULTS: actual scores for backtest resolution
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS game_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    game_date TEXT NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_game_results_lookup
    ON game_results(sport, game_date, home_team, away_team);

-- ──────────────────────────────────────────
-- REGIME TAGGING: rule-change era metadata
-- ──────────────────────────────────────────
-- Regimes encode major rule changes that invalidate historical patterns.
-- Backtests should default to the current regime unless explicitly cross-era.
--
-- Key regime boundaries:
--   MLB:  2023-04-01  pitch clock, shift ban, larger bases
--   NFL:  2024-09-05  XFL-style kickoff rules
--   NBA:  2023-10-24  in-season tournament (NBA Cup) introduced
--   NHL:  (no major recent breaks — stable since 2021)
--
CREATE TABLE IF NOT EXISTS regime_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    regime_name TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    description TEXT NOT NULL,
    rule_changes TEXT,
    UNIQUE(sport, regime_name)
);

INSERT OR IGNORE INTO regime_rules (sport, regime_name, start_date, end_date, description, rule_changes) VALUES
    ('baseball_mlb', 'mlb_pre_pitch_clock', '2020-01-01', '2023-03-29', 'Pre pitch clock era', 'No pitch clock, shifts allowed, standard bases'),
    ('baseball_mlb', 'mlb_post_pitch_clock', '2023-03-30', NULL, 'Post pitch clock era', 'Pitch clock, shift ban, larger bases, disengagement limits'),
    ('americanfootball_nfl', 'nfl_pre_new_kickoff', '2020-01-01', '2024-09-04', 'Traditional kickoff rules', 'Standard NFL kickoff format'),
    ('americanfootball_nfl', 'nfl_post_new_kickoff', '2024-09-05', NULL, 'XFL-style kickoff rules', 'New kickoff formation, fair catch changes'),
    ('basketball_nba', 'nba_pre_cup', '2020-01-01', '2023-10-23', 'Pre NBA Cup era', 'Standard schedule, no in-season tournament'),
    ('basketball_nba', 'nba_cup_era', '2023-10-24', NULL, 'NBA Cup era', 'In-season tournament, schedule adjustments, rest pattern changes'),
    ('icehockey_nhl', 'nhl_modern', '2020-01-01', NULL, 'Modern NHL rules', 'Stable ruleset since 2021'),
    ('basketball_ncaab', 'ncaab_modern', '2020-01-01', NULL, 'Modern NCAA basketball', 'Shot clock at 30s since 2015'),
    ('basketball_ncaaw', 'ncaaw_quarter_era', '2015-01-01', NULL, 'Quarter-based NCAAW', 'Switched from halves to quarters');

CREATE TABLE IF NOT EXISTS research_focus_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    subtopic TEXT,
    reason TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ──────────────────────────────────────────
-- MASTERS HISTORICAL: tournament results 2010+
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS masters_historical (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    position TEXT,
    position_numeric INTEGER,
    r1 INTEGER,
    r2 INTEGER,
    r3 INTEGER,
    r4 INTEGER,
    total INTEGER,
    total_to_par INTEGER,
    cut_made BOOLEAN,
    sg_total REAL,
    sg_putting REAL,
    sg_approach REAL,
    sg_around_green REAL,
    sg_off_tee REAL,
    sg_tee_to_green REAL,
    masters_appearances INTEGER,
    world_ranking INTEGER,
    age INTEGER,
    UNIQUE(year, player)
);

CREATE INDEX IF NOT EXISTS idx_masters_year ON masters_historical(year);
CREATE INDEX IF NOT EXISTS idx_masters_player ON masters_historical(player);
CREATE INDEX IF NOT EXISTS idx_masters_position ON masters_historical(year, position_numeric);

-- ──────────────────────────────────────────
-- PGA SEASON STATS: current form data
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pga_season_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    events_played INTEGER,
    sg_total REAL,
    sg_putting REAL,
    sg_approach REAL,
    sg_around_green REAL,
    sg_off_tee REAL,
    sg_tee_to_green REAL,
    driving_distance REAL,
    driving_accuracy REAL,
    gir_pct REAL,
    scrambling_pct REAL,
    putting_avg REAL,
    par5_scoring_avg REAL,
    par3_scoring_avg REAL,
    world_ranking INTEGER,
    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(year, player)
);

CREATE INDEX IF NOT EXISTS idx_pga_stats_year ON pga_season_stats(year, player);

-- ──────────────────────────────────────────
-- MASTERS FIELD: expected/confirmed entrants
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS masters_field (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    qualification_category TEXT,
    world_ranking INTEGER,
    confirmed BOOLEAN DEFAULT 0,
    UNIQUE(year, player)
);

-- ──────────────────────────────────────────
-- MASTERS BACKTEST RESULTS: LOO/rolling window
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS masters_backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    method TEXT NOT NULL,
    test_year INTEGER NOT NULL,
    train_years TEXT NOT NULL,
    predictions_json TEXT,
    actuals_json TEXT,
    top10_accuracy REAL,
    top10_recall REAL,
    cut_accuracy REAL,
    rank_correlation REAL,
    roi_vs_market REAL,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hypothesis_id, method, test_year)
);

CREATE INDEX IF NOT EXISTS idx_masters_bt_hypo
    ON masters_backtest_results(hypothesis_id, method);

-- ──────────────────────────────────────────
-- PROP SNAPSHOTS: player prop odds over time
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prop_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    home_team TEXT,
    away_team TEXT,
    player TEXT NOT NULL,
    market TEXT NOT NULL,
    line REAL NOT NULL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    price_american INTEGER NOT NULL,
    snapshot_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prop_snap_player
    ON prop_snapshots(player, market, line, book, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_sport_time
    ON prop_snapshots(sport, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_event
    ON prop_snapshots(event_id, market, snapshot_time);

-- ──────────────────────────────────────────
-- MASTERS PREDICTIONS: pre-tournament rankings
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS masters_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    masters_fit_score REAL,
    predicted_rank INTEGER,
    top5_prob REAL,
    top10_prob REAL,
    top20_prob REAL,
    cut_prob REAL,
    win_prob REAL,
    confidence_low INTEGER,
    confidence_high INTEGER,
    key_factors TEXT,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hypothesis_id, year, player)
);
"""


async def ensure_schema(db_path: str = DB_PATH) -> None:
    """Create or upgrade all tables. Safe to call multiple times."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        # Set PRAGMAs before schema creation — these persist for the connection
        await db.execute("PRAGMA busy_timeout = 10000")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.commit()  # commit PRAGMA changes before executescript
        await db.executescript(SCHEMA_SQL)
        await db.commit()

        # Migrations: add regime columns (safe if already exists)
        for tbl in ("historical_odds_cache", "game_results"):
            try:
                await db.execute(f"ALTER TABLE {tbl} ADD COLUMN regime TEXT")
                await db.commit()
                logger.info(f"Added regime column to {tbl}")
            except Exception:
                pass  # Column already exists

        # Migration: add binary embedding blob column for numpy storage
        try:
            await db.execute("ALTER TABLE embeddings ADD COLUMN embedding_blob BLOB")
            await db.commit()
            logger.info("Added embedding_blob column to embeddings table")
        except Exception:
            pass  # Column already exists

        # Backfill: convert existing JSON embeddings to binary blobs
        await _backfill_embedding_blobs(db)

        # One-time migration: backfill signals table from backtest_events
        await _backfill_signals_from_backtests(db)

        # One-time migration: tag existing data with regimes
        await _backfill_regimes(db)

    logger.info("Schema ensured")


async def _backfill_embedding_blobs(db) -> None:
    """Convert existing JSON-serialized embeddings to numpy binary blobs.

    Idempotent — only processes rows where embedding_blob IS NULL.
    Runs in batches of 500 to avoid holding the DB lock too long.
    """
    import json
    import numpy as np

    cursor = await db.execute(
        "SELECT COUNT(*) FROM embeddings WHERE embedding_blob IS NULL"
    )
    pending = (await cursor.fetchone())[0]
    if pending == 0:
        return

    logger.info(f"Backfilling {pending} embedding blobs from JSON...")
    total = 0
    while True:
        cursor = await db.execute(
            "SELECT id, embedding_json FROM embeddings "
            "WHERE embedding_blob IS NULL LIMIT 500"
        )
        rows = await cursor.fetchall()
        if not rows:
            break
        for row_id, emb_json in rows:
            blob = np.array(json.loads(emb_json), dtype=np.float32).tobytes()
            await db.execute(
                "UPDATE embeddings SET embedding_blob = ? WHERE id = ?",
                (blob, row_id),
            )
        await db.commit()
        total += len(rows)
        logger.info(f"  Backfilled {total}/{pending} embedding blobs")

    logger.info(f"Embedding blob backfill complete: {total} rows converted")


async def _backfill_regimes(db) -> None:
    """Tag existing historical_odds_cache and game_results rows with regime."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM game_results WHERE regime IS NULL"
    )
    untagged = (await cursor.fetchone())[0]
    if untagged == 0:
        return

    # Load regime rules
    cursor = await db.execute("SELECT sport, regime_name, start_date, end_date FROM regime_rules")
    rules = await cursor.fetchall()

    for sport, regime_name, start_date, end_date in rules:
        for tbl, date_col in [("game_results", "game_date"), ("historical_odds_cache", "snapshot_date")]:
            end_clause = f"AND {date_col} <= '{end_date}'" if end_date else ""
            await db.execute(
                f"UPDATE {tbl} SET regime = ? "
                f"WHERE sport = ? AND {date_col} >= ? {end_clause} AND regime IS NULL",
                (regime_name, sport, start_date),
            )
    await db.commit()
    logger.info(f"Backfilled regimes for {untagged} untagged rows")


async def _backfill_signals_from_backtests(db) -> None:
    """Copy backtest_events with signal_generated=1 into signals table.

    Idempotent — uses INSERT OR IGNORE and checks if backfill already ran.
    """
    from tools.backtest import _signal_confidence

    # Check if we already have backtest-type signals (skip if already backfilled)
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM signals WHERE signal_type = 'backtest'"
    )
    if row and row[0][0] > 0:
        return  # Already backfilled

    # Count what needs backfilling
    row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
    )
    total = row[0][0] if row else 0
    if total == 0:
        return

    rows = await db.execute_fetchall(
        "SELECT event_id, sport, side, market, book, book_odds_american, "
        "model_fair_prob, edge, ev_pct, kelly_fraction, hypothesis_id, run_id "
        "FROM backtest_events "
        "WHERE signal_generated = 1"
    )

    inserted = 0
    for r in rows:
        edge_val = r[7] or 0
        confidence = _signal_confidence(edge_val)
        await db.execute(
            "INSERT OR IGNORE INTO signals "
            "(event_id, sport, signal_type, team, market, book, "
            "odds_american, fair_probability, fair_prob_source, "
            "edge_pct, ev_pct, confidence, kelly_fraction, "
            "recommended_stake, status, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r[0],        # event_id
                r[1],        # sport
                "backtest",  # signal_type
                r[2],        # side/team
                r[3],        # market
                r[4],        # book
                r[5] or 0,   # odds_american
                r[6] or 0,   # fair_probability
                "cross_book_devig",
                edge_val,
                r[8] or 0,   # ev_pct
                confidence,
                r[9],        # kelly_fraction
                None,        # recommended_stake
                "historical",
                f"hypothesis_id={r[10]}, run_id={r[11]}",
            ),
        )
        inserted += 1

    await db.commit()
    logger.info(f"Backfill migration: inserted {inserted} backtest signals into signals table")


async def get_book_tier(db_path: str = DB_PATH, book_key: str = "") -> str:
    """Look up a book's tier (sharp/retail/reference)."""
    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall(
            "SELECT tier FROM books WHERE book_id = ?", (book_key.lower(),)
        )
        return row[0][0] if row else "retail"
