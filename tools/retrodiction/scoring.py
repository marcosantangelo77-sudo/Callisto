"""Scoring retrodiction runs: Brier, calibration, slices, resolved-claim records.

Output record is shaped to feed the existing lifecycle and B4's inheritance
rule (tools/research_program.py ResolutionRecord): these ARE the resolved
descendants that lift a parent claim's ceiling.
"""

from __future__ import annotations

import random
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


# ── Significance: is config A actually better than config B? ──────────────
#
# The A/B harness reports two Brier means; nothing said whether the gap is a
# real property of the configs or noise from N small questions. NEXT.md's
# whole thesis is that conclusions carry trustworthy confidence — an A/B
# verdict that ignores sampling error violates it. These are pure-Python:
# no numpy/scipy dependency (the repo has none).


def _paired_briers(predictions_a, predictions_b, questions):
    """Per-question Brier vectors for the questions BOTH configs answered.
    Paired: identical questions, so per-question differences cancel question
    difficulty and the test needs far smaller N than unpaired."""
    qmap = {q.question_id: q.answer_binary for q in questions}
    ids = sorted({p.question_id for p in predictions_a}
                 & {p.question_id for p in predictions_b})
    pa = {p.question_id: p.probability for p in predictions_a}
    pb = {p.question_id: p.probability for p in predictions_b}
    da = [pa[i] - (1.0 if qmap[i] else 0.0) for i in ids]
    db = [pb[i] - (1.0 if qmap[i] else 0.0) for i in ids]
    return [(x ** 2, y ** 2) for x, y in zip(da, db)]


def paired_significance(predictions_a, predictions_b, questions,
                        n_permutations: int = 10_000,
                        seed: int = 0) -> dict:
    """Exact-ish paired permutation test on mean Brier difference.

    Under H0 (both configs equivalent), swapping each question's pair of
    scores between configs is equally likely, so the sign pattern of the
    differences is exchangeable. p = fraction of resampled sign patterns
    whose |mean difference| >= the observed one. Two-sided.

    Returns {brier_a, brier_b, delta (a − b; negative = A better),
             n, p_value, better ('A'|'B'|None), significant_at_0_05}.
    """
    pairs = _paired_briers(predictions_a, predictions_b, questions)
    if not pairs:
        raise ValueError("no questions answered by both configs")
    diffs = [a - b for a, b in pairs]
    observed = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    extremes = 0
    for _ in range(n_permutations):
        m = sum(d if rng.random() < 0.5 else -d for d in diffs) / len(diffs)
        if abs(m) >= abs(observed) - 1e-12:
            extremes += 1
    p_value = extremes / n_permutations
    return {
        "brier_a": sum(a for a, _ in pairs) / len(pairs),
        "brier_b": sum(b for _, b in pairs) / len(pairs),
        "delta": observed,
        "n": len(diffs),
        "p_value": p_value,
        "better": ("A" if observed < 0 else "B") if p_value < 0.05 else None,
        "significant_at_0_05": p_value < 0.05,
    }


def brier_decomposition(predictions, questions, n_bins: int = 5) -> dict:
    """Murphy decomposition of the mean Brier score:

        Brier = RELIABILITY − RESOLUTION + UNCERTAINTY

    reliability — penalty for miscalibration (want → 0)
    resolution  — how much the forecasts distinguish outcomes (want large)
    uncertainty — irreducible variance of the outcome itself (floor)

    Reliability near zero with low resolution means the model is honest but
    uninformative — a different failure than overconfidence, and invisible to
    the raw score alone. UNCERTAINTY uses the realised base rate, which is
    exact for binary outcomes.
    """
    qmap = {q.question_id: q.answer_binary for q in questions}
    pairs = [(p.probability, qmap[p.question_id]) for p in predictions
             if p.question_id in qmap]
    if not pairs:
        raise ValueError("no predictions matched any question")
    n = len(pairs)
    base_rate = sum(y for _, y in pairs) / n
    uncertainty = base_rate * (1.0 - base_rate)
    width = 1.0 / n_bins
    rel = 0.0
    res = 0.0
    used = 0
    for i in range(n_bins):
        lo, hi = i * width, (i + 1) * width
        bucket = [(p, y) for p, y in pairs
                  if lo <= p < hi or (i == n_bins - 1 and p == 1.0)]
        if not bucket:
            continue
        nk = len(bucket)
        used += nk
        pk = sum(p for p, _ in bucket) / nk
        ok = sum(1 for _, y in bucket if y) / nk
        rel += nk * (pk - ok) ** 2
        res += nk * (ok - base_rate) ** 2
    rel /= n
    res /= n
    return {
        "reliability": rel,
        "resolution": res,
        "uncertainty": uncertainty,
        "brier_from_parts": rel - res + uncertainty,
        "n": used,
        "base_rate": base_rate,
    }


def bootstrap_brier_ci(predictions, questions, n_bootstraps: int = 10_000,
                       confidence: float = 0.95, seed: int = 0) -> tuple:
    """Percentile bootstrap CI on the mean Brier score, resampling questions.
    The honest companion to every headline 'mean Brier' this harness prints."""
    qmap = {q.question_id: q.answer_binary for q in questions}
    scores = [_brier_one(p.probability, qmap[p.question_id])
              for p in predictions if p.question_id in qmap]
    if not scores:
        raise ValueError("no predictions matched any question")
    rng = random.Random(seed)
    n = len(scores)
    means = []
    for _ in range(n_bootstraps):
        means.append(sum(scores[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_i = max(0, int(alpha * n_bootstraps))
    hi_i = min(n_bootstraps - 1, int((1.0 - alpha) * n_bootstraps))
    return (means[lo_i], means[hi_i])
