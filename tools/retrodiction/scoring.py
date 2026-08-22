"""Scoring retrodiction runs: Brier, calibration, slices, resolved-claim records.

Output record is shaped to feed the existing lifecycle and B4's inheritance
rule (tools/research_program.py ResolutionRecord): these ARE the resolved
descendants that lift a parent claim's ceiling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from tools.retrodiction.questions import QuestionType


@dataclass
class Prediction:
    """One researcher verdict on one retrodiction question."""
    question_id: str
    probability: float                 # P(answer_binary = True), 0..1
    confidence: float = -1.0           # researcher's stated confidence 0..1;
                                       # defaults to |p - 0.5| * 2 if unset
    config_label: str = "default"
    loops: int = 1                     # loop iterations used to produce it

    def __post_init__(self):
        if not (0.0 <= self.probability <= 1.0):
            raise ValueError("probability must be in [0,1]")

    @property
    def effective_confidence(self) -> float:
        if self.confidence >= 0.0:
            return self.confidence
        return abs(self.probability - 0.5) * 2.0


def _brier_one(p: float, y: bool) -> float:
    return (p - (1.0 if y else 0.0)) ** 2


def score_brier(predictions, questions) -> float:
    """Mean Brier score over the intersection of predictions with questions.
    Lower is better; 0.25 = chance for binary."""
    qmap = {q.question_id: q.answer_binary for q in questions}
    scores = [_brier_one(p.probability, qmap[p.question_id])
              for p in predictions if p.question_id in qmap]
    if not scores:
        raise ValueError("no predictions matched any question")
    return sum(scores) / len(scores)


def calibration_curve(predictions, questions, n_bins: int = 5):
    """Binned confidence vs realised frequency.

    Returns list of dicts: {bin_low, bin_high, n, mean_probability,
    realised_frequency}. Bins are on stated probability.
    """
    qmap = {q.question_id: q.answer_binary for q in questions}
    pairs = [(p.probability, qmap[p.question_id]) for p in predictions
             if p.question_id in qmap]
    width = 1.0 / n_bins
    out = []
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        bucket = [(p, y) for p, y in pairs if lo <= p < hi or
                  (i == n_bins - 1 and p == 1.0)]
        entry = {
            "bin_low": round(lo, 4), "bin_high": round(hi, 4),
            "n": len(bucket),
            "mean_probability": None,
            "realised_frequency": None,
        }
        if bucket:
            entry["mean_probability"] = sum(p for p, _ in bucket) / len(bucket)
            entry["realised_frequency"] = (
                sum(1 for _, y in bucket if y) / len(bucket))
        out.append(entry)
    return out


def slice_breakdown(predictions, questions, key="domain"):
    """Brier score per slice of a question attribute (domain, horizon band,
    question_type). Returns {slice_value: {"n": int, "brier": float}}."""
    qmap = {q.question_id: q for q in questions}

    def slice_of(q):
        if key == "horizon":
            d = getattr(q, "horizon_days", 0)
            return ("short" if d <= 45 else
                    "medium" if d <= 120 else "long")
        val = getattr(q, key)
        return val.value if isinstance(val, QuestionType) else str(val)

    slices: dict[str, tuple[list[float], int]] = {}
    for p in predictions:
        q = qmap.get(p.question_id)
        if q is None:
            continue
        s = slice_of(q)
        acc = slices.setdefault(s, [[], 0])
        acc[0].append(_brier_one(p.probability, q.answer_binary))
        acc[1] += 1
    return {s: {"n": n, "brier": sum(bs) / len(bs)}
        for s, (bs, n) in slices.items()}


def resolved_claim_record(question, prediction,
                          source_class: str = "SECONDARY",
                          parent_claim_id: str | None = None) -> dict:
    """Shape a scored retrodiction as a resolved-claim record compatible with
    tools/research_program.py's inheritance rule (ResolutionRecord keys) and
    agp/research_program.py's question model. hit/miss mirrors outcome; a
    well-calibrated near-certain miss still counts as a miss — no credit for
    eloquence."""
    hit = bool(prediction.probability >= 0.5) == bool(question.answer_binary)
    # Pinball-style normalized loss for binary claims, so quantile-consuming
    # code paths get a continuous score too.
    pinball = abs(prediction.probability -
                  (1.0 if question.answer_binary else 0.0))
    return {
        # ResolutionRecord-compatible keys (B4 inheritance rule input):
        "question_id": question.question_id,
        "resolved_at": (question.resolution_date.isoformat()
                        if question.resolution_date else None),
        "outcome": "hit" if hit else "miss",
        "pinball_score": pinball,
        "best_source_class": source_class,
        # Retrodiction extras (audit trail):
        "parent_claim_id": parent_claim_id,
        "claim_date": question.claim_date.isoformat(),
        "resolution_date": question.resolution_date.isoformat(),
        "predicted_probability": prediction.probability,
        "brier": _brier_one(prediction.probability, question.answer_binary),
        "confidence": prediction.effective_confidence,
        "config_label": prediction.config_label,
        "loops": prediction.loops,
        "recorded_at": datetime.utcnow().isoformat(),
    }
