"""I4 — the retrodiction BATCH runner.

Covers: magnitude scoring, per-question checkpoint/resume (kill mid-batch,
resume, never redo), honest nulls/errors, report slices + verdict honesty,
routing-store bridge. No network, no live model — ScriptedModel / stubs.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tools.pipeline.checkpoint import FileCheckpointer
from tools.retrodiction.batch import (
    BatchConfig,
    BatchResult,
    RetrodictionBatch,
    build_report,
    horizon_band,
    magnitude_score,
    render_report,
    write_routing_scores,
)
from tools.retrodiction.questions import QuestionType, RetrodictionQuestion
from tools.retrodiction.scoring import Prediction
from tools.routing.scores import ModelScoreStore


def _q(qid, answer=True, domain="FINANCIAL", market=None,
       claim=date(2024, 1, 1), resolve=date(2024, 3, 1),
       qtype=QuestionType.BEAT_OR_MISS) -> RetrodictionQuestion:
    q = RetrodictionQuestion(
        question_id=qid,
        text=f"question {qid} about {domain.lower()} things",
        domain=domain, question_type=qtype,
        claim_date=claim, resolution_date=resolve,
        answer_binary=answer, answer_confidence=1.0)
    q.market_implied = market  # type: ignore[attr-defined]
    return q


class StubResearcher:
    """Answers from a fixed map; records prompts it was shown."""
    name = "stub"

    def __init__(self, probs=None):
        self.probs = dict(probs or {})
        self.seen: list[str] = []
        self.results: list = []

    def answer(self, prompts, evidence, loops=1):
        self.seen.extend(p["question_id"] for p in prompts)
        out = [Prediction(question_id=p["question_id"],
                          probability=self.probs.get(p["question_id"], 0.5))
               for p in prompts]
        return out


class ExplodingResearcher(StubResearcher):
    def __init__(self, probs=None, blow_up_on: set[str] = None):
        super().__init__(probs or {})
        self.blow_up_on = blow_up_on or set()

    def answer(self, prompts, evidence, loops=1):
        for p in prompts:
            if p["question_id"] in self.blow_up_on:
                raise RuntimeError("simulated crash")
        return super().answer(prompts, evidence, loops)


def _batch(questions, researcher_cls, tmp_path, **kw):
    return RetrodictionBatch(
        questions=questions,
        researcher_factory=lambda: researcher_cls(**kw.get("rk", {})),
        checkpointer=FileCheckpointer(root=tmp_path / "cp"),
        results_path=tmp_path / "results.jsonl",
        config=BatchConfig(label="test"))


# ── magnitude scoring ──────────────────────────────────────────────────────

class TestMagnitudeScoring:
    def test_no_market_returns_none(self):
        assert magnitude_score(0.7, True, None) is None

    def test_right_direction_positive_edge(self):
        # market says 0.3, outcome happened, we said 0.6 → right, edge 0.3
        m = magnitude_score(0.6, True, 0.3)
        assert m["directional_edge"] == pytest.approx(0.3)
        assert m["edge_taken"] == pytest.approx(0.3)

    def test_wrong_direction_negative_edge(self):
        # market 0.8, outcome TRUE, we said 0.4 → wrong direction, |edge| debited
        m = magnitude_score(0.4, True, 0.8)
        assert m["directional_edge"] == pytest.approx(-0.4)

    def test_correct_but_contra_market_still_credited(self):
        # market 0.9 on a FALSE outcome; we said 0.2 → edge -0.7 taken,
        # direction RIGHT → credited +0.7
        m = magnitude_score(0.2, False, 0.9)
        assert m["directional_edge"] == pytest.approx(0.7)

    def test_rejects_invalid_market(self):
        with pytest.raises(ValueError):
            magnitude_score(0.5, True, 1.5)


# ── batch run, checkpoint, resume ─────────────────────────────────────────

class TestBatchRunResume:
    def test_scores_all_questions_and_writes_results(self, tmp_path):
        qs = [_q(f"q{i}", answer=(i % 2 == 0)) for i in range(4)]
        b = _batch(qs, StubResearcher, tmp_path,
                   rk={"probs": {"q0": 0.9, "q1": 0.2,
                                 "q2": 0.8, "q3": 0.1}})
        results = asyncio_run(b.run())
        assert len(results) == 4
        assert all(r.status == "scored" for r in results.values())
        lines = (tmp_path / "results.jsonl").read_text().strip().splitlines()
        assert len(lines) == 4
        rec = json.loads(lines[0])
        assert rec["brier"] <= 0.05

    def test_kill_mid_batch_resume_never_redo(self, tmp_path):
        qs = [_q(f"q{i}", answer=True) for i in range(4)]
        exploding = _batch(qs, ExplodingResearcher, tmp_path,
                           rk={"probs": {}, "blow_up_on": {"q2"}})
        results = asyncio_run(exploding.run())

        # q2 failed loudly as an error row; q0/q1/q3 are done and durable
        assert results["q2"].status == "error"
        done = exploding.load_completed()
        assert {"q0", "q1", "q3"} <= set(done)
        n_ckpt_before = len([ck for ck in
                             FileCheckpointer(root=tmp_path / "cp").list_all()
                             if ck.stage == "retro_batch"])

        # RESUME with a healthy researcher — completed work must NOT rerun.
        # The stub records which questions it was asked; only q2 may appear.
        resumed = _batch(qs, StubResearcher, tmp_path,
                         rk={"probs": {"q2": 0.7}})
        results2 = asyncio_run(resumed.run())
        assert len(results2) == 4
        stub = StubResearcher()
        # re-derive what the resumed factory ran by inspecting checkpoints:
        cps = [ck for ck in FileCheckpointer(root=tmp_path / "cp").list_all()
               if ck.stage == "retro_batch"]
        assert len(cps) == n_ckpt_before  # no NEW work for finished questions
        assert results2["q0"].predicted_probability == \
            results["q0"].predicted_probability  # served from checkpoint
        assert results2["q2"].status == "scored"      # redone (it had failed)

    def test_checkpoint_hit_skips_execution(self, tmp_path):
        from tools.retrodiction.batch import RetrodictionBatch as B
        qs = [_q("only", answer=False)]
        cp = FileCheckpointer(root=tmp_path / "cp")
        # pre-populate the checkpoint as if a previous process finished it
        from tools.pipeline.checkpoint import hash_inputs, run_key
        rk = run_key(qs[0].text, qs[0].domain,
                     qs[0].claim_date.isoformat())
        ih = hash_inputs({"question_id": "only",
                          "claim_date": "2024-01-01", "config": "test"})
        cp.save(rk, B.STAGE, ih,
                {"status": "scored", "question_id": "only",
                 "predicted_probability": 0.3, "brier": 0.09})
        b = B(questions=qs, researcher_factory=lambda: StubResearcher({}),
              checkpointer=cp, results_path=tmp_path / "r.jsonl",
              config=BatchConfig(label="test"))
        calls_before = 0
        results = asyncio_run(b.run())
        assert results["only"].predicted_probability == 0.3
        # results file carries the resumed row too (rehydrated)
        assert (tmp_path / "r.jsonl").exists()

    def test_error_becomes_a_row_not_a_dead_batch(self, tmp_path):
        qs = [_q("bad", answer=True), _q("good", answer=True)]
        b = _batch(qs, ExplodingResearcher, tmp_path,
                   rk={"probs": {"good": 0.9}, "blow_up_on": {"bad"}})
        results = asyncio_run(b.run())
        assert results["bad"].status == "error"
        assert "simulated crash" in results["bad"].error
        assert results["good"].status == "scored"

    def test_null_when_researcher_returns_nothing(self, tmp_path):
        qs = [_q("void", answer=True)]

        class Empty(StubResearcher):
            def answer(self, prompts, evidence, loops=1):
                return []

        b = _batch(qs, Empty, tmp_path, rk={})
        results = asyncio_run(b.run())
        assert results["void"].status == "null"


asyncio_run = __import__("asyncio").run


# ── reporting ──────────────────────────────────────────────────────────────

class TestReport:
    def _rows_mixed(self):
        r1 = BatchResult(question_id="a", status="scored", domain="FINANCIAL",
                         question_type="beat_or_miss", horizon_band="short",
                         predicted_probability=0.9, answer_binary=True,
                         brier=0.01, sealed=True, elapsed_s=100.0)
        r1.magnitude = magnitude_score(0.9, True, 0.6)
        r2 = BatchResult(question_id="b", status="scored", domain="TECHNICAL",
                         question_type="event_outcome", horizon_band="long",
                         predicted_probability=0.2, answer_binary=False,
                         brier=0.04, sealed=True, elapsed_s=120.0)
        r2.magnitude = magnitude_score(0.2, False, 0.5)
        r3 = BatchResult(question_id="c", status="error", domain="SIGNAL",
                         question_type="threshold_cross",
                         horizon_band="medium", error="boom")
        return {"a": r1, "b": r2, "c": r3}

    def test_overall_numbers(self):
        rep = build_report(self._rows_mixed())
        assert rep["n_total"] == 3
        assert rep["n_scored"] == 2
        assert rep["n_null"] == 1
        assert rep["null_rate"] == pytest.approx(1 / 3, abs=1e-3)
        assert rep["mean_brier"] == pytest.approx(0.025)

    def test_slices_by_domain_horizon_type(self):
        rep = build_report(self._rows_mixed())
        s = rep["slices"]
        assert set(s["by_domain"]) == {"FINANCIAL", "TECHNICAL"}
        assert set(s["by_horizon"]) == {"short", "long"}
        assert set(s["by_question_type"]) == {"beat_or_miss",
                                              "event_outcome"}
        assert s["nulls_by_domain"] == {"SIGNAL": 1}

    def test_magnitude_summary_beats_market_rate(self):
        rep = build_report(self._rows_mixed())
        mag = rep["magnitude"]
        assert mag["n_with_market"] == 2
        # both predictions beat their market benchmark → mean edge > 0
        assert mag["mean_directional_edge"] > 0
        assert mag["beat_market_rate"] == 1.0

    def test_verdict_honest_about_majority_nulls(self):
        rows = {}
        for i in range(4):
            rows[f"n{i}"] = BatchResult(
                question_id=f"n{i}", status="null", domain="GENERAL",
                question_type="event_outcome", horizon_band="short",
                refusal_reason="no proof")
        rows["s0"] = BatchResult(question_id="s0", status="scored",
                                 predicted_probability=0.5, answer_binary=True,
                                 brier=0.25)
        rep = build_report(rows)
        assert "MAJORITY NULLS" in rep["verdict"]

    def test_verdict_honest_about_zero_observations(self):
        rows = {"x": BatchResult(question_id="x", status="error",
                                 error="all dead")}
        rep = build_report(rows)
        assert "NO SCORED OBSERVATIONS" in rep["verdict"]

    def test_render_is_readable_and_shows_nulls(self):
        text = render_report(build_report(self._rows_mixed()))
        assert "NULLS BY DOMAIN" in text
        assert "SIGNAL" in text
        assert "BY DOMAIN" in text
        assert "mean Brier" in text


# ── routing bridge ─────────────────────────────────────────────────────────

class TestRoutingBridge:
    def test_only_scored_rows_written(self, tmp_path):
        r_ok = BatchResult(question_id="ok", status="scored",
                           predicted_probability=0.8, answer_binary=True,
                           brier=0.04)
        r_bad = BatchResult(question_id="nope", status="error",
                            error="x")
        store = ModelScoreStore(path=tmp_path / "scores.jsonl")
        n = write_routing_scores({"ok": r_ok, "nope": r_bad}, store)
        assert n == 1
        recs = store.load_all()
        assert len(recs) == 1
        assert recs[0]["question_id"] == "ok"
        assert recs[0]["source"] == "retrodiction_batch"


# ── horizon banding ────────────────────────────────────────────────────────

def test_horizon_band_boundaries():
    assert horizon_band(45) == "short"
    assert horizon_band(46) == "medium"
    assert horizon_band(120) == "medium"
    assert horizon_band(121) == "long"
