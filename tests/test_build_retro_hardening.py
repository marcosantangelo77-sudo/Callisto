"""Improve pass — calibration/retrodiction harness hardening.

Covers:
  1. The sync-researcher-inside-async-batch seam (the defect that made every
     question in the first live batch fail with 'event loop is already
     running' — a researcher whose answer() owns its own loop must work).
  2. Paired permutation significance between two A/B arms.
  3. Murphy Brier decomposition and bootstrap CI.
No network; deterministic seeds.
"""
from __future__ import annotations

import asyncio

import pytest

from tools.retrodiction.batch import RetrodictionBatch, build_report, render_report
from tools.retrodiction.harness import Researcher, RunConfig, run_ab
from tools.retrodiction.questions import QuestionType, RetrodictionQuestion
from tools.retrodiction.scoring import (
    Prediction,
    bootstrap_brier_ci,
    brier_decomposition,
    paired_significance,
    score_brier,
)
from tools.pipeline.checkpoint import FileCheckpointer


def _q(qid, answer=True):
    return RetrodictionQuestion(
        question_id=qid, text=f"question {qid}", domain="FINANCIAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=__import__("datetime").date(2024, 1, 1),
        resolution_date=__import__("datetime").date(2024, 3, 1),
        answer_binary=answer, answer_confidence=1.0)


# ── 1. the sync/async seam ────────────────────────────────────────────────

class LoopOwningResearcher(Researcher):
    """Exactly PipelineResearcher's shape: sync answer() that runs its own
    event loop via run_until_complete."""
    name = "loop_owner"

    def answer(self, prompts, evidence, loops=1):
        # Mirror PipelineResearcher: owns its own loop, thread-safe.
        loop = asyncio.new_event_loop()
        try:
            async def go():
                return [Prediction(question_id=p["question_id"],
                                   probability=0.7) for p in prompts]
            return loop.run_until_complete(go())
        finally:
            loop.close()


class AsyncNativeResearcher(Researcher):
    name = "async_native"

    async def answer(self, prompts, evidence, loops=1):
        return [Prediction(question_id=p["question_id"], probability=0.4)
                for p in prompts]


def _batch(tmp_path, researcher_cls):
    return RetrodictionBatch(
        questions=[_q("q1"), _q("q2", answer=False)],
        researcher_factory=researcher_cls,
        checkpointer=FileCheckpointer(root=tmp_path / "cp"),
        results_path=tmp_path / "results.jsonl")


@pytest.mark.parametrize("cls", [LoopOwningResearcher, AsyncNativeResearcher])
def test_batch_runs_researchers_that_own_or_skip_the_loop(cls, tmp_path):
    """Regression: before the seam fix, LoopOwningResearcher died on every
    question with 'RuntimeError: This event loop is already running'."""
    results = asyncio.run(_batch(tmp_path, cls).run())
    assert all(r.status == "scored" for r in results.values()), \
        [r.error for r in results.values()]
    probs = {r.predicted_probability for r in results.values()}
    assert probs == ({0.7} if cls is LoopOwningResearcher else {0.4})


# ── 2. paired significance ────────────────────────────────────────────────

def _preds(mapping, n=20):
    return [Prediction(question_id=f"q{i}", probability=mapping(i))
            for i in range(n)]


def _questions(n=20, base_rate=0.5):
    import random
    rng = random.Random(7)
    return [_q(f"q{i}", answer=rng.random() < base_rate) for i in range(n)]


def test_significance_detects_real_gap():
    qs = _questions()
    good = _preds(lambda i: 0.9 if qs[i].answer_binary else 0.1)
    bad = _preds(lambda i: 0.5)
    sig = paired_significance(good, bad, qs)
    assert sig["significant_at_0_05"]
    assert sig["better"] == "A"
    assert sig["delta"] < 0


def test_significance_does_not_invent_a_winner_on_noise():
    # Both configs answer identically → p must be 1.0, better must be None.
    qs = _questions()
    same = _preds(lambda i: 0.6)
    sig = paired_significance(same, list(same), qs)
    assert not sig["significant_at_0_05"]
    assert sig["better"] is None
    assert sig["p_value"] == 1.0


def test_significance_small_n_is_honest_about_uncertainty():
    # A 2-question gap, however large, cannot reach significance — the old
    # harness would have reported the raw means as if they settled it.
    qs = _questions(2)
    good = _preds(lambda i: 0.95 if qs[i].answer_binary else 0.05, n=2)
    bad = _preds(lambda i: 0.5, n=2)
    sig = paired_significance(good, bad, qs, seed=1)
    assert not sig["significant_at_0_05"]


def test_run_ab_attaches_significance_to_both_arms():
    qs = _questions()
    # Deterministic answers so the separation is real, not sampling luck.
    answers = [i % 2 == 0 for i in range(20)]
    qs = [_q(f"q{i}", answer=answers[i]) for i in range(20)]

    class ParityResearcher(Researcher):
        """Answers 'yes' for even ids, 'no' for odd — matches the planted
        answers exactly when correct=True."""
        def __init__(self, correct: bool):
            self.correct = correct

        def answer(self, prompts, evidence, loops=1):
            out = []
            for p in prompts:
                i = int(p["question_id"][1:])
                yes = (i % 2 == 0) == self.correct
                out.append(Prediction(question_id=p["question_id"],
                                      probability=0.95 if yes else 0.05))
            return out

    configs = [RunConfig(label="good",
                         researcher_factory=lambda: ParityResearcher(True)),
               RunConfig(label="bad",
                         researcher_factory=lambda: ParityResearcher(False))]
    results = run_ab(configs, qs, ev := [])
    sig = results["good"].significance
    assert results["bad"].significance is sig  # shared object
    assert "significance" in results["good"].summary()
    # Perfect-vs-inverted knowledge is a maximal gap → significant.
    assert sig["significant_at_0_05"]
    assert sig["delta"] < 0  # A (good) has lower Brier
    # And the honest null: two identical arms must never "win".
    same = run_ab([RunConfig(label="x",
                             researcher_factory=lambda: ParityResearcher(True)),
                   RunConfig(label="y",
                             researcher_factory=lambda: ParityResearcher(True))],
                  qs, [])
    assert same["x"].significance["better"] is None


# ── 3. Brier decomposition + CI ──────────────────────────────────────────

def test_decomposition_recovers_the_brier_score():
    qs = _questions()
    preds = _preds(lambda i: 0.85 if qs[i].answer_binary else 0.25)
    d = brier_decomposition(preds, qs)
    assert d["brier_from_parts"] == pytest.approx(score_brier(preds, qs),
                                                  abs=1e-6)


def test_decomposition_flags_uninformative_but_calibrated():
    # Predicting each question exactly at the empirical bin frequency:
    # reliability ≈ 0 by construction, resolution 0 (all predictions in one
    # bin). Use a large deterministic sample so bins are near their base rate.
    answers = [i % 2 == 0 for i in range(200)]
    qs = [_q(f"q{i}", answer=answers[i]) for i in range(200)]
    preds = _preds(lambda i: 0.5, n=200)
    d = brier_decomposition(preds, qs)
    assert d["reliability"] < 0.01
    assert d["resolution"] < 0.01


def test_decomposition_penalises_overconfidence():
    # Confidently wrong half the time → reliability must dominate resolution.
    qs = [_q(f"q{i}", answer=(i % 2 == 0)) for i in range(20)]
    preds = _preds(lambda i: 0.95 if not qs[i].answer_binary else 0.05)
    d = brier_decomposition(preds, qs)
    assert d["reliability"] > 3 * d["resolution"] > 0.1


def test_bootstrap_ci_brackets_point_estimate():
    qs = _questions()
    preds = _preds(lambda i: 0.85 if qs[i].answer_binary else 0.25)
    lo, hi = bootstrap_brier_ci(preds, qs, seed=3)
    point = score_brier(preds, qs)
    assert lo <= point <= hi
    assert 0 < hi - lo < 0.2


def test_empty_inputs_raise_not_lie():
    with pytest.raises(ValueError):
        paired_significance([], [], [])
    with pytest.raises(ValueError):
        brier_decomposition([], [])


# ── report wiring ─────────────────────────────────────────────────────────

def test_report_carries_ci_and_decomposition():
    # All predictions 0.8, answers all True → brier (0.2)^2 = 0.04 exactly;
    # the bootstrap CI is tight and brackets the mean.
    rows = {
        f"q{i}": __import__("tools.retrodiction.batch",
                            fromlist=["BatchResult"]).BatchResult(
            question_id=f"q{i}", status="scored",
            predicted_probability=0.8, answer_binary=True,
            brier=0.04)
        for i in range(10)}
    rep = build_report(rows)
    assert rep["brier_ci95"] is not None
    assert rep["brier_ci95"][0] <= rep["mean_brier"] <= rep["brier_ci95"][1]
    assert rep["brier_ci95"][1] - rep["brier_ci95"][0] < 0.02
    assert rep["brier_decomposition"]["n"] == 10
    text = render_report(rep)
    assert "CI" in text and "decomposition" in text


def test_report_without_scored_rows_has_null_stats():
    rows = {"x": __import__("tools.retrodiction.batch",
                            fromlist=["BatchResult"]).BatchResult(
        question_id="x", status="error", error="boom")}
    rep = build_report(rows)
    assert rep["brier_ci95"] is None
    assert rep["brier_decomposition"] is None
