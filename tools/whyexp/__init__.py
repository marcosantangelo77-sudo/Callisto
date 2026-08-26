"""tools.whyexp — the implementation behind the ``tools.why`` facade.

Split out of tools/why.py so each concern lives in its own module:

  - records:      explanation dataclasses (EvidenceWhy, CeilingWhy, ...)
  - provenance:   per-item class-assignment replay against the ledger
  - rejections:   ingestion-rejection parsing from result notes
  - independence: independent-source accounting with family collapses
  - walker:       explain_result / explain_stored assembly
  - explanation:  the WhyExplanation record + narrative rendering

READ-ONLY by mandate: nothing here mutates a pipeline result, writes to any
component, or recomputes confidence for anything but display.
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
