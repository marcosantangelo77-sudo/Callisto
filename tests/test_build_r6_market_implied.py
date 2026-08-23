"""market_implied on RetrodictionQuestion — the magnitude-scoring benchmark.

NEXT.md RETRODICTION SCORING says score against the market's implied
distribution, not just direction. Before this field existed the batch
runner did getattr(q, 'market_implied', None): the only test exercising
magnitude scoring monkey-patched the attribute on, and no real question
could carry a market price — so every real batch silently fell back to
binary-only scoring. These tests pin the seam end to end: construct,
validate, persist, reload, and score through the batch runner.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from tools.pipeline.checkpoint import FileCheckpointer
from tools.retrodiction.batch import (BatchConfig, RetrodictionBatch,
                                      magnitude_score)
from tools.retrodiction.questions import (
    RetrodictionQuestion,
    generate_earnings_questions,
    load_questions,
    save_questions,
)
from tools.retrodiction.scoring import Prediction


def test_market_implied_round_trips_through_save_and_load(tmp_path):
    q = RetrodictionQuestion(
        text="Will X beat consensus?", domain="FINANCIAL",
        claim_date=date(2024, 1, 3), resolution_date=date(2024, 2, 1),
        answer_binary=True, market_implied=0.62)
    p = save_questions([q], tmp_path / "qs.json")
    loaded = load_questions(p)
    assert loaded[0].market_implied == pytest.approx(0.62)


def test_market_implied_none_is_the_default_and_serialises(tmp_path):
    q = RetrodictionQuestion(
        text="Will X beat consensus?", domain="FINANCIAL",
        claim_date=date(2024, 1, 3), resolution_date=date(2024, 2, 1),
        answer_binary=True)
    p = save_questions([q], tmp_path / "qs.json")
    assert json.loads(p.read_text())[0]["market_implied"] is None
    assert load_questions(p)[0].market_implied is None


@pytest.mark.parametrize("bad", [-0.1, 1.5, "high", True, float("nan")])
def test_market_implied_rejects_non_probabilities(bad):
    with pytest.raises(ValueError):
        RetrodictionQuestion(
            claim_date=date(2024, 1, 3), resolution_date=date(2024, 2, 1),
            market_implied=bad)


def test_generator_carries_market_implied_from_row():
    qs = generate_earnings_questions([
        {"ticker": "AAPL", "report_date": date(2024, 2, 1),
         "eps_actual": 2.18, "eps_consensus": 2.10,
         "market_implied": 0.72},
        {"ticker": "MSFT", "report_date": date(2024, 1, 25),
         "eps_actual": 2.93, "eps_consensus": 2.78},
    ])
    assert qs[0].market_implied == pytest.approx(0.72)
    assert qs[1].market_implied is None


class _StubResearcher:
    name = "stub"

    def __init__(self, probs):
        self.probs = probs
        self.results = []

    def answer(self, prompts, evidence, loops=1):
        return [Prediction(question_id=p["question_id"],
                           probability=self.probs[p["question_id"]])
                for p in prompts]


def _run_batch(questions, probs, tmp_path):
    batch = RetrodictionBatch(
        questions=questions,
        researcher_factory=lambda: _StubResearcher(probs),
        checkpointer=FileCheckpointer(root=tmp_path / "cp"),
        results_path=tmp_path / "results.jsonl",
        config=BatchConfig(label="test"))
    import asyncio
    return asyncio.run(batch.run())


def test_batch_magnitude_scores_a_loaded_question_set(tmp_path):
    """End to end: a question loaded from disk WITH market_implied gets a
    non-None magnitude block through the real batch runner — the exact
    path that was dead when the benchmark could not ride on the record."""
    q = RetrodictionQuestion(
        question_id="abc123", text="Will X beat consensus?",
        claim_date=date(2024, 1, 3), resolution_date=date(2024, 2, 1),
        answer_binary=True, market_implied=0.55)
    p = save_questions([q], tmp_path / "qs.json")
    loaded = load_questions(p)

    results = list(_run_batch(loaded, {"abc123": 0.70}, tmp_path).values())

    mag = results[0].magnitude
    assert mag is not None
    assert mag["edge_taken"] == pytest.approx(0.15, abs=1e-4)
    # model said 0.70, market 0.55, outcome TRUE — direction right
    assert mag["directional_edge"] == pytest.approx(0.15, abs=1e-4)


def test_batch_binary_fallback_when_no_market(tmp_path):
    q = RetrodictionQuestion(
        question_id="nomkt1", text="Did event E happen?",
        claim_date=date(2024, 1, 3), resolution_date=date(2024, 2, 1),
        answer_binary=True, market_implied=None)
    results = list(_run_batch([q], {"nomkt1": 0.7}, tmp_path).values())
    assert results[0].magnitude is None
    assert results[0].brier is not None
