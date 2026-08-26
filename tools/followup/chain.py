"""tools.followup.chain — chain-tree reporting for /task/{id}/chain."""

from __future__ import annotations

import logging

import aiosqlite

from tools.followup.schema import get_task_meta

logger = logging.getLogger("callisto.followup_guard")


async def get_chain_tree(
    db: aiosqlite.Connection, task_id: int
) -> dict:
    """Return the full task tree rooted at ``task_id``'s root ancestor.

    Shape:
      {
        "root_task_id": int,
        "task_count": int,
        "total_cost_usd": float,
        "max_depth": int,
        "tasks": [
          {task_id, query, status, followup_depth, parent_task_id,
           cost_usd, created_at, completed_at}, ...
        ]
      }
    """
    meta = await get_task_meta(db, task_id)
    if meta is None:
        return {"error": "task_not_found", "task_id": task_id}

    root_id = int(meta["root_task_id"] or task_id)
    try:
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query, status, followup_depth, parent_task_id, "
            "cost_usd, created_at, completed_at "
            "FROM task_queue WHERE root_task_id = ? "
            "ORDER BY followup_depth ASC, task_id ASC",
            (root_id,),
        )
        rows = await cur.fetchall()
    except Exception as e:
        logger.warning("get_chain_tree failed: %r", e)
        return {"error": "chain_query_failed", "root_task_id": root_id}

    tasks = []
    max_d = 0
    total_cost = 0.0
    for r in rows:
        depth = int(r[3] or 0)
        cost = float(r[5] or 0.0)
        max_d = max(max_d, depth)
        total_cost += cost
        tasks.append({
            "task_id": r[0],
            "query": r[1],
            "status": r[2],
            "followup_depth": depth,
            "parent_task_id": r[4],
            "cost_usd": cost,
            "created_at": r[6],
            "completed_at": r[7],
        })

    return {
        "root_task_id": root_id,
        "task_count": len(tasks),
        "total_cost_usd": round(total_cost, 6),
        "max_depth": max_d,
        "tasks": tasks,
    }
