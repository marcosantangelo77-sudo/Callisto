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
    """Open a DB connection with the canonical Callisto pragma set.

    Use this instead of raw aiosqlite.connect() everywhere. The connection is
    tagged with ``_callisto_db_path`` so ``tools.db_utils.execute_with_retry``
    can route writes through the matching ``WriteCoordinator`` (single-writer
    pattern, see ``tools/db_writer.py``). When no coordinator is running the
    connection still works as a regular aiosqlite connection.

    Canonical pragmas (feat/db-wal-health):
        journal_mode=WAL, busy_timeout=120000, synchronous=NORMAL,
        foreign_keys=ON, wal_autocheckpoint=1000, journal_size_limit=64MB,
        cache_size=-512, mmap_size=0.
    """
    if db_path is None:
        from tools.state_paths import db_path as _resolve_db_path
        db_path = _resolve_db_path()
    db = await aiosqlite.connect(db_path)
    try:
        db._callisto_db_path = os.path.abspath(db_path)
    except Exception:
        pass
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 120000")
    await db.execute("PRAGMA synchronous = NORMAL")
    await db.execute("PRAGMA wal_autocheckpoint = 1000")
    await db.execute("PRAGMA journal_size_limit = 67108864")
    await db.execute("PRAGMA cache_size = -512")
    await db.execute("PRAGMA mmap_size = 0")
    if os.getenv("CALLISTO_DISABLE_FK", "0") != "1":
        await db.execute("PRAGMA foreign_keys = ON")
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
    logged_at DATETIME,
    local_game_date DATE  -- Canonical: date in venue's local tz. See tools.game_dates.
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
    -- 'paused' added 2026-04-21: demotion state from LIVE for underperforming
    -- hypotheses (see tools.hypothesis.review_live_hypotheses). Not retired:
    -- can be un-paused once stats recover.
    -- 'drawdown_paused' added 2026-04-22 (feat/portfolio-kelly-live-loop):
    -- distinct from 'paused' so recovery logic knows the system-wide drawdown
    -- kill-switch fired and a *manual* re-enable is required after review.
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','backtesting','paper_trading','live','paused','drawdown_paused','retired','rejected')),
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
    sortino_ratio_val REAL,
    brier_score REAL,
    information_coefficient REAL,
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
    game_date DATE NOT NULL,  -- DEPRECATED: use local_game_date. Kept for back-compat.
    local_game_date DATE,     -- Canonical: date in venue's local tz. See tools.game_dates.
    snapshot_time DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id),
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_bt_events_run ON backtest_events(run_id, game_date);
CREATE INDEX IF NOT EXISTS idx_bt_events_signal ON backtest_events(hypothesis_id, signal_generated, actual_result);
CREATE INDEX IF NOT EXISTS idx_bt_events_local_date ON backtest_events(local_game_date);

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
    game_date DATE NOT NULL,  -- DEPRECATED: use local_game_date. Kept for back-compat.
    local_game_date DATE,     -- Canonical: date in venue's local tz. See tools.game_dates.
    home_team TEXT,
    away_team TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_hypo ON paper_trades(hypothesis_id, game_date);
CREATE INDEX IF NOT EXISTS idx_paper_result ON paper_trades(hypothesis_id, actual_result);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_dedup ON paper_trades(hypothesis_id, event_id, book, game_date);
CREATE INDEX IF NOT EXISTS idx_paper_local_date ON paper_trades(local_game_date);

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
    sortino REAL,
    brier_score REAL,
    information_coefficient REAL,
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
    game_date DATE NOT NULL,  -- DEPRECATED: use local_game_date.
    local_game_date DATE,     -- Canonical: date in venue's local tz.
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
CREATE INDEX IF NOT EXISTS idx_game_ctx_local_date ON game_contexts(sport, local_game_date);

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
-- STATCAST PITCHES: pitch-level telemetry from Baseball Savant (MLB only).
-- One row per pitch. 40 high-signal fields kept, the remaining ~80 CSV
-- columns dropped as derivative or low-signal. This table is THE input
-- for pitcher-vs-batter prop modeling, pitch-mix prediction, stuff-based
-- ERA estimators, and hitter-quality-of-contact baselines. Row count
-- projects to ~1.9M pitches per MLB season.
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
-- NBA PLAYERS: core metadata + NBA Draft Combine measurables where available.
-- Source: stats.nba.com commonallplayers + commonplayerinfo (height, weight,
-- jersey, position, experience), and draftcombineplayeranthro for wingspan
-- and standing reach (available only for draft-class players since 2000).
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nba_players (
    player_id           INTEGER PRIMARY KEY,
    full_name           TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    position            TEXT,
    height_in           INTEGER,
    weight_lb           INTEGER,
    wingspan_in         REAL,
    standing_reach_in   REAL,
    jersey_number       TEXT,
    birth_date          DATE,
    country             TEXT,
    college             TEXT,
    draft_year          INTEGER,
    draft_round         INTEGER,
    draft_pick          INTEGER,
    years_pro           INTEGER,
    current_team_id     INTEGER,
    current_team_abbr   TEXT,
    active              INTEGER DEFAULT 1,
    updated_at          DATETIME DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nba_players_name ON nba_players(full_name);
CREATE INDEX IF NOT EXISTS idx_nba_players_team ON nba_players(current_team_id);

-- ──────────────────────────────────────────
-- NBA SHOT EVENTS: one row per shot attempt. Source: stats.nba.com
-- shotchartdetail (free, requires UA header). Court coords in tenths of
-- feet; origin at the hoop, +y toward midcourt, +x toward right sideline.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nba_shot_events (
    game_id             TEXT NOT NULL,
    event_num           INTEGER NOT NULL,
    game_date           DATE NOT NULL,
    player_id           INTEGER,
    player_name         TEXT,
    team_id             INTEGER,
    team_abbr           TEXT,
    period              INTEGER,
    minutes_remaining   INTEGER,
    seconds_remaining   INTEGER,
    shot_type           TEXT,
    action_type         TEXT,
    shot_zone_basic     TEXT,
    shot_zone_area      TEXT,
    shot_zone_range     TEXT,
    shot_distance       REAL,
    loc_x               INTEGER,
    loc_y               INTEGER,
    made_flag           INTEGER,
    htm                 TEXT,
    vtm                 TEXT,
    ingested_at         DATETIME DEFAULT (datetime('now')),
    PRIMARY KEY (game_id, event_num)
);
CREATE INDEX IF NOT EXISTS idx_nba_shots_player_date ON nba_shot_events(player_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nba_shots_game ON nba_shot_events(game_id);
CREATE INDEX IF NOT EXISTS idx_nba_shots_zone ON nba_shot_events(player_id, shot_zone_basic, made_flag);
CREATE INDEX IF NOT EXISTS idx_nba_shots_type ON nba_shot_events(shot_type, made_flag);

-- ──────────────────────────────────────────
-- NFL PLAYERS: roster + scout-reported metadata. Source: nflfastR rosters
-- CSV (free, weekly updates). gsis_id is the canonical player_id.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nfl_players (
    player_id           TEXT PRIMARY KEY,
    full_name           TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    position            TEXT,
    position_group      TEXT,
    jersey_number       INTEGER,
    height_in           INTEGER,
    weight_lb           INTEGER,
    birth_date          DATE,
    college             TEXT,
    draft_year          INTEGER,
    draft_round         INTEGER,
    draft_pick          INTEGER,
    years_exp           INTEGER,
    current_team        TEXT,
    status              TEXT,
    headshot_url        TEXT,
    updated_at          DATETIME DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nfl_players_name ON nfl_players(full_name);
CREATE INDEX IF NOT EXISTS idx_nfl_players_team ON nfl_players(current_team);

-- ──────────────────────────────────────────
-- NFL COMBINE: measurables and drill results from the NFL Scouting Combine.
-- Source: nflreadr combine CSV / Pro-Football-Reference. Public, free.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nfl_combine_results (
    player_id           TEXT,
    combine_year        INTEGER NOT NULL,
    full_name           TEXT NOT NULL,
    position            TEXT,
    college             TEXT,
    height_in           REAL,
    weight_lb           INTEGER,
    arm_length_in       REAL,
    hand_size_in        REAL,
    forty_yard          REAL,
    bench_press_reps    INTEGER,
    vertical_in         REAL,
    broad_jump_in       INTEGER,
    three_cone          REAL,
    shuttle_20y         REAL,
    draft_year          INTEGER,
    draft_round         INTEGER,
    draft_pick          INTEGER,
    draft_team          TEXT,
    PRIMARY KEY (combine_year, full_name, position)
);
CREATE INDEX IF NOT EXISTS idx_nfl_combine_player ON nfl_combine_results(player_id);
CREATE INDEX IF NOT EXISTS idx_nfl_combine_name ON nfl_combine_results(full_name);

-- ──────────────────────────────────────────
-- NFL PLAY EVENTS: play-by-play per game. Source: nflfastR play_by_play
-- per-season CSV on GitHub (free). High-signal fields kept.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nfl_play_events (
    play_id             INTEGER NOT NULL,
    game_id             TEXT NOT NULL,
    game_date           DATE,
    home_team           TEXT,
    away_team           TEXT,
    posteam             TEXT,
    defteam             TEXT,
    season              INTEGER,
    week                INTEGER,
    qtr                 INTEGER,
    time                TEXT,
    down                INTEGER,
    ydstogo             INTEGER,
    yrdln               TEXT,
    yardline_100        INTEGER,
    play_type           TEXT,
    yards_gained        INTEGER,
    epa                 REAL,
    wpa                 REAL,
    success             INTEGER,
    passer_id           TEXT,
    passer_name         TEXT,
    receiver_id         TEXT,
    receiver_name       TEXT,
    air_yards           REAL,
    yards_after_catch   REAL,
    pass_length         TEXT,
    pass_location       TEXT,
    complete_pass       INTEGER,
    incomplete_pass     INTEGER,
    interception        INTEGER,
    rusher_id           TEXT,
    rusher_name         TEXT,
    run_location        TEXT,
    run_gap             TEXT,
    sack                INTEGER,
    qb_hit              INTEGER,
    tackle_with_assist  INTEGER,
    sack_player_id      TEXT,
    touchdown           INTEGER,
    td_player_id        TEXT,
    field_goal_attempt  INTEGER,
    field_goal_result   TEXT,
    kick_distance       INTEGER,
    score_differential  INTEGER,
    PRIMARY KEY (game_id, play_id)
);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_date ON nfl_play_events(game_date);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_passer ON nfl_play_events(passer_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_receiver ON nfl_play_events(receiver_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_rusher ON nfl_play_events(rusher_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_game ON nfl_play_events(game_id);
CREATE INDEX IF NOT EXISTS idx_nfl_plays_type ON nfl_play_events(play_type, qtr);

-- ──────────────────────────────────────────
-- NHL PLAYERS: identity + physical + shooting/catching hand.
-- Source: api.nhle.com (free, no key).
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nhl_players (
    player_id           INTEGER PRIMARY KEY,
    full_name           TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    position            TEXT,
    shoots_catches      TEXT,
    sweater_number      INTEGER,
    height_in           INTEGER,
    weight_lb           INTEGER,
    birth_date          DATE,
    birth_country       TEXT,
    birth_city          TEXT,
    draft_year          INTEGER,
    draft_round         INTEGER,
    draft_pick          INTEGER,
    draft_team          TEXT,
    current_team_id     INTEGER,
    current_team_abbr   TEXT,
    active              INTEGER DEFAULT 1,
    updated_at          DATETIME DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nhl_players_name ON nhl_players(full_name);
CREATE INDEX IF NOT EXISTS idx_nhl_players_team ON nhl_players(current_team_id);

-- ──────────────────────────────────────────
-- NHL SHOT EVENTS: one row per shot attempt. Source: api.nhle.com
-- /v1/gamecenter/{game}/play-by-play (free). Rink coords x in [-100, 100],
-- y in [-42.5, 42.5]. zone_code is relative to the shooting team.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nhl_shot_events (
    game_id             INTEGER NOT NULL,
    event_id            INTEGER NOT NULL,
    game_date           DATE,
    period              INTEGER,
    period_type         TEXT,
    time_in_period      TEXT,
    time_remaining      TEXT,
    event_type          TEXT,
    shot_type           TEXT,
    situation_code      TEXT,
    x_coord             REAL,
    y_coord             REAL,
    zone_code           TEXT,
    shooting_team_id    INTEGER,
    shooting_team_abbr  TEXT,
    shooter_id          INTEGER,
    goalie_id           INTEGER,
    assist1_id          INTEGER,
    assist2_id          INTEGER,
    is_goal             INTEGER DEFAULT 0,
    home_score          INTEGER,
    away_score          INTEGER,
    ingested_at         DATETIME DEFAULT (datetime('now')),
    PRIMARY KEY (game_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_nhl_shots_shooter ON nhl_shot_events(shooter_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nhl_shots_goalie ON nhl_shot_events(goalie_id, game_date);
CREATE INDEX IF NOT EXISTS idx_nhl_shots_matchup ON nhl_shot_events(shooter_id, goalie_id);
CREATE INDEX IF NOT EXISTS idx_nhl_shots_game ON nhl_shot_events(game_id);
CREATE INDEX IF NOT EXISTS idx_nhl_shots_goal ON nhl_shot_events(is_goal) WHERE is_goal = 1;

-- ──────────────────────────────────────────
-- NCAA BASKETBALL (M + W) PLAYERS: rosters with class / height / position.
-- Source: ESPN college endpoints. `sport` discriminates M vs W.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ncaa_basketball_players (
    sport               TEXT NOT NULL,
    player_id           TEXT NOT NULL,
    full_name           TEXT NOT NULL,
    first_name          TEXT,
    last_name           TEXT,
    team_id             TEXT,
    team_abbr           TEXT,
    team_name           TEXT,
    jersey_number       TEXT,
    position            TEXT,
    class               TEXT,
    height_in           INTEGER,
    weight_lb           INTEGER,
    home_town           TEXT,
    hand                TEXT,
    active              INTEGER DEFAULT 1,
    updated_at          DATETIME DEFAULT (datetime('now')),
    PRIMARY KEY (sport, player_id)
);
CREATE INDEX IF NOT EXISTS idx_ncaa_bball_players_name ON ncaa_basketball_players(full_name);
CREATE INDEX IF NOT EXISTS idx_ncaa_bball_players_team ON ncaa_basketball_players(sport, team_id);

-- ──────────────────────────────────────────
-- NCAA BASKETBALL (M + W) GAME STATS: per-player per-game box + advanced.
-- Source: ESPN boxscore.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ncaa_basketball_game_stats (
    sport               TEXT NOT NULL,
    game_id             TEXT NOT NULL,
    game_date           DATE,
    player_id           TEXT NOT NULL,
    player_name         TEXT,
    team_id             TEXT,
    team_abbr           TEXT,
    opponent_team_id    TEXT,
    opponent_abbr       TEXT,
    is_home             INTEGER,
    started             INTEGER,
    minutes             REAL,
    points              INTEGER,
    rebounds            INTEGER,
    off_reb             INTEGER,
    def_reb             INTEGER,
    assists             INTEGER,
    steals              INTEGER,
    blocks              INTEGER,
    turnovers           INTEGER,
    personal_fouls      INTEGER,
    fgm                 INTEGER,
    fga                 INTEGER,
    fg3m                INTEGER,
    fg3a                INTEGER,
    ftm                 INTEGER,
    fta                 INTEGER,
    plus_minus          INTEGER,
    true_shooting_pct   REAL,
    efg_pct             REAL,
    PRIMARY KEY (sport, game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_ncaa_bball_stats_player ON ncaa_basketball_game_stats(sport, player_id, game_date);
CREATE INDEX IF NOT EXISTS idx_ncaa_bball_stats_game ON ncaa_basketball_game_stats(sport, game_id);

-- ──────────────────────────────────────────
-- PGA PLAYER ROUNDS: round-level strokes-gained + core stats per event.
-- Source: DataGolf public pages and/or official PGA Tour API when scrape-able.
-- Complements the existing golf_masters tables for the Masters specifically.
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS golf_player_rounds (
    player_id           TEXT NOT NULL,
    player_name         TEXT NOT NULL,
    event_id            TEXT NOT NULL,
    event_name          TEXT,
    course              TEXT,
    season              INTEGER,
    round_num           INTEGER NOT NULL,
    round_date          DATE,
    tee_time            TEXT,
    score               INTEGER,
    score_to_par        INTEGER,
    thru                INTEGER,
    sg_total            REAL,
    sg_ott              REAL,
    sg_app              REAL,
    sg_arg              REAL,
    sg_putt             REAL,
    sg_t2g              REAL,
    driving_distance    REAL,
    driving_accuracy    REAL,
    gir_pct             REAL,
    scrambling_pct      REAL,
    putts_per_round     REAL,
    made_cut            INTEGER,
    PRIMARY KEY (player_id, event_id, round_num)
);
CREATE INDEX IF NOT EXISTS idx_golf_rounds_player ON golf_player_rounds(player_id, round_date);
CREATE INDEX IF NOT EXISTS idx_golf_rounds_event ON golf_player_rounds(event_id, round_num);
CREATE INDEX IF NOT EXISTS idx_golf_rounds_course ON golf_player_rounds(course, round_date);

-- live_edge_surface: ranked output of tools.quant.edge_ranker, one row
-- per (market, outcome, snapshot). Consumed by /edges/live and by the
-- executor once it's wired.
CREATE TABLE IF NOT EXISTS live_edge_surface (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at        TEXT NOT NULL,
    sport              TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    market             TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    placement_book     TEXT NOT NULL,
    placement_implied  REAL NOT NULL,
    placement_fair     REAL NOT NULL,
    consensus_fair     REAL NOT NULL,
    consensus_std_err  REAL,
    raw_edge           REAL NOT NULL,
    effective_edge     REAL NOT NULL,
    penalty_total      REAL NOT NULL,
    penalty_breakdown  TEXT NOT NULL,
    disagreement       INTEGER DEFAULT 0,
    n_books            INTEGER NOT NULL,
    outlier_books      TEXT,
    decision           TEXT NOT NULL,
    rank               INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edge_surface_recency ON live_edge_surface(computed_at);
CREATE INDEX IF NOT EXISTS idx_edge_surface_rank ON live_edge_surface(computed_at, rank);
CREATE INDEX IF NOT EXISTS idx_edge_surface_sport ON live_edge_surface(sport, computed_at);
CREATE INDEX IF NOT EXISTS idx_edge_surface_event ON live_edge_surface(event_id, market);

-- ──────────────────────────────────────────
-- GAME RESULTS: actual scores for backtest resolution
-- ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS game_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    game_date TEXT NOT NULL,   -- DEPRECATED: use local_game_date (see tools.game_dates).
    local_game_date DATE,      -- Canonical: date in venue's local tz.
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
CREATE INDEX IF NOT EXISTS idx_game_results_local_date
    ON game_results(sport, local_game_date);

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
-- Single-column time index for retention pruning (see prop_scraper_free.py
-- for rationale). Prevents a full-table scan when the pruner runs
-- `DELETE FROM prop_snapshots WHERE snapshot_time < ?`.
CREATE INDEX IF NOT EXISTS idx_prop_snap_time
    ON prop_snapshots(snapshot_time);

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

-- ──────────────────────────────────────────
-- INGESTION OBSERVABILITY: per-source run ledger
-- ──────────────────────────────────────────
-- Populated by @tracked_ingestion (tools/ingestion_tracking.py). Each call to
-- a wrapped ingestion function writes one row on entry (status='running') and
-- updates it on exit with the final status, duration, and row count.
--
-- Source tags are hierarchical (e.g. 'espn.scoreboard.mlb',
-- 'odds_api_io.v3.odds.updated') and STABLE — changing them loses history
-- for SLA evaluation.
--
-- Read by tools/health.py::_check_data_collector which compares each source's
-- most-recent `finished_at` against the SLA table and trips the breaker when
-- runs go stale. This is how Callisto notices that ESPN has been 500-looping
-- for six hours — something we previously had ZERO visibility into.
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
);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_source_finished
    ON ingestion_runs(source, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_status
    ON ingestion_runs(status, finished_at DESC);
"""


async def _safe_add_column(
    db, table: str, column: str, coltype: str
) -> None:
    """Idempotent ADD COLUMN that distinguishes "already exists" from real errors.

    SQLite reports the already-exists case with a specific substring in the
    error message; anything else (permission denied, disk full, invalid type,
    missing table) is a real problem and must reach the logs instead of
    being swallowed as `except: pass` — that pattern silently leaves the
    schema incomplete and downstream writers fail with "no such column"
    hours later, far from the root cause.
    """
    from tools.db_utils import safe_ident
    tbl = safe_ident(table)
    col = safe_ident(column)
    try:
        await db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coltype}")
        await db.commit()
        logger.info(f"Added {column} column to {table}")
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            # Expected on second+ runs; not worth logging at INFO level.
            return
        logger.warning(
            f"Failed to ADD COLUMN {column} {coltype} to {table}: {e!r}. "
            "Schema may be incomplete — check underlying cause before restarting."
        )


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
        # Run schema statements individually instead of executescript() to
        # avoid the EXCLUSIVE lock executescript takes for the whole script.
        #
        # 2026-04-18: two bugs bit the naive `split(";")` approach and caused
        # tables to silently fail to materialize:
        #   1. `;` characters inside `-- ...` comments split the DDL mid-
        #      statement (statcast_pitches, nba_shot_events).
        #   2. A leading `-- header divider` comment on a chunk made the
        #      naive `startswith("--")` filter drop the whole CREATE TABLE.
        # Fix: strip every `-- line comment` (up to end-of-line) from the
        # entire SCHEMA_SQL body BEFORE splitting on `;`. Inline trailing
        # `-- ...` column comments survive as part of each column line until
        # stripped, which is fine because SQLite would accept them if kept
        # anyway. This is DDL-only; no string literals in SCHEMA_SQL depend
        # on retaining the `-- ` sequence.
        import re as _re_schema
        cleaned = _re_schema.sub(r"--[^\n]*", "", SCHEMA_SQL)
        for raw in cleaned.split(";"):
            stmt = raw.strip()
            if not stmt:
                continue
            try:
                await db.execute(stmt)
            except Exception as e:
                # Pre-fix this was ``except Exception: pass`` which silently
                # dropped any DDL failure — including typos, wrong column
                # counts, referenced-but-missing tables. Downstream writes
                # then exploded hours later with confusing "no such column"
                # errors. Log the failing statement and the root cause;
                # IF NOT EXISTS / OR IGNORE duplicates are still tolerated
                # because SQLite reports them with a recognisable message.
                msg = str(e).lower()
                if (
                    "already exists" in msg
                    or "duplicate column" in msg
                ):
                    continue
                first_line = stmt.splitlines()[0][:140] if stmt else "<empty>"
                logger.error(
                    f"ensure_schema statement failed: {e!r} — "
                    f"first line: {first_line!r}. Downstream writers that "
                    f"depend on this table/column will fail."
                )
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
        await _safe_add_column(db, "embeddings", "embedding_blob", "BLOB")

        # Migration (2026-04-21): add model_name to embeddings so we don't mix
        # vectors from different embed models when the EMBED_MODEL env var
        # changes. Old rows default to NULL — retrieval treats NULL as "unknown,
        # logged-and-excluded" rather than silently comparing cross-model.
        await _safe_add_column(db, "embeddings", "model_name", "TEXT")

        # Migration (2026-04-21): wiki_articles gains source_task_id so
        # file_task_result can join back to the task_queue instead of minting
        # a fake "task_{int(time.time())}" id that can't be traced.
        await _safe_add_column(db, "wiki_articles", "source_task_id", "TEXT")

        # Migration: add microstructure metric columns to hypothesis_stats.
        # (Baseline schema now includes these; migration stays for old DBs.)
        for col in ("sortino", "brier_score", "information_coefficient"):
            await _safe_add_column(db, "hypothesis_stats", col, "REAL")

        # Migration: add microstructure metric columns to backtest_runs.
        for col in ("sortino_ratio_val", "brier_score", "information_coefficient"):
            await _safe_add_column(db, "backtest_runs", col, "REAL")

        # Migration: add home_team/away_team to paper_trades for resolution matching
        for col in ("home_team", "away_team"):
            await _safe_add_column(db, "paper_trades", col, "TEXT")

        # Migration (2026-04-18): add `source` to ev_opportunities. Before this,
        # line_monitor INSERTed with (game_id, bookmaker, team, edge) while
        # autonomous.py attempted INSERTs with (event_id, book, side, ev_pct)
        # against the same table — the autonomous writes silently dropped
        # because the table had no such columns, producing recurring
        # OperationalError("no column named event_id") in the WriteCoordinator.
        # autonomous.py is now remapped onto the canonical column names and
        # stamps `source` to distinguish signal provenance.
        await _safe_add_column(
            db, "ev_opportunities", "source", "TEXT DEFAULT 'line_movement'"
        )

        # Migration (audit 2026-04-21): allow 'paused' status for LIVE-hypothesis
        # demotion loop. Older DBs have a CHECK constraint that rejects 'paused';
        # SQLite cannot alter a CHECK in place so we rebuild the table.
        try:
            cur = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 20260421"
            )
            if not await cur.fetchone():
                cur = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='hypotheses'"
                )
                row = await cur.fetchone()
                table_sql = row[0] if row else ""
                if table_sql and "'paused'" not in table_sql:
                    logger.info("Migration 20260421: rebuilding hypotheses table to add 'paused' status")
                    await db.execute("BEGIN")
                    try:
                        await db.execute("ALTER TABLE hypotheses RENAME TO hypotheses_old_20260421")
                        await db.execute("""
                            CREATE TABLE hypotheses (
                                hypothesis_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                thesis TEXT NOT NULL,
                                sport TEXT NOT NULL,
                                market_type TEXT NOT NULL,
                                model_config TEXT NOT NULL,
                                edge_threshold REAL NOT NULL DEFAULT 0.01,
                                status TEXT NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','backtesting','paper_trading','live','paused','retired','rejected')),
                                min_sample_size INTEGER NOT NULL DEFAULT 50,
                                significance_level REAL NOT NULL DEFAULT 0.05,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                promoted_at DATETIME,
                                promoted_by TEXT,
                                notes TEXT
                            )
                        """)
                        # Copy all existing rows (unchanged data).
                        await db.execute(
                            "INSERT INTO hypotheses "
                            "(hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes) "
                            "SELECT hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes "
                            "FROM hypotheses_old_20260421"
                        )
                        await db.execute("DROP TABLE hypotheses_old_20260421")
                        await db.execute(
                            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name ON hypotheses(name)"
                        )
                        await db.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (20260421, 'add_paused_status')"
                        )
                        await db.commit()
                        logger.info("Migration 20260421 complete: 'paused' status now allowed")
                    except Exception as mig_err:
                        await db.rollback()
                        logger.error(f"Migration 20260421 failed: {mig_err}")
                else:
                    # Table already has 'paused' — record migration as complete.
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name) "
                        "VALUES (20260421, 'add_paused_status')"
                    )
                    await db.commit()
        except Exception as e:
            logger.warning(f"Could not evaluate migration 20260421: {e}")

        # Migration 20260422 (feat/portfolio-kelly-live-loop): allow
        # 'drawdown_paused' status so the drawdown kill-switch can flag LIVE
        # hypotheses distinctly from ordinary 'paused' demotion. Also create
        # the bankroll_peak table used by the kill switch.
        try:
            cur = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 20260422"
            )
            if not await cur.fetchone():
                cur = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='hypotheses'"
                )
                row = await cur.fetchone()
                table_sql = row[0] if row else ""
                if table_sql and "'drawdown_paused'" not in table_sql:
                    logger.info("Migration 20260422: rebuilding hypotheses table to add 'drawdown_paused' status")
                    await db.execute("BEGIN")
                    try:
                        await db.execute("ALTER TABLE hypotheses RENAME TO hypotheses_old_20260422")
                        await db.execute("""
                            CREATE TABLE hypotheses (
                                hypothesis_id TEXT PRIMARY KEY,
                                name TEXT NOT NULL,
                                thesis TEXT NOT NULL,
                                sport TEXT NOT NULL,
                                market_type TEXT NOT NULL,
                                model_config TEXT NOT NULL,
                                edge_threshold REAL NOT NULL DEFAULT 0.01,
                                status TEXT NOT NULL DEFAULT 'draft'
                                    CHECK(status IN ('draft','backtesting','paper_trading','live','paused','drawdown_paused','retired','rejected')),
                                min_sample_size INTEGER NOT NULL DEFAULT 50,
                                significance_level REAL NOT NULL DEFAULT 0.05,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                promoted_at DATETIME,
                                promoted_by TEXT,
                                notes TEXT
                            )
                        """)
                        await db.execute(
                            "INSERT INTO hypotheses "
                            "(hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes) "
                            "SELECT hypothesis_id, name, thesis, sport, market_type, "
                            " model_config, edge_threshold, status, min_sample_size, "
                            " significance_level, created_at, updated_at, "
                            " promoted_at, promoted_by, notes "
                            "FROM hypotheses_old_20260422"
                        )
                        await db.execute("DROP TABLE hypotheses_old_20260422")
                        await db.execute(
                            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name ON hypotheses(name)"
                        )
                        await db.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (20260422, 'add_drawdown_paused_status')"
                        )
                        await db.commit()
                        logger.info("Migration 20260422 complete: 'drawdown_paused' status now allowed")
                    except Exception as mig_err:
                        await db.rollback()
                        logger.error(f"Migration 20260422 failed: {mig_err}")
                else:
                    await db.execute(
                        "INSERT OR IGNORE INTO schema_migrations (version, name) "
                        "VALUES (20260422, 'add_drawdown_paused_status')"
                    )
                    await db.commit()

            # bankroll_peak table (drawdown kill-switch state). Keyed by date
            # so we can see a rolling 30d peak via a simple MAX query.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bankroll_peak (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at DATETIME NOT NULL,
                    balance REAL NOT NULL,
                    note TEXT
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_bankroll_peak_ts ON bankroll_peak(observed_at)"
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"Could not evaluate migration 20260422: {e}")

        # Migration (odds-freshness audit): add ingestion-time stamp to
        # odds_snapshots so downstream consumers can compute freshness-weighted
        # consensus. The existing `timestamp` column records the row's write
        # time; `fetched_at` records when we *fetched* the odds (may differ if
        # a snapshot is re-processed, replayed from WS, or backfilled).
        # Books themselves emit `last_update` inside snapshot_json — that's
        # the book's own stamp and cannot be trusted for our freshness model.
        await _safe_add_column(db, "odds_snapshots", "fetched_at", "TEXT")

        # Migration (odds-freshness audit): event source so we can distinguish
        # scheduled snapshots (interval=15m), WebSocket deltas, and
        # incremental /odds/updated polls. Used for telemetry and for
        # replaying only the fresh slice.
        await _safe_add_column(
            db, "odds_snapshots", "source", "TEXT DEFAULT 'interval'"
        )

        # Migration (odds-freshness audit): add prob-basis-point CLV column
        # alongside legacy clv_cents (which was a mix of American cents and
        # prob×10000 depending on which code path wrote it — see
        # clv_tracker.py:414 vs :419). Going forward writers populate
        # clv_prob_bp unambiguously; readers should prefer it and treat
        # clv_cents as deprecated/mixed-units.
        await _safe_add_column(db, "clv_log", "clv_prob_bp", "REAL")

        # Migration (feat/regime-aware-sizing, 2026-04-22): stamp the
        # market regime (sport|season_phase) at placement time so CLV
        # analysis can bucket by regime. Future regime-bucket queries show
        # whether a hypothesis is regime-robust or regime-fragile.
        await _safe_add_column(db, "clv_log", "regime_phase_at_placement", "TEXT")

        # Migration (odds-freshness audit): gate flag for ev_opportunities.
        # An ev_opportunity with steam_only=1 means the row was surfaced by
        # line-movement consensus alone, NOT ratified by an independent model
        # (pace, props, sim). Kept so downstream filters can exclude
        # steam-only rows from Telegram alerts without losing them from
        # research backfill.
        await _safe_add_column(
            db, "ev_opportunities", "steam_only", "INTEGER DEFAULT 0"
        )

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

    Implementation note (vacuum-in-tx fix):
    SQLite refuses ``VACUUM`` when any transaction is active on the connection,
    and aiosqlite's default isolation_level opens an *implicit* transaction
    around every write. That manifested as the silent
    ``OperationalError: cannot VACUUM from within a transaction`` hidden behind
    the WriteCoordinator's ``writes_failed`` counter.

    The correct call path for VACUUM is therefore a dedicated, autocommit,
    UNTAGGED stdlib ``sqlite3`` connection on a worker thread:
      * stdlib sqlite3 with ``isolation_level=None`` ⇒ true autocommit, no
        implicit BEGIN around VACUUM.
      * Not tagged with ``_callisto_db_path`` ⇒ the aiosqlite monkey-patch in
        ``tools.db_writer.install_aiosqlite_routing`` can never re-route VACUUM
        through the coordinator (which would re-introduce the bug).
      * Run inside ``asyncio.to_thread`` so we don't block the event loop for
        the minutes VACUUM can take on a multi-GB DB.
    """
    import os as _os
    import sqlite3 as _sqlite3
    import asyncio as _asyncio

    before = _os.path.getsize(db_path) if _os.path.exists(db_path) else 0

    def _run_vacuum_sync() -> None:
        # isolation_level=None ⇒ autocommit. No implicit BEGIN is issued by
        # the driver, so VACUUM runs on a connection with no active tx.
        conn = _sqlite3.connect(db_path, isolation_level=None, timeout=300.0)
        try:
            # 5-minute busy timeout for the EXCLUSIVE lock contention window.
            conn.execute("PRAGMA busy_timeout = 300000")
            # Truncate WAL first so VACUUM's new DB is as small as possible.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # Invariant check: silent-failure → loud-failure upgrade. If this
            # connection somehow has an open transaction we refuse to VACUUM
            # rather than letting SQLite surface the confusing error string.
            if conn.in_transaction:
                raise RuntimeError(
                    "vacuum_db invariant violated: dedicated connection has "
                    "an open transaction before VACUUM. Refusing to VACUUM."
                )
            conn.execute("VACUUM")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    await _asyncio.to_thread(_run_vacuum_sync)

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
