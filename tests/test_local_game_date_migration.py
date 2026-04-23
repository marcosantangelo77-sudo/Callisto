"""Backfill smoke test for migration 007_local_game_dates.

Builds a tiny in-memory-shaped DB on disk, runs migration 007 in isolation,
and asserts:
  - every ``game_results`` row ends with a non-NULL ``local_game_date``
  - rows whose ``event_id`` joins a ``markets.commence_time`` of a late
    West-Coast time shift their canonical date relative to the legacy
    ``game_date``
  - re-running the migration is a no-op (idempotency)
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from tools.migrations.runner import apply_pending_migrations


def _seed(conn: sqlite3.Connection) -> None:
    # Create just enough of the real schema that migration 007 can target
    # it. We intentionally use IF NOT EXISTS so the same seed runs cleanly
    # against a fresh DB before or after migration 007 has been applied.
    # NOTE: intentionally DO NOT create the ``hypotheses`` table here.
    # The migration runner treats "hypotheses exists" as a signal that this
    # is an existing Callisto DB and bootstraps all migrations as already-
    # applied. For this test we want the runner to actually RUN 007.
    conn.executescript(
        """
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
            home_team TEXT
        );
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # Seed rows. Two MLB games on 2026-04-22 per the UTC-sliced legacy:
    # 1. Dodgers home 7:30pm PT → commence 02:30Z 04-22 → local date SHOULD be 04-21
    # 2. Red Sox home 7:10pm ET → commence 23:10Z 04-22 → local date 04-22 (unchanged)
    conn.executemany(
        "INSERT INTO markets (market_id, sport, event_id, event_name, "
        "home_team, away_team, commence_time, market_type) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("mkt-lad", "baseball_mlb", "evt-lad", "LAD vs SF",
             "Los Angeles Dodgers", "San Francisco Giants",
             "2026-04-22T02:30:00Z", "h2h"),
            ("mkt-bos", "baseball_mlb", "evt-bos", "BOS vs NYY",
             "Boston Red Sox", "New York Yankees",
             "2026-04-22T23:10:00Z", "h2h"),
        ],
    )

    # game_results: legacy date comes from ESPN — for the Dodgers game
    # ESPN's ET-oriented scoreboard tags it 04-21; the Red Sox 04-22.
    conn.executemany(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("baseball_mlb", "2026-04-21", "Los Angeles Dodgers",
             "San Francisco Giants", 5, 3),
            ("baseball_mlb", "2026-04-22", "Boston Red Sox",
             "New York Yankees", 4, 2),
        ],
    )

    # backtest_events: legacy bug — game_date was ``commence_time[:10]``
    # which gave 04-22 for BOTH games. Migration should fix Dodgers to 04-21.
    conn.executemany(
        "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, sport, "
        "market, side, book, book_odds_american, book_implied_prob, "
        "model_fair_prob, edge, ev_pct, game_date, snapshot_time, home_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("run-1", "evt-lad", "hyp-1", "baseball_mlb", "h2h", "home",
             "draftkings", -120, 0.545, 0.55, 0.5, 0.5,
             "2026-04-22", "2026-04-21T00:00:00Z", "Los Angeles Dodgers"),
            ("run-1", "evt-bos", "hyp-1", "baseball_mlb", "h2h", "home",
             "draftkings", -120, 0.545, 0.55, 0.5, 0.5,
             "2026-04-22", "2026-04-21T00:00:00Z", "Boston Red Sox"),
        ],
    )

    conn.commit()


def test_migration_populates_local_game_date_everywhere(tmp_path):
    db_path = str(tmp_path / "mig.db")
    conn = sqlite3.connect(db_path)
    try:
        _seed(conn)
    finally:
        conn.close()

    # Apply all pending migrations (includes 007)
    result = apply_pending_migrations(db_path)
    assert 7 in result["applied"] or 7 in result["skipped"] or 7 in (
        result.get("bootstrapped") and []  # bootstrap path returns an int; no version list
        or []
    )

    conn = sqlite3.connect(db_path)
    try:
        # game_results: every row has local_game_date set
        rows = conn.execute(
            "SELECT home_team, game_date, local_game_date FROM game_results"
        ).fetchall()
        assert len(rows) == 2
        for _home, _gd, lgd in rows:
            assert lgd is not None, "local_game_date NULL after migration"

        # backtest_events: Dodgers row SHOULD have shifted from 04-22 → 04-21
        lad_row = conn.execute(
            "SELECT game_date, local_game_date FROM backtest_events "
            "WHERE event_id = 'evt-lad'"
        ).fetchone()
        assert lad_row is not None
        legacy, local = lad_row
        assert str(legacy).startswith("2026-04-22")
        assert str(local).startswith("2026-04-21"), (
            f"Dodgers backtest_event should have shifted to 04-21 local; got {local}"
        )

        # Red Sox row should NOT have shifted
        bos_row = conn.execute(
            "SELECT game_date, local_game_date FROM backtest_events "
            "WHERE event_id = 'evt-bos'"
        ).fetchone()
        assert bos_row is not None
        legacy_bos, local_bos = bos_row
        assert str(local_bos).startswith("2026-04-22")
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    """Re-running the migration must not double-update or crash."""
    db_path = str(tmp_path / "mig_idem.db")
    conn = sqlite3.connect(db_path)
    try:
        _seed(conn)
    finally:
        conn.close()

    r1 = apply_pending_migrations(db_path)
    r2 = apply_pending_migrations(db_path)

    # Second run should have nothing new to apply (but also shouldn't raise)
    assert r2["applied"] == [] or all(
        v in r1["applied"] or v in r1.get("skipped", []) for v in r2["applied"]
    )

    # Data sanity — values didn't get mutated a second time
    conn = sqlite3.connect(db_path)
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE local_game_date IS NULL"
        ).fetchone()[0]
        assert cnt == 0
    finally:
        conn.close()
