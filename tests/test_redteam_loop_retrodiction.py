"""RED TEAM H6 — retrodiction scoring: can a model score well without
researching well?

Vectors probed:
  A. Question selection — score_brier averages only the intersection of
     predictions with questions. Skipping hard questions is invisible.
  B. Cutoff exploitation — the enforcer is sound, but questions carry
     answer-shaped metadata into generation (see below).
  C. Base-rate riding — binary Brier rewards extreme confident guesses on
     skewed question sets; no per-question difficulty control exists.
"""
import math
from datetime import date

import pytest

from tools.retrodiction.questions import QuestionType, RetrodictionQuestion
from tools.retrodiction.scoring import (
    Prediction,
    score_brier,
    resolved_claim_record,
)


def q(qid, answer):
    return RetrodictionQuestion(
        text=f"question {qid}",
        domain="FINANCIAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=date(2024, 1, 1),
        resolution_date=date(2024, 3, 1),
        answer_binary=answer,
        question_id=qid,
    )


def p(qid, prob, conf=None):
    return Prediction(question_id=qid, probability=prob,
                      confidence=conf if conf is not None else -1.0)


# ── A. Cherry-picking: skip what you'd get wrong ────────────────────────

def test_score_brier_ignores_unanswered_questions_silently():
    """A researcher that answers only the 8 easy questions out of 10 gets
    scored on 8. Nothing in the score record exposes the 2 skips; mean
    Brier improves by omission. Compare against answering all 10 badly."""
    qs = [q(f"easy{i}", True) for i in range(8)] + \
         [q(f"hard{i}", True) for i in range(2)]
    cherry = [p(f"easy{i}", 0.95) for i in range(8)]          # skips hards
    honest = [p(f"easy{i}", 0.95) for i in range(8)] + \
             [p(f"hard{i}", 0.05) for i in range(2)]          # wrong on hards
    b_cherry = score_brier(cherry, qs)
    b_honest = score_brier(honest, qs)
    assert b_cherry < 0.01 < b_honest
    # And nothing distinguishes 'skipped' from 'attempted-and-scored':
    # score_brier returns one float; coverage is not in the contract.


def test_coverage_is_not_part_of_the_score_contract():
    """Documenting the gap: there is no API on scoring that reports how
    many questions were attempted vs available."""
    import tools.retrodiction.scoring as sc
    assert not hasattr(sc, "coverage")
    assert "n_attempted" not in str(
        __import__("inspect").getsource(sc.score_brier))


# ── B. Answer-shaped leakage at question construction ───────────────────

def test_question_object_carries_answer_into_the_same_process():
    """prompt_for_researcher() is the sanctioned view, but the object itself
    sits in memory with answer_binary set. Any harness bug that passes the
    question (not prompt_for_researcher()) to the model leaks ground truth
    with no guard raising. The type makes leakage one mistake away, and
    nothing tests that researchers never receive the full object."""
    question = q("leak", True)
    safe = question.prompt_for_researcher()
    assert "answer" not in str(safe).lower()
    assert question.answer_binary is True  # ...but it's right there


def test_perfect_predictions_from_leaked_answers_score_impossibly():
    """If the leak happens, the harness cannot detect it from scores alone:
    Brier=0.0 is indistinguishable from a genuinely perfect run."""
    qs = [q("a", True), q("b", False)]
    preds = [p("a", 1.0), p("b", 0.0)]
    assert score_brier(preds, qs) == 0.0


# ── C. Base-rate riding on skewed sets ──────────────────────────────────

def test_confident_constant_on_skewed_set_beats_calibration():
    """80% of questions resolve True. Saying 0.9 always beats any calibrated
    strategy that respects its own uncertainty on this set — and the
    verdict strings ('strongly better than chance') would bless it without
    knowing whether the next set is also 80/20."""
    qs = [q(f"s{i}", True) for i in range(8)] + [q("sX", False)]
    rider = [p(x.question_id, 0.9) for x in qs]
    humble = [p(x.question_id, 0.8) for x in qs]
    assert score_brier(rider, qs) < score_brier(humble, qs)
    assert score_brier(rider, qs) < 0.20  # 'strongly better than chance'


def test_resolved_record_counts_direction_only_for_inheritance():
    """resolved_claim_record feeds the INHERITANCE RULE via hit/miss. A
    parent claim's ceiling rises on descendants that were merely lucky in
    direction — a coin-flip prediction recorded at 0.5... actually hits
    half the time and each hit is a full ResolutionRecord 'hit'."""
    rec = resolved_claim_record(q("r", True), p("r", 0.5000001))
    assert rec["outcome"] == "hit"
    assert rec["brier"] > 0.24  # near-chance Brier, yet an inheritance 'hit'
