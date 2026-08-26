"""Shared constants and schema DDL for the orders subsystem."""

from __future__ import annotations

import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

ORDER_EXPIRY_MIN = int(os.getenv("CALLISTO_ORDER_EXPIRY_MIN", "10"))
# Backwards-compat toggle. 1 (default here) routes through order_manager; 0
# falls back to the legacy Playwright path in bet_executor.
USE_ORDER_MANAGER = os.getenv("CALLISTO_USE_ORDER_MANAGER", "1") == "1"

CREATE_ORDERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    signal_id TEXT,
    odds_snapshot_id INTEGER,
    sport TEXT,
    event_id TEXT,
    market TEXT,
    side TEXT,
    price_american INTEGER,
    stake_units REAL,
    stake_dollars REAL,
    state TEXT NOT NULL,
    state_history_json TEXT NOT NULL DEFAULT '[]',
    book TEXT,
    placed_at TIMESTAMP,
    settled_at TIMESTAMP,
    pnl_dollars REAL,
    telegram_msg_id TEXT,
    expires_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    bet_id INTEGER,
    edge REAL,
    fair_prob REAL,
    game_description TEXT,
    notes TEXT
)
"""

ORDERS_INDEXES_SQL = (
    """
    CREATE INDEX IF NOT EXISTS idx_orders_state
    ON orders(state, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_orders_hypothesis
    ON orders(hypothesis_id, created_at DESC)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_signal_open
    ON orders(hypothesis_id, signal_id)
    WHERE signal_id IS NOT NULL
    AND state IN ('pending_approval','approved','submitted','filled')
    """,
)

INSERT_ORDER_SQL = """
INSERT INTO orders (
    order_id, hypothesis_id, signal_id, odds_snapshot_id,
    sport, event_id, market, side, price_american,
    stake_units, stake_dollars, state, state_history_json,
    book, expires_at, created_at, edge, fair_prob,
    game_description
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

OPEN_STATES_SQL = (
    "SELECT order_id FROM orders "
    "WHERE hypothesis_id = ? AND signal_id = ? "
    "AND state IN (?, ?, ?, ?)"
)
