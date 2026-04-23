"""Migration 007: orders FSM table for Telegram-approved manual placement.

Supersedes the Playwright ``bets`` path as the canonical order ledger. Each
order row is a durable FSM record joining hypothesis -> signal -> odds
snapshot -> stake -> book -> state. ``bets`` and ``executor_log`` stay
populated during the transition so CLV and audit tooling keep working.

State machine (append-only in ``state_history_json``). See
:data:`tools.order_manager.ALLOWED_TRANSITIONS` for the authoritative edges.
Terminal states: ``rejected``, ``cancelled``, ``expired``,
``settled_win``, ``settled_loss``, ``settled_push``.

Idempotency key: (hypothesis_id, signal_id). The uniqueness is enforced by
a partial index so historical rows that predate the signal_id field (NULL)
don't trip the constraint.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
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
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_state "
        "ON orders(state, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_hypothesis "
        "ON orders(hypothesis_id, created_at DESC)"
    )
    # Idempotency: no two open orders for the same (hypothesis, signal). We
    # use a partial UNIQUE so NULL signal_id rows (legacy/backfill) don't
    # collide. Settled/rejected/cancelled/expired orders are excluded so a
    # fresh resubmit after rejection is permitted.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_signal_open
        ON orders(hypothesis_id, signal_id)
        WHERE signal_id IS NOT NULL
          AND state IN ('pending_approval','approved','submitted','filled')
        """
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS uq_orders_signal_open")
    conn.execute("DROP INDEX IF EXISTS idx_orders_hypothesis")
    conn.execute("DROP INDEX IF EXISTS idx_orders_state")
    conn.execute("DROP TABLE IF EXISTS orders")
