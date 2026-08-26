"""tools.followup.decision — the FollowupDecision dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class FollowupDecision:
    """Result of evaluating whether a followup should be enqueued.

    ``allowed=True``   → caller should proceed to ``queue.submit_task``
                         using ``query`` and associated metadata.
    ``allowed=False``  → caller should LOG ``reason`` and drop the
                         followup. ``merge_target_id`` is set when the
                         rejection is a dedup-merge (the caller may want
                         to attach context to that existing task).
    """

    allowed: bool
    reason: str
    query: str = ""
    parent_task_id: Optional[int] = None
    root_task_id: Optional[int] = None
    depth: int = 0
    merge_target_id: Optional[int] = None
