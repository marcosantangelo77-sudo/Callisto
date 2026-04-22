"""Migration 001: initial schema marker.

This migration is intentionally a no-op on the ``up()`` side. The canonical
pre-framework schema is whatever ``ensure_schema()`` (SCHEMA_SQL + the
legacy _safe_add_column loop) produced. Rather than duplicate that 1300-line
DDL blob here and risk drift, we treat v001 as the baseline:

- Fresh DB: ``ensure_schema()`` runs first and creates all base tables, then
  ``apply_pending_migrations`` runs and records v001 as applied.
- Existing DB: the migration runner's ``bootstrap_existing_db`` notices
  ``hypotheses`` already exists and seeds v001 (and every other migration)
  with ``bootstrap=1, applied_at=NULL`` so nothing re-runs.

Future versioned schema changes (v002+) hold the real DDL.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    # No-op. ensure_schema() handles the v1 baseline for fresh DBs.
    pass


def down(conn: sqlite3.Connection) -> None:
    raise NotImplementedError(
        "Cannot roll back initial schema — drop the DB file instead."
    )
