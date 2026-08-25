"""SPEED run 19 — bounded cross-question concurrency in RetrodictionBatch.run().

Pins:
  - default (max_concurrency=1) is byte-identical to the old serial loop:
    same results, same JSONL line ORDER, resume/error semantics untouched
  - max_concurrency=N overlaps questions: wall time drops ~min(N, len)x under
    a stubbed-latency researcher, while every result is still correct and
    appended exactly once
  - errors remain rows, not dead batches, under concurrency
No network; researchers are stubs with asyncio.sleep latency.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.pipeline.checkpoint import FileCheckpointer          # noqa: E402
from tools.retrodiction.batch import (                          # noqa: E402
    BatchConfig, RetrodictionBatch)
from tools.retrodiction.scoring import Prediction                # noqa: E402


def _q(qid: str, answer: bool = True):
    from datetime import date

    from tools.retrodiction.questions import (
        QuestionType, RetrodictionQuestion)

    return RetrodictionQuestion(
        question_id=qid,
        text=f"retro question {qid}",
        domain="FINANCIAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=date(2024, 1, 1),
        resolution_date=date(2024, 3, 1),
        answer_binary=answer,
        answer_confidence=1.0,
        market_implied=None,
    )


class SlowResearcher:
    """Stub researcher: sleeps `latency` then predicts 0.5 for everything."""

    name = "slow-stub"

    def __init__(self, latency: float = 0.5, probs=None):
        self.latency = latency
        self.probs = probs or {}
        self.started: list[str] = []
        self.finished: list[str] = []

    async def answer(self, prompts, evidence, loops=1):
        out = []
        for p in prompts:
            self.started.append(p["question_id"])
            await asyncio.sleep(self.latency)
            self.finished.append(p["question_id"])
            out.append(Prediction(
                question_id=p["question_id"],
                probability=self.probs.get(p["question_id"], 0.5)))
        return out


def _batch(qs, researcher, tmp_path, mc=1):
    return RetrodictionBatch(
        questions=qs,
        researcher_factory=lambda: researcher,
        checkpointer=FileCheckpointer(root=tmp_path / "cp"),
        results_path=tmp_path / "results.jsonl",
        config=BatchConfig(label="test", max_concurrency=mc))


def test_default_serial_matches_old_order(tmp_path):
    qs = [_q(f"q{i}") for i in range(4)]
    b = _batch(qs, SlowResearcher(0.01), tmp_path, mc=1)
    t0 = time.monotonic()
    results = asyncio.run(b.run())
    elapsed = time.monotonic() - t0
    assert all(r.status == "scored" for r in results.values())
    lines = [json.loads(l)["question_id"]
             for l in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert lines == ["q0", "q1", "q2", "q3"]   # serial order preserved


def test_concurrency_overlaps_questions(tmp_path):
    qs = [_q(f"q{i}") for i in range(4)]
    lat = 0.3
    serial = _batch(qs, SlowResearcher(lat), tmp_path / "s", mc=1)
    t0 = time.monotonic()
    r_ser = asyncio.run(serial.run())
    t_serial = time.monotonic() - t0

    par = _batch(qs, SlowResearcher(lat), tmp_path / "p", mc=4)
    t0 = time.monotonic()
    r_par = asyncio.run(par.run())
    t_par = time.monotonic() - t0

    # answers identical
    for qid in r_ser:
        assert r_par[qid].predicted_probability == \
            r_ser[qid].predicted_probability
        assert r_par[qid].status == r_ser[qid].status == "scored"
    # speedup: 4 x 0.3s serial vs ~0.3s parallel — require >=2.5x headroom
    assert t_par < t_serial / 2.5, (t_serial, t_par)
    # each question appended exactly once
    lines = (tmp_path / "p" / "results.jsonl").read_text().splitlines()
    assert sorted(json.loads(l)["question_id"] for l in lines) == \
        ["q0", "q1", "q2", "q3"]


def test_semaphore_bounds_in_flight(tmp_path):
    qs = [_q(f"q{i}") for i in range(6)]
    peak = {"n": 0}
    BOUND = 2

    class Tracked(SlowResearcher):
        async def answer(self, prompts, evidence, loops=1):
            peak["n"] += 1
            try:
                assert peak["n"] <= BOUND, \
                    f"in-flight {peak['n']} > bound {BOUND}"
                await asyncio.sleep(0.05)
                return [Prediction(question_id=p["question_id"],
                                   probability=0.5) for p in prompts]
            finally:
                peak["n"] -= 1

    b = _batch(qs, Tracked(), tmp_path, mc=BOUND)
    results = asyncio.run(b.run())
    assert len(results) == 6
    assert all(r.status == "scored" for r in results.values())


def test_errors_stay_rows_under_concurrency(tmp_path):
    class Exploding(SlowResearcher):
        def __init__(self):
            super().__init__(0.01)

        async def answer(self, prompts, evidence, loops=1):
            for p in prompts:
                if p["question_id"] == "bad":
                    raise RuntimeError("simulated crash")
            return await super().answer(prompts, evidence, loops)

    qs = [_q("bad"), _q("good")]
    b = _batch(qs, Exploding(), tmp_path, mc=2)
    results = asyncio.run(b.run())
    assert results["bad"].status == "error"
    assert results["good"].status == "scored"
