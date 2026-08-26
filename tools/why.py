"""WHY — explain a sealed (or refused) conclusion's confidence in plain language.

A live run sealed at SPECULATIVE 0.34. Nothing explains WHY 0.34. The number
is the product of provenance assignments, source-class ceilings, the evidence-
requirement gate, the inheritance rule, and adversary penalties — every one of
which is recorded somewhere, and none of which was assembled into an answer a
human can read. This module is that assembly.

HARD RULES (mirroring BUILD_MANDATE §4):
  - READ-ONLY. Nothing here writes to any component, mutates any object it
    is handed, or computes a new confidence. Every number reported is either
    read off the result or recomputed FROM THE SAME RULES the scorers used,
    purely for display. It cannot make a score friendlier.
  - Domain-general: nothing here knows what a wager or a semiconductor is.

Usage:
    expl = explain_result(pipeline_result, ledger=pipeline.ledger)
    print(expl.narrative())        # plain language
    expl.to_dict()                 # machine-readable, attachable to a claim

Refused runs get the same treatment: the chain is walked up to the point of
refusal and the refusal itself is explained.

The implementation lives in ``tools.whyexp``; this module is a stable facade
so existing imports (``from tools.why import explain_result``) keep working.
"""
from tools.whyexp.records import (
    CeilingWhy,
    EvidenceWhy,
    IndependenceWhy,
    ObjectionWhy,
    RejectedWhy,
    SCHEMA_VERSION,
    StepWhy,
)
from tools.whyexp.explanation import WhyExplanation
from tools.whyexp.provenance import assignment_reason
from tools.whyexp.rejections import parse_rejections
from tools.whyexp.independence import independence_from_fetches
from tools.whyexp.walker import (
    _largest_constraint,
    _StoredShim,
    explain_result,
    explain_stored,
    pipeline_adversary_ledger_statuses,
)

__all__ = [
    "SCHEMA_VERSION",
    "CeilingWhy",
    "EvidenceWhy",
    "IndependenceWhy",
    "ObjectionWhy",
    "RejectedWhy",
    "StepWhy",
    "WhyExplanation",
    "_StoredShim",
    "_largest_constraint",
    "assignment_reason",
    "explain_result",
    "explain_stored",
    "independence_from_fetches",
    "parse_rejections",
    "pipeline_adversary_ledger_statuses",
]
