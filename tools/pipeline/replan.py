"""Gap-triggered re-planning — the smallest structural answer to DRA's
hierarchical planner.

SIDE-BY-SIDE (JOB 1, see findings/hierarchical_planner.md for the full
report): Skywork's DeepResearchAgent (src/agent/planning_agent.py, MIT,
Copyright (c) 2025 AgentOrchestra) runs a top-level PlanningAgent that is
RE-CONSULTED EVERY ROUND: it sees the execution history of what its
sub-agents actually returned (PlanDecision.analysis evaluates the previous
round) and re-dispatches accordingly. Callisto decomposes ONCE: whatever
the Architect emitted at stage 1 is the whole plan, and a leaf whose
retrieval failed structurally (no query issued, key missing, plausible
source never tried) still gets exactly one attempt with exactly the same
selection. tools/gaps.py already classifies WHY such a leaf came back
empty — honest_null vs retrieval_failure vs unprovable — and nothing acts
on that signal.

This module closes exactly that gap, and nothing else:

  - A leaf whose gap classification is RETRIEVAL FAILURE with an obstacle
    the PLANNER can act on (no query was ever issued / plausible sources
    never consulted / no fetch route) triggers ONE re-plan of THAT LEAF.
    The model is shown the gap statement and asked for a replacement or
    supplementary sub-question spec in the SAME JSON shape as decompose,
    so no new schema and no second decomposition pass over the root.
  - An HONEST NULL does NOT trigger a re-plan: the search was competent;
    spending more budget on it would be re-rolling dice against sources
    that already answered. UNPROVABLE is not acted on either — that is a
    decision about OUR OWN evidence bar (gates/confidence), which this
    module may not touch.

CONSTRAINTS honoured:
  - The re-planner decides WHAT to research (a new question text /
    question_type), never HOW CERTAIN to be. It cannot emit confidence,
    requirements are rebuilt by _decompose's own rules, and every
    confidence path downstream (min(estimate, ceiling), gates, adversary)
    runs unchanged on the replacement leaf's evidence.
  - ONE re-plan per leaf per run (budget), recorded in PipelineResult.notes
    and result.replan_events — a run that needed help must not look like
    one that did not.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from tools.gaps import GapKind, Obstacle

logger = logging.getLogger("callisto.pipeline.replan")

#: Obstacles the planner can actually fix by asking a different question or
#: naming a different source type. RATE_LIMITED / NO_API_KEY / PAYWALLED are
#: access problems — re-planning cannot conjure a key; they stay reported
#: as gaps for the OWNER (tools.gaps.OwnerAction), not retried blind.
PLANNER_ACTIONABLE = frozenset({
    "no_query_issued",   # selection produced nothing routable -> re-route
    "no_adapter",        # selected source has no route -> different source
})

#: Hard cap: one re-plan per leaf per run.
MAX_REPLANS_PER_LEAF = 1


@dataclass
class ReplanEvent:
    """One re-planning decision, surfaced on PipelineResult."""
    question_id: str
    original_text: str
    reason: str                 # gap_kind + obstacle that triggered it
    replaced: bool              # True = new sub-question substituted
    new_text: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "original_text": self.original_text,
            "reason": self.reason,
            "replaced": self.replaced,
            "new_text": self.new_text,
            "note": self.note,
        }


def should_replan(gap_kind: str, obstacle: str) -> bool:
    """THE single membership rule for triggering a re-plan (obstacle form).

    Fires ONLY on a retrieval failure whose obstacle is one the planner can
    remove by researching differently. Honest nulls (competent search,
    genuinely absent) and unprovable claims (our own bar) never fire; an
    empty gap_kind means the leaf answered normally and never fires.
    """
    if not gap_kind:
        return False
    if gap_kind != "retrieval_failure":
        return False
    return obstacle in PLANNER_ACTIONABLE


def gap_is_planner_fixable(gap) -> bool:
    """THE single membership rule, on the full structured EvidenceGap.

    A retrieval failure warrants ONE re-plan when the PLANNER can plausibly
    fix it by researching differently:

      - the recorded obstacle is itself planner-actionable
        (nothing routable was selected / selected source had no route), or
      - some plausible holder of this evidence was NEVER TRIED and is
        reachable (has its API key, is not declared unable to hold it) —
        routing the sub-question at that source is exactly what a
        hierarchical planner does with a failed dispatch.
    """
    if getattr(gap, "kind", None) is None:
        return False
    if str(getattr(gap, "kind", "")) not in (
            GapKind.RETRIEVAL_FAILURE.value, str(GapKind.RETRIEVAL_FAILURE)):
        return False
    if gap.obstacle.value in PLANNER_ACTIONABLE:
        return True
    for c in getattr(gap, "candidates", []) or []:
        if c.tried or c.obstacle is Obstacle.NOT_INDEXED:
            continue
        if _missing_key_for(c.name):
            continue          # unreachable: re-planning cannot conjure a key
        return True
    return False


#: registry seam for gap_is_planner_fixable; set once per process by the
#: engine (or tests) so the predicate stays import-cycle-free.
_registry_lookup = None


def set_registry_lookup(fn) -> None:
    global _registry_lookup
    _registry_lookup = fn


def _missing_key_for(source_name: str) -> bool:
    if _registry_lookup is None:
        return False
    entry = _registry_lookup(source_name)
    if entry is None:
        return True           # unknown source: treat as unreachable
    spec = entry.spec
    env = getattr(spec, "key_env_var", "") or ""
    return bool(env) and not os.environ.get(env)


def replan_messages(original_question_text: str, gap_explanation: str,
                    root_query: str) -> list[dict]:
    """Model messages for the re-plan turn.

    Deliberately the SAME output contract as decompose (one sub_questions
    entry) so the engine's existing parsing/validation path handles the
    reply unchanged. The model sees WHY the first attempt failed and the
    ROOT question for context, but is NOT given any confidence, tier, or
    requirement information — those are not planning inputs.
    """
    from tools.pipeline.model import DECOMPOSE_SYSTEM

    instruction = (
        "Your previous attempt to research this sub-question FAILED as a "
        f"retrieval problem:\n{gap_explanation}\n"
        "Re-plan just this sub-question: either rephrase it so the "
        "available sources can be routed to it, or replace it with a "
        "sub-question that serves the same role for the root question "
        "but targets evidence we CAN reach. Return JSON only, same shape "
        'as before: {"sub_questions": [{...}]} with EXACTLY ONE entry.'
    )
    return [
        {"role": "system", "content": DECOMPOSE_SYSTEM},
        {"role": "user", "content": f"QUESTION: {root_query}"},
        {"role": "user", "content":
            f"SUB-QUESTION TO RE-PLAN: {original_question_text}\n\n"
            f"{instruction}"},
    ]


def parse_replacement(resp_json: Optional[dict],
                      original_question) -> Optional[dict]:
    """Validate a re-plan response into a single replacement spec dict.

    Returns None (keep the original leaf, report honestly) when the model
    produced nothing usable. Only structural validity is checked here —
    no confidence, no gates.
    """
    specs = (resp_json or {}).get("sub_questions") or []
    if len(specs) != 1:
        return None
    spec = specs[0]
    text = str(spec.get("text", "")).strip()
    # Degenerate outputs keep the original: an empty or identical text
    # would re-run the exact retrieval that already failed.
    if not text or text == getattr(original_question, "text", "").strip():
        return None
    spec["text"] = text
    return spec
