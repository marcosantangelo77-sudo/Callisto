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

    Use this instead of raw aiosqlite.connect() everywhere. The connection is
    tagged with ``_callisto_db_path`` so ``tools.db_utils.execute_with_retry``
    can route writes through the matching ``WriteCoordinator`` (single-writer
    pattern, see ``tools/db_writer.py``). When no coordinator is running the
    connection still works as a regular aiosqlite connection.
    """
    if db_path is None:
        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    db = await aiosqlite.connect(db_path)
    # Tag the connection so coordinator routing works without a path lookup.
    try:
        db._callisto_db_path = os.path.abspath(db_path)
    except Exception:
        pass
    await db.execute("PRAGMA busy_timeout = 60000")   # 60s — prevents 'database is locked' during bulk writes
    await db.execute("PRAGMA journal_mode = WAL")      # WAL mode for concurrent reads during writes
    await db.execute("PRAGMA synchronous = NORMAL")    # Safe with WAL, reduces fsync overhead
    await db.execute("PRAGMA wal_autocheckpoint = 1000")  # Checkpoint after 1000 pages (~4MB) — prevents WAL bloat
    await db.execute("PRAGMA journal_size_limit = 67108864")  # 64MB WAL cap — SQLite tries harder to checkpoint
    await db.execute("PRAGMA cache_size = -512")        # 512KB page cache (default -2000 = 2MB) — reduces RSS per conn
    await db.execute("PRAGMA mmap_size = 0")           # Disable mmap — prevents WAL from being memory-mapped into RSS
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
    home_team TEXT,
    away_team TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_hypo ON paper_trades(hypothesis_id, game_date);
CREATE INDEX IF NOT EXISTS idx_paper_result ON paper_trades(hypothesis_id, actual_result);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_dedup ON paper_trades(hypothesis_id, event_id, book, game_date);

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
-- EVENT LOG: audit trail for event bus
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    event_data TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_event_log_type_time ON event_log(event_type, created_at);

-- ──────────────────────────────────────────
-- LEARNED CORRELATIONS: empirical correlation estimates
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS learned_correlations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    market_a TEXT NOT NULL,
    market_b TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    mean_a REAL DEFAULT 0,
    mean_b REAL DEFAULT 0,
    m2_a REAL DEFAULT 0,
    m2_b REAL DEFAULT 0,
    co_moment REAL DEFAULT 0,
    pearson_r REAL DEFAULT 0,
    ci_low REAL DEFAULT -1,
    ci_high REAL DEFAULT 1,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, market_a, market_b)
);

CREATE INDEX IF NOT EXISTS idx_learned_corr_sport
    ON learned_correlations(sport, market_a, market_b);

-- ──────────────────────────────────────────
-- KL METRICS: information flow between opening and closing lines
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kl_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    market_type TEXT NOT NULL,
    kl_divergence REAL NOT NULL,
    js_divergence REAL,
    n_books INTEGER,
    opening_entropy REAL,
    closing_entropy REAL,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, event_id, market_type)
);

CREATE INDEX IF NOT EXISTS idx_kl_sport_time ON kl_metrics(sport, computed_at);

-- ──────────────────────────────────────────
-- GRANGER RESULTS: book leadership analysis
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS granger_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,
    book_a TEXT NOT NULL,
    book_b TEXT NOT NULL,
    f_statistic REAL,
    p_value REAL,
    optimal_lag INTEGER,
    is_significant BOOLEAN,
    direction TEXT,
    n_observations INTEGER,
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_granger_sport
    ON granger_results(sport, market_type, computed_at);

-- ──────────────────────────────────────────
-- MARKET MICROSTRUCTURE: per-snapshot market quality metrics
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_microstructure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    game_id TEXT,
    market_type TEXT NOT NULL,
    timestamp DATETIME NOT NULL,
    hhi_overall REAL,
    hhi_sharp REAL,
    entropy_overall REAL,
    entropy_sharp REAL,
    num_books INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, game_id, market_type, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_microstructure_sport_time
    ON market_microstructure(sport, timestamp);

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
-- STATCAST PITCHES: pitch-level telemetry from Baseball Savant (MLB only)
-- One row per pitch. 40 fields carry the signal; the remaining ~80 columns
-- from the savant CSV are intentionally dropped as derivative or low-signal.
-- This table is THE input for pitcher-vs-batter prop modeling, pitch-mix
-- prediction, stuff-based ERA estimators, and hitter-quality-of-contact
-- baselines. Row count projects to ~1.9M pitches per MLB season.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS statcast_pitches (
    -- Identity
    game_pk              INTEGER NOT NULL,
    at_bat_number        INTEGER NOT NULL,
    pitch_number         INTEGER NOT NULL,
    game_date            DATE NOT NULL,
    -- Teams / inning
    home_team            TEXT,
    away_team            TEXT,
    inning               INTEGER,
    inning_topbot        TEXT,              -- 'Top' | 'Bot'
    -- Participants
    pitcher_id           INTEGER,
    pitcher_name         TEXT,
    pitcher_throws       TEXT,              -- 'L' | 'R'
    batter_id            INTEGER,
    batter_name          TEXT,
    batter_stands        TEXT,              -- 'L' | 'R'
    -- Pitch physics
    pitch_type           TEXT,              -- FF, SL, CH, CU, SI, FC, KC, FS, ST, SV, EP, KN
    pitch_name           TEXT,              -- human-readable
    release_speed        REAL,              -- mph
    release_spin_rate    REAL,              -- rpm
    release_extension    REAL,              -- ft
    release_pos_x        REAL,
    release_pos_y        REAL,
    release_pos_z        REAL,
    spin_axis            REAL,              -- degrees
    pfx_x                REAL,              -- horizontal break (ft)
    pfx_z                REAL,              -- vertical break (ft)
    -- Location at the plate
    plate_x              REAL,
    plate_z              REAL,
    zone                 INTEGER,           -- 1-9 in strike zone, 11-14 outside
    sz_top               REAL,
    sz_bot               REAL,
    -- Batted ball (NULL if not in play)
    launch_speed         REAL,              -- exit velocity, mph
    launch_angle         REAL,              -- degrees
    hit_distance_sc      REAL,              -- ft
    bb_type              TEXT,              -- ground_ball | fly_ball | line_drive | popup
    hc_x                 REAL,              -- spray
    hc_y                 REAL,
    -- Outcome
    type                 TEXT,              -- 'S' | 'B' | 'X' (strike/ball/in-play)
    description          TEXT,              -- hit_into_play, ball, called_strike, foul, swinging_strike, etc.
    events               TEXT,              -- strikeout, walk, single, double, triple, home_run, field_out, ...
    -- Count / game state when pitch was thrown
    balls                INTEGER,
    strikes              INTEGER,
    outs_when_up         INTEGER,
    on_1b                INTEGER,           -- runner id or NULL
    on_2b                INTEGER,
    on_3b                INTEGER,
    -- Expected stats (Statcast models)
    estimated_ba_using_speedangle    REAL,
    estimated_woba_using_speedangle  REAL,
    woba_value                       REAL,
    woba_denom                       REAL,
    -- Scoreboard after the pitch
    post_home_score      INTEGER,
    post_away_score      INTEGER,
    ingested_at          DATETIME DEFAULT (datetime('now')),
    PRIMARY KEY (game_pk, at_bat_number, pitch_number)
);

CREATE INDEX IF NOT EXISTS idx_statcast_date       ON statcast_pitches(game_date);
CREATE INDEX IF NOT EXISTS idx_statcast_pitcher    ON statcast_pitches(pitcher_id, game_date);
CREATE INDEX IF NOT EXISTS idx_statcast_batter     ON statcast_pitches(batter_id, game_date);
CREATE INDEX IF NOT EXISTS idx_statcast_matchup    ON statcast_pitches(pitcher_id, batter_id);
CREATE INDEX IF NOT EXISTS idx_statcast_game       ON statcast_pitches(game_pk, at_bat_number, pitch_number);
CREATE INDEX IF NOT EXISTS idx_statcast_inplay     ON statcast_pitches(events) WHERE events IS NOT NULL;

-- ──────────────────────────────────────────
-- MLB PLAYERS: static / slow-changing metadata per player
-- Source: MLB Stats API (https://statsapi.mlb.com, free, no key required).
-- Refreshed nightly. Height / weight / handedness / MLB debut anchor every
-- prop model that asks "how does a 6'6\" LHP with 7ft extension do vs a
-- short-armed RHB who stands in the back of the box".
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mlb_players (
    player_id           INTEGER PRIMARY KEY,
    full_name           TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    primary_position    TEXT,              -- 'P', '1B', 'OF', ...
    position_type       TEXT,              -- 'Pitcher', 'Hitter', 'Two-Way Player'
    bats                TEXT,              -- 'L' | 'R' | 'S' (switch)
    throws              TEXT,              -- 'L' | 'R'
    height_in           INTEGER,           -- inches
    weight_lb           INTEGER,
    birth_date          DATE,
    mlb_debut_date      DATE,
    current_team_id     INTEGER,
    current_team_abbr   TEXT,
    active              INTEGER DEFAULT 1,
    updated_at          DATETIME DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mlb_players_name ON mlb_players(full_name);
CREATE INDEX IF NOT EXISTS idx_mlb_players_team ON mlb_players(current_team_id);

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

CREATE VIEW IF NOT EXISTS box_scores AS
SELECT sport, game_date, home_team AS team_name,
       home_score AS points, away_score AS opponent_points
FROM game_results WHERE home_score IS NOT NULL
UNION ALL
SELECT sport, game_date, away_team AS team_name,
       away_score AS points, home_score AS opponent_points
FROM game_results WHERE away_score IS NOT NULL;

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
    # SECURITY (audit P2): warn loudly when the DB lives inside a OneDrive sync
    # folder. OneDrive holds file handles open while syncing, which corrupts WAL
    # writes and (with bankroll/bet data) replicates financial PII to Microsoft
    # cloud. Marco's current install IS inside OneDrive, so this is a warning
    # rather than a hard fail — but the path forward is to symlink the DB out.
    # Set CALLISTO_SILENCE_ONEDRIVE_WARNING=1 to suppress.
    if (
        "OneDrive" in os.path.abspath(db_path)
        and os.getenv("CALLISTO_SILENCE_ONEDRIVE_WARNING", "0") != "1"
    ):
        logger.warning(
            f"DB path {db_path!r} is inside a OneDrive sync folder. WAL + cloud sync "
            "can corrupt data; bankroll and bets replicate to Microsoft cloud. Move to "
            "a non-synced location (e.g. C:/CallistoLocal/callisto.db) when feasible. "
            "Set CALLISTO_SILENCE_ONEDRIVE_WARNING=1 to suppress this warning."
        )
    async with aiosqlite.connect(db_path) as db:
        # Set PRAGMAs before schema creation — these persist for the connection
        await db.execute("PRAGMA busy_timeout = 120000")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA wal_autocheckpoint = 1000")
        await db.execute("PRAGMA journal_size_limit = 67108864")
        await db.execute("PRAGMA synchronous = NORMAL")  # Safe with WAL, reduces fsync
        await db.commit()

        # SECURITY (audit P2): schema_migrations table tracks which one-time
        # migrations have been applied. Future migrations should INSERT a row
        # here so a failed/skipped migration is detectable instead of silent.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY,"
            "  name TEXT NOT NULL,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        await db.commit()
        # Run schema statements individually instead of executescript() to avoid
        # EXCLUSIVE lock. executescript() blocks ALL concurrent readers/writers;
        # individual execute() calls use WAL-mode write lock (readers can continue).
        for stmt in SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                try:
                    await db.execute(stmt)
                except Exception:
                    pass  # IF NOT EXISTS / OR IGNORE handles duplicates
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

        # Migration: add microstructure metric columns to hypothesis_stats
        for col in ("sortino", "brier_score", "information_coefficient"):
            try:
                await db.execute(f"ALTER TABLE hypothesis_stats ADD COLUMN {col} REAL")
                await db.commit()
                logger.info(f"Added {col} column to hypothesis_stats")
            except Exception:
                pass  # Column already exists

        # Migration: add microstructure metric columns to backtest_runs
        for col in ("sortino_ratio_val", "brier_score", "information_coefficient"):
            try:
                await db.execute(f"ALTER TABLE backtest_runs ADD COLUMN {col} REAL")
                await db.commit()
                logger.info(f"Added {col} column to backtest_runs")
            except Exception:
                pass  # Column already exists

        # Migration: add home_team/away_team to paper_trades for resolution matching
        for col in ("home_team", "away_team"):
            try:
                await db.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} TEXT")
                await db.commit()
                logger.info(f"Added {col} column to paper_trades")
            except Exception:
                pass  # Column already exists

        # Migration (2026-04-18): add `source` to ev_opportunities. Before this,
        # line_monitor INSERTed with (game_id, bookmaker, team, edge) while
        # autonomous.py attempted INSERTs with (event_id, book, side, ev_pct)
        # against the same table — the autonomous writes silently dropped
        # because the table had no such columns, producing recurring
        # OperationalError("no column named event_id") in the WriteCoordinator.
        # autonomous.py is now remapped onto the canonical column names and
        # stamps `source` to distinguish signal provenance.
        try:
            await db.execute("ALTER TABLE ev_opportunities ADD COLUMN source TEXT DEFAULT 'line_movement'")
            await db.commit()
            logger.info("Added source column to ev_opportunities")
        except Exception:
            pass  # Column already exists

        # Migration (audit P2): add UNIQUE index on hypothesis_stats(hypothesis_id, stage)
        # so concurrent backtest writes can't insert competing rows for the same
        # hypothesis/stage. Existing duplicates (if any) are not removed here; the
        # CREATE UNIQUE INDEX call will fail loudly if duplicates exist, prompting a
        # one-time dedupe rather than silently masking the data corruption.
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_hypothesis_stats_unique ON hypothesis_stats(hypothesis_id, stage)"
            )
            await db.commit()
        except Exception as e:
            logger.error(
                f"Could not create UNIQUE index on hypothesis_stats: {e}. "
                "Existing duplicate rows must be deduplicated; run "
                "`DELETE FROM hypothesis_stats WHERE id NOT IN (SELECT MIN(id) "
                "FROM hypothesis_stats GROUP BY hypothesis_id, stage);` and retry."
            )

        # Backfill: convert existing JSON embeddings to binary blobs
        await _backfill_embedding_blobs(db)

        # One-time migration: backfill signals table from backtest_events
        await _backfill_signals_from_backtests(db)

        # One-time migration: tag existing data with regimes
        await _backfill_regimes(db)

    logger.info("Schema ensured")


async def vacuum_db(db_path: str = DB_PATH) -> dict:
    """Run VACUUM + WAL checkpoint to reclaim space (audit P2).

    Call from a periodic task (e.g. weekly). VACUUM rewrites the entire DB so it
    holds an EXCLUSIVE lock — schedule it during a quiet window (overnight) and
    after wait_for_drain() so backtest/line_monitor writers are paused.
    """
    import os as _os
    before = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 300000")  # 5 min for VACUUM
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await db.execute("VACUUM")
        await db.commit()
    after = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0
    reclaimed = max(0, before - after)
    logger.info(
        f"VACUUM complete: {before/1e6:.1f}MB -> {after/1e6:.1f}MB "
        f"(reclaimed {reclaimed/1e6:.1f}MB)"
    )
    return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": reclaimed}


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

    from tools.db_utils import safe_ident
    for sport, regime_name, start_date, end_date in rules:
        for tbl, date_col in [("game_results", "game_date"), ("historical_odds_cache", "snapshot_date")]:
            tbl_q = safe_ident(tbl)
            col_q = safe_ident(date_col)
            # SECURITY (audit C-5): parameterize end_date instead of inlining a quoted
            # string literal. Even though end_date originates from regime_rules (an
            # internal table), splicing a quoted string into SQL is the same anti-pattern
            # the rest of the audit closed.
            if end_date:
                await db.execute(
                    f"UPDATE {tbl_q} SET regime = ? "
                    f"WHERE sport = ? AND {col_q} >= ? AND {col_q} <= ? AND regime IS NULL",
                    (regime_name, sport, start_date, end_date),
                )
            else:
                await db.execute(
                    f"UPDATE {tbl_q} SET regime = ? "
                    f"WHERE sport = ? AND {col_q} >= ? AND regime IS NULL",
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
