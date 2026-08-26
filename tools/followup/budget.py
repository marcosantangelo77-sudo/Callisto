"""tools.followup.budget — fan-out and chain-budget checks plus cost recording."""

from __future__ import annotations

import logging

import aiosqlite

logger = logging.getLogger("callisto.followup_guard")


async def count_direct_followups(
    db: aiosqlite.Connection, parent_task_id: int
) -> int:
    """Return how many direct-child followups this parent has already spawned."""
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT COUNT(*) FROM task_queue WHERE parent_task_id = ?",
            (parent_task_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_direct_followups failed: %r", e)
        return 0


async def chain_cost_usd(
    db: aiosqlite.Connection, root_task_id: int
) -> float:
    """Return the sum of ``cost_usd`` across the chain rooted at ``root_task_id``."""
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM task_queue "
            "WHERE root_task_id = ?",
            (root_task_id,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as e:
        logger.debug("chain_cost_usd failed: %r", e)
        return 0.0


async def record_task_cost(
    db: aiosqlite.Connection, task_id: int, cost_usd: float
) -> None:
    """Idempotently set cost_usd on a task row. Safe on pre-migration DBs."""
    try:
        await db.execute(
            "UPDATE task_queue SET cost_usd = ? WHERE task_id = ?",
            (float(cost_usd), int(task_id)),
        )
        await db.commit()
    except Exception as e:
        logger.debug("record_task_cost skipped for %d: %r", task_id, e)
