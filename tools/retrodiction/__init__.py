"""Retrodiction harness (NEXT.md §1).

Ask questions whose answers were unknown at a past date and are known now;
hard-limit evidence acquisition to sources published before that cutoff; score
the conclusion against reality. Produces the corpus of RESOLVED claims that
surprise-driven exploration and cross-domain analogy both require.

Modules:
  cutoff    — provenance-verified temporal cutoff (the hard part)
  questions — retrodiction question generation + storage
  scoring   — Brier, calibration curves, slice breakdowns, resolved-claim records
  harness   — pluggable-researcher A/B runner + loop-calibration measurement

Zero domain vocabulary: financial questions are one generator among many; any
dated, resolvable fact works identically. No network in tests — fixtures only.
"""

from tools.retrodiction.cutoff import (
    CutoffEnforcer,
    CutoffViolation,
    EvidenceRecord,
    PublicationProof,
)
from tools.retrodiction.questions import (
    QuestionType,
    RetrodictionQuestion,
    generate_earnings_questions,
    save_questions,
    load_questions,
)
from tools.retrodiction.scoring import (
    Prediction,
    score_brier,
    calibration_curve,
    slice_breakdown,
    resolved_claim_record,
)
from tools.retrodiction.harness import (
    Researcher,
    StubResearcher,
    RunConfig,
    RunResult,
    run_ab,
    loop_calibration,
)

__all__ = [
    "CutoffEnforcer", "CutoffViolation", "EvidenceRecord", "PublicationProof",
    "QuestionType", "RetrodictionQuestion",
    "generate_earnings_questions", "save_questions", "load_questions",
    "Prediction", "score_brier", "calibration_curve", "slice_breakdown",
    "resolved_claim_record",
    "Researcher", "StubResearcher", "RunConfig", "RunResult",
    "run_ab", "loop_calibration",
]
