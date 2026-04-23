"""Migration 006: indexes & columns to support the portfolio-correlation gate.

Context (2026-04-22 FWER/per-game-dedup audit):
  check_promotion_readiness now runs `_compute_portfolio_overlap`, which
  scans ``backtest_events`` twice per LIVE hypothesis in the window for
  every promotion candidate. Without an appropriate index the gate is
  O(N^2) over the full event table.

Additions:
  1. Index on (hypothesis_id, signal_generated, game_date) for the
     overlap scan. Co-located with the existing event_id index from
     migration 003 so per-event DISTINCT stays cheap.
  2. ``promotion_audit`` table — every promotion decision is logged with
     the portfolio-overlap map as JSON so we can audit historical
     correlation decisions post-hoc without re-running the gate.

Idempotent: all CREATE statements use IF NOT EXISTS.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_columns(conn: sqlite3.Connection, table: str, cols: tuple[str, ...]) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {r[1] for r in rows}
    return all(c in existing for c in cols)


def up(conn: sqlite3.Connection) -> None:
    # backtest_events may not exist yet on fresh installs — its creation is
    # owned by memory.py::init_db. Also skip if the required columns aren't
    # present (early-version schema in test harnesses). memory.py will
    # (re)create the full table on next start; the index is additive.
    if _table_exists(conn, "backtest_events") and _has_columns(
        conn, "backtest_events",
        ("hypothesis_id", "signal_generated", "game_date"),
    ):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_backtest_events_hyp_signal_date "
            "ON backtest_events(hypothesis_id, signal_generated, game_date)"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS promotion_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL,
            from_stage TEXT NOT NULL,
            to_stage TEXT NOT NULL,
            decision TEXT NOT NULL,        -- 'promoted' | 'rejected' | 'held'
            reason TEXT,                   -- e.g. 'portfolio_correlation_too_high'
            portfolio_overlap_json TEXT,   -- {live_id: pct, …} snapshot
            fwer_n INTEGER,                -- FWER denominator at decision time
            p_value REAL,
            p_threshold REAL,
            decided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_promotion_audit_hyp_time "
        "ON promotion_audit(hypothesis_id, decided_at DESC)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_promotion_audit_hyp_time")
    conn.execute("DROP TABLE IF EXISTS promotion_audit")
    conn.execute("DROP INDEX IF EXISTS idx_backtest_events_hyp_signal_date")
