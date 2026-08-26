"""tools.followup.schema — migration + row lookup + insert helpers.

The schema migration adds four columns to ``task_queue``:
  - ``followup_depth``  (INTEGER DEFAULT 0)  — 0 for user-initiated
  - ``parent_task_id``  (INTEGER)            — direct parent, or NULL
  - ``root_task_id``    (INTEGER)            — 0-depth ancestor, always self for depth=0
  - ``cost_usd``        (REAL DEFAULT 0)     — Claude escalation cost for this task
"""

from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.followup_guard")


# ── Schema migration ─────────────────────────────────────────────────────

async def ensure_followup_columns(db: aiosqlite.Connection) -> None:
    """Idempotently add followup bookkeeping columns to ``task_queue``.

    Safe to call on every startup — identical contract to
    ``tools.schema._safe_add_column``. We intentionally re-implement the
    "already exists" swallow here rather than importing to avoid a circular
    import between ``tools.schema`` (which calls into multiple subsystems)
    and the followup guard.
    """
    cols = [
        ("followup_depth", "INTEGER NOT NULL DEFAULT 0"),
        ("parent_task_id", "INTEGER"),
        ("root_task_id", "INTEGER"),
        ("cost_usd", "REAL NOT NULL DEFAULT 0"),
    ]
    for col, coltype in cols:
        try:
            await db.execute(f"ALTER TABLE task_queue ADD COLUMN {col} {coltype}")
            await db.commit()
            logger.info("followup_guard: added task_queue.%s", col)
        except Exception as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                continue
            logger.warning(
                "followup_guard: failed to add task_queue.%s: %r", col, e
            )

    # Back-fill root_task_id for depth=0 rows where it's NULL so the chain
    # queries can rely on root_task_id being populated. This is a one-shot
    # no-op once the column is already filled.
    try:
        await db.execute(
            "UPDATE task_queue SET root_task_id = task_id "
            "WHERE root_task_id IS NULL AND followup_depth = 0"
        )
        await db.commit()
    except Exception as e:
        logger.debug("followup_guard: root backfill skipped: %r", e)

    # Helpful index for chain lookups. CREATE IF NOT EXISTS is safe.
    try:
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_chain "
            "ON task_queue(root_task_id, followup_depth)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_queue_parent "
            "ON task_queue(parent_task_id)"
        )
        await db.commit()
    except Exception as e:
        logger.debug("followup_guard: index create skipped: %r", e)


# ── Row lookup ───────────────────────────────────────────────────────────

async def get_task_meta(
    db: aiosqlite.Connection, task_id: int
) -> Optional[dict]:
    """Fetch the followup bookkeeping fields for ``task_id``.

    Returns a dict with keys ``task_id, query, followup_depth,
    parent_task_id, root_task_id, cost_usd, created_at`` or ``None`` if
    the row doesn't exist / predates the migration.
    """
    try:
        # Refresh WAL snapshot so we see rows committed by the worker
        # coordinator.
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query, followup_depth, parent_task_id, "
            "root_task_id, cost_usd, created_at "
            "FROM task_queue WHERE task_id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0],
            "query": row[1],
            "followup_depth": row[2] or 0,
            "parent_task_id": row[3],
            "root_task_id": row[4] or row[0],
            "cost_usd": row[5] or 0.0,
            "created_at": row[6],
        }
    except Exception as e:
        # Pre-migration DB — columns don't exist yet. Treat the task as
        # a depth-0 root so followup semantics still work. The migration
        # is run at startup so this should only fire during tests that
        # skip ensure_schema().
        logger.debug("get_task_meta fallback (pre-migration?): %r", e)
        try:
            cur = await db.execute(
                "SELECT task_id, query, created_at "
                "FROM task_queue WHERE task_id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "task_id": row[0],
                "query": row[1],
                "followup_depth": 0,
                "parent_task_id": None,
                "root_task_id": row[0],
                "cost_usd": 0.0,
                "created_at": row[2],
            }
        except Exception:
            return None


# ── Insert helper ────────────────────────────────────────────────────────

async def insert_followup(
    db: aiosqlite.Connection,
    query: str,
    priority: int,
    parent_task_id: int,
    root_task_id: int,
    depth: int,
    cost_usd: float = 0.0,
) -> int:
    """Insert a task row with the followup bookkeeping populated. Returns task_id.

    Callers that want the WriteCoordinator path should use the TaskQueue
    abstraction instead; this is here for tests and for direct use from
    the worker when the coordinator isn't running.
    """
    cur = await db.execute(
        "INSERT INTO task_queue "
        "(query, priority, created_at, followup_depth, parent_task_id, root_task_id, cost_usd) "
        "VALUES (?, ?, datetime('now'), ?, ?, ?, ?)",
        (query, priority, depth, parent_task_id, root_task_id, cost_usd),
    )
    await db.commit()
    return int(cur.lastrowid)
