"""Versioned migration framework for Callisto.

Problem this solves
-------------------
Pre-fix, schema evolution happened two ways, both lossy:

1. ``ensure_schema()`` split a 1300-line DDL blob on ``;`` and swallowed every
   per-statement exception. A typo, a missing column, a failing CREATE INDEX —
   all vanished silently. The schema was "whatever survived."

2. Ad-hoc ``ALTER TABLE ADD COLUMN`` calls sprinkled across modules (most
   notably ``cache_manager.rotate_caches`` running on every rotation cycle),
   wrapped in ``except Exception: pass``. The write coordinator saw these as
   writes, routed them, they failed with ``duplicate column name``, and
   ``writes_failed`` incremented by 4 every rotation. 23 of 28,394 writes lost
   this way per the data-layer audit.

This module introduces:

- A ``migrations/`` directory with one Python file per version:
  ``001_initial.py``, ``002_add_archived_columns.py``, etc. Each defines an
  ``up(conn)`` and optional ``down(conn)``.
- ``apply_pending_migrations(db_path)`` reads ``schema_migrations``, finds
  versions whose Python file exists but whose row is missing, and runs them
  in order. Each migration commits independently; a failure halts the run
  and leaves previously-applied migrations durable.
- Exclusive locking via ``BEGIN EXCLUSIVE`` on a dedicated ``_migration_lock``
  table so two concurrent processes (watchdog restart + manual ensure) can't
  both try to apply the same migration.
- Bootstrap mode: on first run against an existing DB that already has the
  target tables, seed ``schema_migrations`` with ``applied_at=NULL,
  bootstrap=1`` for every known migration so they don't re-run.

DDL runs on a **dedicated autocommit sqlite3 connection**, never through
the write coordinator. This is the same pattern ``vacuum_db`` uses.
"""

from __future__ import annotations

from .runner import (
    Migration,
    apply_pending_migrations,
    bootstrap_existing_db,
    discover_migrations,
    ensure_migration_table,
    get_applied_versions,
)

__all__ = [
    "Migration",
    "apply_pending_migrations",
    "bootstrap_existing_db",
    "discover_migrations",
    "ensure_migration_table",
    "get_applied_versions",
]
