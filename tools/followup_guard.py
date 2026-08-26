"""Guards for Callisto's task auto-followup mechanism — compatibility facade.

The implementation now lives in the ``tools.followup`` package:

  tools/followup/env.py          env-var toggles + cost-model defaults
  tools/followup/decision.py     FollowupDecision dataclass
  tools/followup/schema.py       migration / row lookup / insert helper
  tools/followup/quality.py      query quality gate
  tools/followup/dedup.py        semantic near-duplicate detection
  tools/followup/budget.py       fan-out counts, chain cost, cost recording
  tools/followup/orchestrate.py  evaluate_followup orchestration
  tools/followup/chain.py        get_chain_tree reporting

Every public symbol from the original monolithic module is re-exported
here, so ``from tools.followup_guard import X`` keeps working for
``api.py`` and existing tests.
"""

from __future__ import annotations

import logging

# Re-export everything (including private aliases) for full backwards
# compatibility with callers that poke at internals.
from tools.followup import (  # noqa: F401
    DEFAULT_TASK_COST_USD,
    FollowupDecision,
    _ENTITY_PATTERNS,
    _VAGUE_PHRASES,
    _env_bool,
    _env_float,
    _env_int,
    _strip_followup_header,
    _token_edit_distance_ratio,
    chain_cost_usd,
    count_direct_followups,
    dedup_enabled,
    dedup_threshold,
    dedup_window_seconds,
    ensure_followup_columns,
    evaluate_followup,
    evaluate_quality,
    find_near_duplicate,
    get_chain_tree,
    get_task_meta,
    insert_followup,
    max_chain_budget_usd,
    max_depth,
    max_fanout,
    quality_gate_enabled,
    record_task_cost,
    strip_followup_header,
    token_edit_distance_ratio,
)

logger = logging.getLogger("callisto.followup_guard")

__all__ = [
    "DEFAULT_TASK_COST_USD",
    "FollowupDecision",
    "chain_cost_usd",
    "count_direct_followups",
    "dedup_enabled",
    "dedup_threshold",
    "dedup_window_seconds",
    "ensure_followup_columns",
    "evaluate_followup",
    "evaluate_quality",
    "find_near_duplicate",
    "get_chain_tree",
    "get_task_meta",
    "insert_followup",
    "max_chain_budget_usd",
    "max_depth",
    "max_fanout",
    "quality_gate_enabled",
    "record_task_cost",
    "strip_followup_header",
    "token_edit_distance_ratio",
]
