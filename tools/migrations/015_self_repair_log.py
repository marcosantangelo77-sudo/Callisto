"""Migration 015: ``self_repair_log`` — durable audit of every auto-recovery.

Why this exists
---------------
``tools.self_repair.SelfRepairEngine`` previously only recorded outcomes to
Hermes memory under a per-key summary. That's fine for "did we fix it this
cycle?" but useless for operators asking:

  * "How many times did the DB-lock recovery trip in the last 24h?"
  * "When did we last force-checkpoint the WAL, and did it succeed?"
  * "Is the orphaned-PROCESSING-task reaper actually firing, or silently
    losing to its circuit breaker?"

This table is the one-row-per-invocation ledger — success or failure, which
recovery fired, what it touched. The ``/admin/self-repair/status`` endpoint
reads the latest row per ``recovery_name`` to surface cooldowns and last
outcomes; the manual ``/admin/self-repair/trigger/{name}`` path writes the
same row shape so manual invocations show up in the same audit surface.

Schema choices
--------------
* ``recovery_name`` is a stable slug (not free-text) — every new recovery
  registers its name explicitly in ``SelfRepairEngine._RECOVERIES``. Renaming
  a slug later loses the history for that recovery's pre-rename runs.
* ``trigger`` distinguishes ``auto`` (detector-driven) from ``manual``
  (admin endpoint). Useful when debugging "did the loop fire this, or did
  I push the button?"
* ``success`` / ``action`` / ``detail`` mirror the existing repair-result
  dict shape so the engine can INSERT a row from the same object it already
  builds for the Hermes log.
* ``metadata_json`` is free-form for recovery-specific context (e.g. the
  number of stuck tasks marked FAILED, the sport that the scraper fallback
  targeted). Never rely on its shape across recoveries.

Indexes
-------
1. ``(recovery_name, invoked_at DESC)`` — the status endpoint's primary
   lookup ("last N runs for recovery X"). Also supports cooldown reads
   where we need the single newest row per recovery.
2. ``(invoked_at DESC)`` — a straight timeline scan for dashboards.

Down migration
--------------
Drops cleanly. The Hermes record remains as a secondary audit path, so no
operational history is lost if the table has to be rebuilt.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS self_repair_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recovery_name TEXT NOT NULL,
            trigger TEXT NOT NULL DEFAULT 'auto'
                CHECK (trigger IN ('auto', 'manual')),
            success INTEGER NOT NULL DEFAULT 0,
            action TEXT,
            detail TEXT,
            metadata_json TEXT,
            invoked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            elapsed_ms REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_repair_log_name_time "
        "ON self_repair_log(recovery_name, invoked_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_self_repair_log_time "
        "ON self_repair_log(invoked_at DESC)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_self_repair_log_time")
    conn.execute("DROP INDEX IF EXISTS idx_self_repair_log_name_time")
    conn.execute("DROP TABLE IF EXISTS self_repair_log")
