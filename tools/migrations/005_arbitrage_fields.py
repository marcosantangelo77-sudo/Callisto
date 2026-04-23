"""Migration 005: add ``thesis_tag`` and ``expires_at`` to ``ev_opportunities``.

The arbitrage scanner (``tools/arbitrage_scanner.py``) writes one row per
leg with ``source='arbitrage'``. Consumers (executor, Telegram alerts) need
two things the base schema doesn't provide:

    - ``thesis_tag``   — ``'arb'`` | ``'dutch'`` | ``'synthetic_arb'``, so
                         filters can drop synthetic (higher-risk) rows and
                         keep only provable pure/dutch arbs.
    - ``expires_at``   — ISO timestamp. Arbs evaporate fast; a row detected
                         at T should refuse to execute past T+60s. Baking
                         the TTL into the row keeps the check O(1).

Both columns are nullable and have no CHECK constraint — older rows from
the line-movement pipeline simply leave them as NULL.

A partial-index on ``(source, status, expires_at)`` keeps the "hot open
arbs" query cheap as the table grows: all queries for in-flight arbs filter
by ``source='arbitrage' AND status='open'`` and want the freshest rows.
"""

from __future__ import annotations

import sqlite3


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == col:
            return True
    return False


def up(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ev_opportunities'"
    ).fetchone()
    if not row:
        # Table is created by tools/line_monitor.py at runtime; migration is
        # a no-op if it hasn't run yet. The runtime DDL includes the new
        # columns via persist_opportunity's defensive ALTERs, so this only
        # matters for long-lived DBs already past that code path.
        return

    if not _column_exists(conn, "ev_opportunities", "thesis_tag"):
        conn.execute("ALTER TABLE ev_opportunities ADD COLUMN thesis_tag TEXT")
    if not _column_exists(conn, "ev_opportunities", "expires_at"):
        conn.execute("ALTER TABLE ev_opportunities ADD COLUMN expires_at TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ev_open_arbs "
        "ON ev_opportunities(source, status, expires_at)"
    )


def down(conn: sqlite3.Connection) -> None:
    # SQLite can't DROP COLUMN pre-3.35; we intentionally do not rebuild the
    # table on downgrade — ``thesis_tag``/``expires_at`` staying as NULL
    # columns is harmless for the line-movement pipeline.
    conn.execute("DROP INDEX IF EXISTS idx_ev_open_arbs")
