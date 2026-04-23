"""Migration 003: index on ``backtest_events(event_id)``.

Audit finding: ``api.py:~2797`` runs
``SELECT COUNT(DISTINCT event_id) FROM backtest_events`` on every call to
``/system/full-status`` (and several /odds endpoints). With 112k rows, no
index on ``event_id`` alone, and only a composite ``(run_id, game_date)``
index, the query falls back to a full scan.

Before:
    SCAN backtest_events                 (~112k rows)
After:
    SCAN backtest_events USING COVERING INDEX idx_bt_events_event_id

The index is not UNIQUE — multiple rows per event_id are expected (one per
book/side/line snapshot). A covering index on the single column is enough
for ``COUNT(DISTINCT event_id)`` to be satisfied purely from the index.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    # Table may not exist on a DB that predates backtest support.
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_events'"
    ).fetchone()
    if not row:
        return
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bt_events_event_id "
        "ON backtest_events(event_id)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_bt_events_event_id")
