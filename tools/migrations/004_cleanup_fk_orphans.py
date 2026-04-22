"""Migration 004: clean up dangling foreign-key rows before
``PRAGMA foreign_keys=ON`` becomes a default.

Audit finding: ``tools/schema.py:34-40`` does NOT set
``PRAGMA foreign_keys=ON`` on the main DB (``memory.py:106`` does it only
for hermes memory). Before turning it on for ``open_db`` we have to make
sure existing rows with broken FKs won't cause writes to suddenly fail.

Live DB snapshot (2026-04-21): 1 orphan row in
``hypothesis_stats`` pointing at a hypothesis_id that no longer exists;
all other FK-declaring tables were clean. This migration deletes orphans
for every declared FK. If zero rows match, the UPDATE is a no-op.

Why delete vs. relink: hypothesis_stats is a derived aggregation table; a
row whose parent hypothesis vanished is pure garbage — the hypothesis was
retired and its stats should have gone with it. Losing 1 row does not
damage the pipeline.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("callisto.migrations.004")

# (child_table, parent_table, fk_column). Derived from SCHEMA_SQL
# FOREIGN KEY declarations in tools/schema.py.
FK_RELATIONS = (
    ("hypothesis_stats", "hypotheses", "hypothesis_id"),
    ("backtest_runs", "hypotheses", "hypothesis_id"),
    ("backtest_events", "hypotheses", "hypothesis_id"),
    ("backtest_events", "backtest_runs", "run_id"),
    ("paper_trades", "hypotheses", "hypothesis_id"),
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def up(conn: sqlite3.Connection) -> None:
    total_deleted = 0
    for child, parent, fk in FK_RELATIONS:
        if not _table_exists(conn, child) or not _table_exists(conn, parent):
            continue
        # Count first so the log line tells us whether the migration
        # actually did anything. A clean DB on first run should report
        # zeros across the board.
        orphans = conn.execute(
            f"SELECT COUNT(*) FROM {child} "
            f"WHERE {fk} NOT IN (SELECT {fk} FROM {parent})"
        ).fetchone()[0]
        if orphans:
            conn.execute(
                f"DELETE FROM {child} "
                f"WHERE {fk} NOT IN (SELECT {fk} FROM {parent})"
            )
            logger.warning(
                f"Deleted {orphans} orphan row(s) from {child}.{fk} "
                f"(no matching {parent}.{fk})."
            )
            total_deleted += orphans
    if total_deleted:
        logger.warning(f"FK-orphan cleanup removed {total_deleted} row(s) total.")


def down(conn: sqlite3.Connection) -> None:
    # Deleted rows cannot be restored.
    raise NotImplementedError("FK-orphan deletes are not reversible.")
