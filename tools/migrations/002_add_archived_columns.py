"""Migration 002: add `archived` column to cache-rotation targets.

Pre-fix, ``tools/cache_manager.py:rotate_caches`` ran
``ALTER TABLE ... ADD COLUMN archived BOOLEAN DEFAULT 0`` on every rotation
cycle for bets/ev_opportunities/line_movements/odds_snapshots. The ALTERs
were wrapped in ``except Exception: pass`` because after the first run they
hit ``duplicate column name``. The write coordinator routed each of them
anyway, incremented ``writes_failed`` 4× per cycle, and logged nothing.
That accounted for 23 of 28,394 failed writes in the audit window.

Post-fix, ``rotate_caches`` assumes the column exists (this migration puts
it there once, on DB upgrade) and no longer issues ALTERs at runtime. The
data-rotation UPDATE still runs every cycle; only the schema DDL is
moved here.
"""

from __future__ import annotations

import sqlite3

TABLES = ("bets", "ev_opportunities", "line_movements", "odds_snapshots")


def up(conn: sqlite3.Connection) -> None:
    for table in TABLES:
        # Check the table exists first — some deployments don't have every
        # one of these tables (e.g. if a feature was never enabled). Don't
        # error; just skip.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not row:
            continue

        # Idempotent ADD COLUMN. A hand-edited DB that already has the
        # column must not fail the migration.
        already_has = any(
            r[1] == "archived"
            for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if already_has:
            continue
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN archived BOOLEAN DEFAULT 0"
        )


def down(conn: sqlite3.Connection) -> None:
    # SQLite DROP COLUMN is supported from 3.35+ but risks data loss.
    # If a rollback is ever needed, do it manually with a table rebuild.
    raise NotImplementedError(
        "Rollback of 002_add_archived_columns is manual — SQLite DROP COLUMN "
        "is supported but a table rebuild is safer."
    )
