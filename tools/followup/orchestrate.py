"""tools.followup.orchestrate — evaluate_followup: run every guard in order.

Order matters — cheap checks first so noisy rejects don't cost
embedding calls:
  1. Load parent meta (must exist).
  2. Depth cap.
  3. Fan-out cap.
  4. Quality gate.
  5. Chain budget.
  6. Semantic dedup (most expensive).

The caller (``api._maybe_auto_followup``) is responsible for actually
enqueuing when ``allowed`` is True, using the returned ``query`` plus
``parent_task_id`` / ``root_task_id`` / ``depth`` for the insert.
"""

from __future__ import annotations

import logging

import aiosqlite

from tools.followup.budget import chain_cost_usd, count_direct_followups
from tools.followup.decision import FollowupDecision
from tools.followup.dedup import find_near_duplicate
from tools.followup.env import (
    dedup_enabled,
    max_chain_budget_usd,
    max_depth,
    max_fanout,
    quality_gate_enabled,
)
from tools.followup.quality import evaluate_quality
from tools.followup.schema import get_task_meta

logger = logging.getLogger("callisto.followup_guard")


async def evaluate_followup(
    db: aiosqlite.Connection,
    parent_task_id: int,
    proposed_query: str,
) -> FollowupDecision:
    """Run every guard and return a single Decision."""
    parent = await get_task_meta(db, parent_task_id)
    if parent is None:
        return FollowupDecision(
            allowed=False, reason="parent_not_found", query=proposed_query
        )

    parent_depth = int(parent["followup_depth"])
    parent_query = parent["query"] or ""
    root_id = int(parent["root_task_id"] or parent_task_id)
    new_depth = parent_depth + 1

    # 1) Depth
    cap = max_depth()
    if new_depth > cap:
        logger.warning(
            "followup_depth_exceeded: parent=%d depth=%d cap=%d",
            parent_task_id, new_depth, cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="followup_depth_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 2) Fan-out
    fanout_cap = max_fanout()
    direct = await count_direct_followups(db, parent_task_id)
    if direct >= fanout_cap:
        logger.warning(
            "followup_fanout_exceeded: parent=%d existing=%d cap=%d",
            parent_task_id, direct, fanout_cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="followup_fanout_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 3) Quality
    if quality_gate_enabled():
        passed, reason = evaluate_quality(parent_query, proposed_query)
        if not passed:
            logger.info(
                "followup_quality_rejected: parent=%d reason=%s",
                parent_task_id, reason,
            )
            return FollowupDecision(
                allowed=False,
                reason=f"quality_gate:{reason}",
                query=proposed_query,
                parent_task_id=parent_task_id,
                root_task_id=root_id,
                depth=new_depth,
            )

    # 4) Chain budget
    budget_cap = max_chain_budget_usd()
    spent = await chain_cost_usd(db, root_id)
    if spent >= budget_cap:
        logger.warning(
            "followup_chain_budget_exceeded: root=%d spent=%.4f cap=%.4f",
            root_id, spent, budget_cap,
        )
        return FollowupDecision(
            allowed=False,
            reason="chain_budget_exceeded",
            query=proposed_query,
            parent_task_id=parent_task_id,
            root_task_id=root_id,
            depth=new_depth,
        )

    # 5) Dedup — merge into an existing recent task when too similar.
    if dedup_enabled():
        dup = await find_near_duplicate(db, proposed_query)
        if dup is not None:
            logger.info(
                "followup_dedup_merge: parent=%d → existing task %d",
                parent_task_id, dup,
            )
            return FollowupDecision(
                allowed=False,
                reason="dedup_merge",
                query=proposed_query,
                parent_task_id=parent_task_id,
                root_task_id=root_id,
                depth=new_depth,
                merge_target_id=dup,
            )

    return FollowupDecision(
        allowed=True,
        reason="ok",
        query=proposed_query,
        parent_task_id=parent_task_id,
        root_task_id=root_id,
        depth=new_depth,
    )
