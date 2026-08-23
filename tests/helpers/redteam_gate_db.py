"""Shared in-memory DB builder for promotion-gate red-team tests.

Mirrors the schema HypothesisManager's gate SQL touches (same tables
test_promotion_gates.py builds, plus backtest_runs and clv_log which the
Šidák denominator and the canonical CLV path read).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = [
    """CREATE TABLE hypotheses (
        hypothesis_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        thesis TEXT NOT NULL,
        sport TEXT NOT NULL,
        market_type TEXT NOT NULL,
        model_config TEXT NOT NULL,
        edge_threshold REAL NOT NULL DEFAULT 0.01,
        status TEXT NOT NULL DEFAULT 'draft',
        min_sample_size INTEGER NOT NULL DEFAULT 50,
        significance_level REAL NOT NULL DEFAULT 0.05,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        promoted_at DATETIME,
        promoted_by TEXT,
        notes TEXT
    )""",
    """CREATE TABLE backtest_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        event_id TEXT NOT NULL,
        hypothesis_id TEXT NOT NULL,
        sport TEXT,
        player TEXT,
        market TEXT,
        line REAL,
        side TEXT,
        book TEXT,
        book_odds_american INTEGER,
        book_implied_prob REAL,
        model_fair_prob REAL,
        model_factors TEXT,
        edge REAL,
        ev_pct REAL,
        kelly_fraction REAL,
        signal_generated BOOLEAN DEFAULT FALSE,
        actual_result TEXT,
        actual_stat REAL,
        closing_odds INTEGER,
        closing_implied REAL,
        clv_implied REAL,
        game_date DATE,
        snapshot_time DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE paper_trades (
        trade_id TEXT PRIMARY KEY,
        hypothesis_id TEXT NOT NULL,
        event_id TEXT,
        sport TEXT,
        player TEXT,
        market TEXT,
        line REAL,
        side TEXT,
        book TEXT,
        signal_time DATETIME,
        signal_odds_american INTEGER,
        signal_implied_prob REAL,
        model_fair_prob REAL,
        edge REAL,
        ev_pct REAL,
        kelly_fraction REAL,
        recommended_stake REAL,
        closing_odds INTEGER,
        closing_implied REAL,
        clv_implied REAL,
        actual_result TEXT,
        actual_stat REAL,
        hypothetical_pnl REAL,
        game_date DATE,
        home_team TEXT,
        away_team TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE hypothesis_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hypothesis_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        computed_at DATETIME NOT NULL,
        total_n INTEGER,
        signals_n INTEGER,
        win INTEGER, loss INTEGER, push_ INTEGER,
        hit_rate REAL, avg_edge REAL, avg_ev REAL, avg_clv REAL,
        positive_clv_rate REAL, roi_pct REAL, sharpe REAL, max_drawdown REAL,
        p_value REAL, is_significant BOOLEAN,
        sortino REAL, brier_score REAL, information_coefficient REAL
    )""",
    """CREATE TABLE backtest_runs (
        run_id TEXT PRIMARY KEY,
        hypothesis_id TEXT NOT NULL,
        started_at DATETIME,
        completed_at DATETIME,
        total_events INTEGER,
        signals_generated INTEGER,
        actual_win INTEGER,
        actual_loss INTEGER,
        hit_rate REAL,
        avg_edge REAL,
        avg_ev REAL
    )""",
    """CREATE TABLE clv_log (
        bet_id TEXT PRIMARY KEY,
        event TEXT,
        outcome TEXT,
        point REAL,
        book TEXT,
        our_odds_decimal REAL,
        pinnacle_close_fair_prob REAL,
        pinnacle_close_fair_decimal REAL,
        clv_cents REAL,
        clv_prob_bp REAL,
        actual_result TEXT,
        actual_pnl REAL,
        close_reliable BOOLEAN,
        logged_at TEXT,
        regime_phase_at_placement TEXT
    )""",
]


async def make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    for stmt in SCHEMA:
        await db.execute(stmt)
    await db.commit()
    return db


def days_ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def insert_hypothesis(
    db: aiosqlite.Connection,
    hid: str = "h-redteam",
    *,
    status: str = "paper_trading",
    edge_threshold: float = 0.03,
    model_config: dict | None = None,
    promoted_days_ago: float = 14.0,
) -> str:
    import json

    await db.execute(
        "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type,"
        " model_config, edge_threshold, status, min_sample_size,"
        " significance_level, created_at, updated_at, promoted_at, promoted_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hid,
            f"redteam_{hid}",
            "Red team test thesis",
            "baseball_mlb",
            "h2h",
            json.dumps(model_config or {}),
            edge_threshold,
            status,
            50,
            0.05,
            days_ago_iso(promoted_days_ago + 30),
            days_ago_iso(promoted_days_ago),
            days_ago_iso(promoted_days_ago),
            "redteam",
        ),
    )
    await db.commit()
    return hid


async def insert_backtest_event(
    db: aiosqlite.Connection,
    hid: str,
    event_id: str,
    *,
    run_id: str = "run-1",
    edge: float = 0.04,
    signal: int = 1,
    result: str | None = "won",
    odds: int = -110,
    implied: float = 0.524,
    fair: float = 0.560,
    game_date: str = "2026-08-01",
    model_factors: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, sport,"
        " market, side, book, book_odds_american, book_implied_prob,"
        " model_fair_prob, model_factors, edge, ev_pct, signal_generated,"
        " actual_result, game_date, snapshot_time)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, event_id, hid, "baseball_mlb", "h2h", "Home", "draftkings",
            odds, implied, fair, model_factors, edge, 5.0, signal, result,
            game_date, game_date + "T00:00:00+00:00",
        ),
    )


async def insert_paper_trade(
    db: aiosqlite.Connection,
    hid: str,
    trade_id: str,
    *,
    result: str = "won",
    odds: int = -110,
    edge: float = 0.04,
    signal_implied: float = 0.524,
    closing_implied: float | None = None,
    clv_implied: float | None = None,
    game_key: str = "G1",
    days_ago: int = 3,
) -> None:
    game_date = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%d")
    await db.execute(
        "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport,"
        " market, side, book, signal_time, signal_odds_american,"
        " signal_implied_prob, model_fair_prob, edge, ev_pct, closing_odds,"
        " closing_implied, clv_implied, actual_result, hypothetical_pnl,"
        " game_date, home_team, away_team)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trade_id, hid, f"E-{trade_id}", "baseball_mlb", "h2h", "Home",
            "draftkings", days_ago_iso(days_ago), odds, signal_implied, 0.55,
            edge, 5.0, -108 if closing_implied is not None else None,
            closing_implied if closing_implied is not None else 0.520,
            clv_implied if clv_implied is not None else 0.004,
            result, 90.0 if result == "won" else (-100.0 if result == "lost" else 0.0),
            game_date, f"Home{game_key}", f"Away{game_key}",
        ),
    )
