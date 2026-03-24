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

logger = logging.getLogger("callisto.schema")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

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
    edge_threshold REAL NOT NULL DEFAULT 0.02,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','backtesting','paper_trading','live','retired','rejected')),
    min_sample_size INTEGER NOT NULL DEFAULT 1000,
    significance_level REAL NOT NULL DEFAULT 0.05,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at DATETIME,
    promoted_by TEXT,
    notes TEXT
);

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
"""


async def ensure_schema(db_path: str = DB_PATH) -> None:
    """Create or upgrade all tables. Safe to call multiple times."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
    logger.info("Schema ensured")


async def get_book_tier(db_path: str = DB_PATH, book_key: str = "") -> str:
    """Look up a book's tier (sharp/retail/reference)."""
    async with aiosqlite.connect(db_path) as db:
        row = await db.execute_fetchall(
            "SELECT tier FROM books WHERE book_id = ?", (book_key.lower(),)
        )
        return row[0][0] if row else "retail"
