"""Masters database schema and connection helpers."""

import logging
import os
import sqlite3

logger = logging.getLogger("callisto.golf_masters")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ──────────────────────────────────────────────────
# DATABASE SCHEMA
# ──────────────────────────────────────────────────

MASTERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS masters_historical (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    position TEXT,          -- '1', 'T2', 'T10', 'CUT', 'WD', 'DQ'
    position_numeric INTEGER,  -- numeric finish (ties get same number)
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
    masters_appearances INTEGER,  -- how many Masters before this one
    world_ranking INTEGER,        -- OWGR at time of event
    age INTEGER,
    UNIQUE(year, player)
);

CREATE INDEX IF NOT EXISTS idx_masters_year ON masters_historical(year);
CREATE INDEX IF NOT EXISTS idx_masters_player ON masters_historical(player);
CREATE INDEX IF NOT EXISTS idx_masters_position ON masters_historical(year, position_numeric);

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

CREATE TABLE IF NOT EXISTS masters_field (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    qualification_category TEXT,  -- past_champion, major_winner, world_ranking, etc.
    world_ranking INTEGER,
    confirmed BOOLEAN DEFAULT 0,
    UNIQUE(year, player)
);

CREATE TABLE IF NOT EXISTS masters_backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    method TEXT NOT NULL,         -- 'leave_one_out' or 'rolling_window'
    test_year INTEGER NOT NULL,
    train_years TEXT NOT NULL,    -- JSON list of training years
    predictions_json TEXT,        -- JSON list of {player, predicted_rank, predicted_top10_prob, ...}
    actuals_json TEXT,            -- JSON list of {player, actual_position, ...}
    top10_accuracy REAL,          -- fraction of predicted top-10 who actually finished top-10
    top10_recall REAL,            -- fraction of actual top-10 who were predicted
    cut_accuracy REAL,
    rank_correlation REAL,        -- Spearman rank correlation
    roi_vs_market REAL,           -- hypothetical ROI if we'd bet the predictions
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hypothesis_id, method, test_year)
);

CREATE INDEX IF NOT EXISTS idx_masters_bt_hypo
    ON masters_backtest_results(hypothesis_id, method);

CREATE TABLE IF NOT EXISTS masters_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT,           -- NULL for composite predictions
    year INTEGER NOT NULL,
    player TEXT NOT NULL,
    masters_fit_score REAL,       -- 0-100 composite score
    predicted_rank INTEGER,
    top5_prob REAL,
    top10_prob REAL,
    top20_prob REAL,
    cut_prob REAL,
    win_prob REAL,
    confidence_low INTEGER,       -- predicted finish range low
    confidence_high INTEGER,      -- predicted finish range high
    key_factors TEXT,              -- JSON: which signals drove this rating
    computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(hypothesis_id, year, player)
);
"""


def ensure_masters_schema(db_path: str = DB_PATH) -> None:
    """Create Masters-specific tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    try:
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (s.strip() for s in MASTERS_SCHEMA.split(";") if s.strip()):
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()
    logger.info("Masters schema ensured")

