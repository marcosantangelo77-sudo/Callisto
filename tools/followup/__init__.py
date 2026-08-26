"""tools.followup — Callisto's task auto-followup guard package.

Background
----------
``api._maybe_auto_followup`` used to re-enqueue tasks whenever a
task's result contained the literal string "Next step:" and >20 chars of
trailing text. There was no depth cap, no dedup, no quality gate, no
chain-budget. A hallucinated next-step could spawn a runaway chain or,
in the worst case, a recursive loop that burned the entire Claude hourly
budget.

This package provides the hardened replacement (split out of the former
monolithic ``tools/followup_guard.py``). It is deliberately separate so:
  1. ``api.py`` stays readable — the guard logic is testable in isolation.
  2. Migrations live alongside the guards that consume the new columns.
  3. Tests can exercise each guard independently without spinning up
     the full FastAPI app.

Guards (all toggleable via env; all default-on):
  - ``CALLISTO_MAX_FOLLOWUP_DEPTH``     hard cap on follow-up nesting (default 5)
  - ``CALLISTO_FOLLOWUP_DEDUP``         semantic dedup within a 1h window
  - ``CALLISTO_FOLLOWUP_QUALITY_GATE``  reject vague / verbatim / entity-free followups
  - ``CALLISTO_MAX_FOLLOWUP_FANOUT``    direct followups per parent (default 3)
  - ``CALLISTO_MAX_CHAIN_BUDGET_USD``   cumulative cost ceiling per chain (default 1.00)
  - ``CALLISTO_FOLLOWUP_DEDUP_WINDOW_S``  dedup lookback window (default 3600)
  - ``CALLISTO_FOLLOWUP_DEDUP_THRESHOLD`` cosine threshold (default 0.95)

The schema migration adds four columns to ``task_queue``:
  - ``followup_depth``  (INTEGER DEFAULT 0)  — 0 for user-initiated
  - ``parent_task_id``  (INTEGER)            — direct parent, or NULL
  - ``root_task_id``    (INTEGER)            — 0-depth ancestor, always self for depth=0
  - ``cost_usd``        (REAL DEFAULT 0)     — Claude escalation cost for this task

Submodules:
  - ``env``          env-var toggles + cost-model defaults
  - ``decision``     the FollowupDecision dataclass
  - ``schema``       migration / row lookup / insert helper
  - ``quality``      the query quality gate
  - ``dedup``        semantic near-duplicate detection
  - ``budget``       fan-out counts, chain cost, cost recording
  - ``orchestrate``  evaluate_followup — runs every guard in order
  - ``chain``        get_chain_tree reporting for /task/{id}/chain

The legacy module name ``tools.followup_guard`` remains as a facade that
re-exports every public symbol, so existing callers and tests keep working.
"""

from tools.followup.budget import (
    chain_cost_usd,
    count_direct_followups,
    record_task_cost,
)
from tools.followup.chain import get_chain_tree
from tools.followup.decision import FollowupDecision
from tools.followup.dedup import find_near_duplicate
from tools.followup.env import (
    DEFAULT_TASK_COST_USD,
    _env_bool,
    _env_float,
    _env_int,
    dedup_enabled,
    dedup_threshold,
    dedup_window_seconds,
    max_chain_budget_usd,
    max_depth,
    max_fanout,
    quality_gate_enabled,
)
from tools.followup.orchestrate import evaluate_followup
from tools.followup.quality import (
    _ENTITY_PATTERNS,
    _VAGUE_PHRASES,
    evaluate_quality,
    strip_followup_header,
    token_edit_distance_ratio,
)
from tools.followup.schema import (
    ensure_followup_columns,
    get_task_meta,
    insert_followup,
)

# Backwards-compatible private aliases from the monolithic module era.
_strip_followup_header = strip_followup_header
_token_edit_distance_ratio = token_edit_distance_ratio

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
