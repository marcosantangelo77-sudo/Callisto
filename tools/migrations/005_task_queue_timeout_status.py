"""Migration 005: add ``TIMEOUT`` to task_queue.status CHECK constraint.

The original schema (task_queue.py TASK_SCHEMA_SQL) pinned status to
``('PENDING','PROCESSING','COMPLETED','FAILED')``. With the adaptive
orchestrator-timeout rollout (feat/adaptive-orchestrator-timeout), we want
a distinct ``TIMEOUT`` value so downstream code can:
  - tell "we ran out of wall-clock time" apart from "the session raised"
  - decide whether to retry at a higher budget
  - feed the SLA watchdog with which task_type buckets routinely time out

SQLite doesn't support ``ALTER TABLE ... ALTER CONSTRAINT`` — we have to
rebuild. Standard 12-step table rebuild:

  1. Turn off foreign_keys
  2. Create new table with relaxed CHECK
  3. Copy rows
  4. Drop old table
  5. Rename new table
  6. Recreate indexes
  7. Turn foreign_keys back on

The operation is wrapped in a single transaction by the migration runner.

This migration is idempotent: if the new CHECK is already present we skip.
"""

from __future__ import annotations

import sqlite3


NEW_SCHEMA_SQL = """
CREATE TABLE task_queue_new (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'TIMEOUT')),
    priority INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);
"""


def _current_check_allows_timeout(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_queue'"
    ).fetchone()
    if not row:
        return True  # no table, nothing to do — treated as "already fine"
    sql = row[0] or ""
    return "TIMEOUT" in sql


def up(conn: sqlite3.Connection) -> None:
    # No table? Newer installs get the TIMEOUT-aware schema from
    # task_queue.py::initialize (updated in this same feature). Skip.
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_queue'"
    ).fetchone()
    if not row:
        return

    if _current_check_allows_timeout(conn):
        return  # already migrated

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(NEW_SCHEMA_SQL)
        conn.execute(
            """INSERT INTO task_queue_new
               (task_id, query, status, priority, result, error, session_id,
                created_at, started_at, completed_at)
               SELECT task_id, query, status, priority, result, error, session_id,
                      created_at, started_at, completed_at
               FROM task_queue"""
        )
        conn.execute("DROP TABLE task_queue")
        conn.execute("ALTER TABLE task_queue_new RENAME TO task_queue")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_poll "
            "ON task_queue(status, priority DESC, created_at)"
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def down(conn: sqlite3.Connection) -> None:
    # Rolling back would require any TIMEOUT rows be coerced to FAILED first.
    # Do it by hand if ever needed — an automatic path risks silent data
    # mutation on a live DB.
    raise NotImplementedError(
        "Rollback of 005_task_queue_timeout_status is manual — any TIMEOUT "
        "rows must first be migrated to FAILED, then the table rebuilt with "
        "the original CHECK constraint."
    )
