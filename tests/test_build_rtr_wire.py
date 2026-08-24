"""RTR — close the loop: retrodiction scores feed empirical routing.

Covers:
  1. RoleTrackingModel captures which model played which pipeline role.
  2. Honest attribution — one model playing every role is ONE observation
     about that model, never several independent ones.
  3. The batch runner records per-role (role, model, score) into the routing
     store; nulls/errors record nothing.
  4. Readiness threshold: explicit, derived, and enforced — empirical_routing
     stays disabled until honest samples reach it.
  5. The readable report shows n_honest beside n_raw and a visible basis,
     so 3 observations can never masquerade as 300.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.pipeline.model import ScriptedModel
from tools.retrodiction.attribution import (
    RoleTrackingModel,
    attribute_run,
    effective_observation_count,
    roles_for_run,
)
from tools.retrodiction.batch import (
    BatchConfig,
    BatchResult,
    RetrodictionBatch,
    write_routing_scores,
)
from tools.routing.readiness import (
    MIN_DETECTABLE_BRIER,
    PAIRWISE_MIN_N,
    pairwise_min_n,
    readiness_report,
    role_readiness,
)
from tools.routing.report import build_routing_report, render_routing_report
from tools.routing.scores import ModelScoreStore


# ── 1. role tracking at the model seam ─────────────────────────────────────


class _FakeInner:
    name = "stub-model"

    def __init__(self):
        self.calls = []

    async def complete(self, role, messages, **kw):
        self.calls.append(role)
        return {"content": "{}", "model": "stub-model"}


class _FakeResearcher:
    """Minimal PipelineResearcher shape: .model seam + .results list."""

    def __init__(self, model):
        self.model = model
        self.results = []


@pytest.mark.asyncio
async def test_tracker_records_roles_per_run():
    inner = _FakeInner()
    tracker = RoleTrackingModel(inner)
    researcher = _FakeResearcher(tracker)

    rid = tracker.start_run()
    # simulate one pipeline question: Architect once, Manager twice
    await tracker.complete("Architect", [])
    await tracker.complete("Manager", [])
    await tracker.complete("Manager", [])

    used = roles_for_run(tracker, rid)
    assert used == {"Architect": 1, "Manager": 2}
    assert researcher.results == []          # transparent passthrough
    assert tracker.name == "stub-model"


@pytest.mark.asyncio
async def test_tracker_runs_do_not_leak_across_questions():
    tracker = RoleTrackingModel(_FakeInner())
    r1 = tracker.start_run()
    await tracker.complete("Architect", [])
    r2 = tracker.start_run()
    await tracker.complete("Manager", [])
    assert roles_for_run(tracker, r1) == {"Architect": 1}
    assert roles_for_run(tracker, r2) == {"Manager": 1}


# ── 2. honest attribution ───────────────────────────────────────────────────


def test_single_model_multi_role_is_correlated():
    att = attribute_run(
        "run1", {"Architect": 1, "Manager": 2}, default_model="m-27b")
    assert att.single_model_run is True
    assert att.n_distinct_models == 1
    assert set(att.role_models) == {"Architect", "Manager"}
    assert "CORRELATED" in " ".join(att.notes)


def test_effective_count_one_model_all_roles_counts_once():
    atts = [attribute_run(f"run{i}", {"Architect": 1, "Manager": 1},
                          default_model="m-27b")
            for i in range(30)]
    # raw per-role records would say 60; honesty says 30 questions
    raw_role_records = sum(len(a.role_models) for a in atts)
    assert raw_role_records == 60
    assert effective_observation_count(atts, "m-27b") == 30


def test_effective_count_mixed_models_counts_separable_roles():
    a = attribute_run("run1", {"Architect": 1, "Manager": 1},
                      default_model="frontier",
                      role_models_seen={"Manager": "local-27b"})
    assert a.role_models == {"Architect": "frontier",
                             "Manager": "local-27b"}
    assert a.single_model_run is False
    assert effective_observation_count([a], "frontier") == 1
    assert effective_observation_count([a], "local-27b") == 1


def test_no_calls_captured_is_honest_empty():
    att = attribute_run("run0", {}, default_model="x")
    assert att.role_models == {}
    assert not att.single_model_run
    assert effective_observation_count([att], "x") == 0


# ── 3. batch → store wire ───────────────────────────────────────────────────

class _NullCheckpointer:
    def list_all(self):
        return []

    def load(self, *a, **k):
        return None

    def save(self, *a, **k):
        pass


class _StubResearcher:
    """Records calls through a RoleTrackingModel so the batch attributes
    them; returns one prediction for the asked question."""

    def __init__(self, prob):
        self.prob = prob
        self.tracked = RoleTrackingModel(_FakeInner())
        self.model = self.tracked
        self.results = []

    async def answer_async(self, prompts, evidence, loops=1):
        await self.tracked.complete("Architect", ["decompose"])
        await self.tracked.complete("Manager", ["synthesize"])
        from tools.retrodiction.scoring import Prediction
        return [Prediction(question_id=prompts[0]["question_id"],
                           probability=self.prob)]


class _SyncStubResearcher(_StubResearcher):
    def answer(self, prompts, evidence, loops=1):
        import asyncio
        return asyncio.run(self.answer_async(prompts, evidence, loops))


def _mk_question(qid, answer=True):
    from datetime import date
    from tools.retrodiction.questions import RetrodictionQuestion
    return RetrodictionQuestion(
        question_id=qid, text=f"text of {qid}", domain="GENERAL",
        claim_date=date(2024, 1, 1), horizon_days=30,
        answer_binary=answer)


@pytest.mark.asyncio
async def test_batch_wires_per_role_scores_into_store(tmp_path):
    q = _mk_question("q-wire-1")

    def factory():
        return _SyncStubResearcher(prob=0.9)

    batch = RetrodictionBatch(
        questions=[q], researcher_factory=factory,
        checkpointer=_NullCheckpointer(),
        results_path=tmp_path / "res.jsonl",
        config=BatchConfig(label="t", model_name="stub-model"))
    results = await batch.run()

    scored = [r for r in results.values() if r.status == "scored"]
    assert len(scored) == 1
    r = scored[0]
    assert r.attribution is not None
    assert r.attribution["single_model_run"] is True
    assert set(r.attribution["role_models"]) >= {"Architect", "Manager"}

    store = ModelScoreStore(path=tmp_path / "scores.jsonl")
    n = write_routing_scores(results, store, model="stub-model")
    assert n == 1  # ONE question → counts once, even though roles > 1

    recs = store.load_all()
    roles_written = {rec["role"] for rec in recs}
    assert {"Architect", "Manager"} <= roles_written
    assert all(rec["model"] == "stub-model" for rec in recs)
    # every record carries the correlation tag and shared run id
    run_ids = {rec["notes"].split()[0] for rec in recs}
    assert len(run_ids) == 1
    assert all("correlated=true" in rec["notes"] for rec in recs)


@pytest.mark.asyncio
async def test_null_question_writes_nothing(tmp_path):
    class NullR(_SyncStubResearcher):
        async def answer_async(self, prompts, evidence, loops=1):
            return []   # no prediction

    q = _mk_question("q-null-1")
    batch = RetrodictionBatch(
        questions=[q], researcher_factory=lambda: NullR(0.5),
        checkpointer=_NullCheckpointer(),
        results_path=tmp_path / "res.jsonl",
        config=BatchConfig(label="t"))
    results = await batch.run()
    store = ModelScoreStore(path=tmp_path / "scores.jsonl")
    assert write_routing_scores(results, store) == 0
    assert store.load_all() == []


def test_legacy_rows_without_attribution_still_record(tmp_path):
    r = BatchResult(question_id="legacy", status="scored",
                    predicted_probability=0.7, answer_binary=True,
                    brier=0.09)
    store = ModelScoreStore(path=tmp_path / "scores.jsonl")
    n = write_routing_scores({"legacy": r}, store, role="pipeline",
                             model="hermes-cli")
    assert n == 1
    rec = store.load_all()[0]
    assert rec["role"] == "pipeline" and rec["model"] == "hermes-cli"


def test_attribution_persists_through_results_jsonl(tmp_path):
    r = BatchResult(question_id="q9", status="scored",
                    predicted_probability=0.8, answer_binary=False,
                    brier=0.64,
                    attribution={"run_id": "abc",
                                 "role_models": {"Architect": "a",
                                                 "Manager": "b"},
                                 "n_distinct_models": 2,
                                 "single_model_run": False})
    line = json.dumps(r.to_dict(), sort_keys=True)
    back = BatchResult(**json.loads(line))
    assert back.attribution["role_models"]["Manager"] == "b"
    assert back.attribution["single_model_run"] is False


# ── 4. readiness / crossover threshold ─────────────────────────────────────


def test_threshold_formula_is_derived_not_magic():
    # n ≥ 2(z_a + z_b)^2 σ^2 / Δ^2 with worst-case σ²=0.25, Δ=0.05
    assert pairwise_min_n() == 1568
    assert PAIRWISE_MIN_N == pairwise_min_n(MIN_DETECTABLE_BRIER)
    # a larger detectable effect needs fewer observations
    assert pairwise_min_n(min_detectable_brier=0.10) \
        < pairwise_min_n(min_detectable_brier=0.05)


def _attrs(model, runs, roles=("Architect",)):
    out = []
    for i in range(runs):
        out.append(attribute_run(
            f"{model}-{i}", {r: 1 for r in roles}, default_model=model))
    return out


def test_readiness_blocked_below_threshold():
    rr = role_readiness("Sentinel", {
        "alpha": _attrs("alpha", 40),
        "beta": _attrs("beta", 40),
    }, candidates=["alpha", "beta"])
    assert not rr.ready
    assert rr.basis == "provisional"
    assert str(PAIRWISE_MIN_N) in rr.blocking_reason or "1568" \
        in rr.blocking_reason.replace(",", "")


def test_readiness_ready_at_threshold():
    rr = role_readiness("Sentinel", {
        "alpha": _attrs("alpha", PAIRWISE_MIN_N),
        "beta": _attrs("beta", PAIRWISE_MIN_N),
    }, candidates=["alpha", "beta"])
    assert rr.ready
    assert rr.basis == "measured"
    assert readiness_report([rr])["empirical_routing_should_enable"]


def test_readiness_blocked_when_candidate_unmeasured():
    rr = role_readiness("Manager", {
        "alpha": _attrs("alpha", PAIRWISE_MIN_N),
    }, candidates=["alpha", "gamma"])
    assert not rr.ready
    assert "gamma" in rr.blocking_reason


def test_correlated_runs_do_not_inflate_readiness():
    # same model played BOTH roles on every run: raw records 2n, honest n
    atts = [attribute_run(f"r{i}", {"Architect": 1, "Sentinel": 1},
                          default_model="solo") for i in range(PAIRWISE_MIN_N)]
    other = _attrs("other", PAIRWISE_MIN_N)
    rr = role_readiness("Sentinel", {
        "solo": atts, "other": other}, candidates=["solo", "other"])
    assert rr.honest_counts["solo"] == PAIRWISE_MIN_N      # counted once/run
    assert rr.raw_counts["solo"] == PAIRWISE_MIN_N         # per-role records
                                                        # in THIS role
    assert rr.ready                                        # would look like


# ── 5. the readable report ──────────────────────────────────────────────────


def test_report_distinguishes_3_from_300(tmp_path):
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(3):
        store.record(role="Architect", model="small", task_class="hg",
                     question_id=f"a{i}", brier=0.10,
                     predicted_probability=0.8, answer_binary=True)
    for i in range(300):
        store.record(role="Manager", model="big", task_class="ex",
                     question_id=f"b{i}", brier=0.12,
                     predicted_probability=0.7, answer_binary=True)
    rep = build_routing_report(store)
    arch = rep["roles"]["Architect"]["models"]["small"]
    man = rep["roles"]["Manager"]["models"]["big"]
    assert arch["basis"] == "sparse"
    assert man["basis"] == "measured"
    assert arch["n_honest"] == 3 and man["n_honest"] == 300
    text = render_routing_report(rep)
    assert "sparse" in text and "measured" in text


def test_report_flags_correlation_inflation(tmp_path):
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    attrs = [attribute_run(f"r{i}", {"Architect": 1, "Manager": 1},
                           default_model="solo") for i in range(50)]
    # store holds both roles' records; attributions reveal correlation
    for i, a in enumerate(attrs):
        for role, m in a.role_models.items():
            store.record(role=role, model=m, task_class="tc",
                         question_id=f"q{i}", brier=0.2)
    rep = build_routing_report(
        store, attributions_by_role={
            "Architect": {"solo": attrs}, "Manager": {"solo": attrs}})
    for sec in rep["roles"].values():
        m = sec["models"]["solo"]
        assert m["counts_inflated_by_correlation"]
        assert m["n_honest"] == 50 < m["n_raw"] == 100
    text = render_routing_report(rep)
    assert "correlated" in text.lower()


def test_report_calibration_table(tmp_path):
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    # overconfident model: predicts ~0.9, right only half the time
    for i in range(20):
        store.record(role="Sentinel", model="loud", task_class="ar",
                     question_id=f"s{i}", brier=0.5 if i % 2 else 0.01,
                     predicted_probability=0.9,
                     answer_binary=bool(i % 2))
    rep = build_routing_report(store)
    cal = rep["roles"]["Sentinel"]["models"]["loud"]["calibration"]
    top = [b for b in cal if b["bin_low"] >= 0.8]
    assert top and top[0]["n"] == 20
    assert abs(top[0]["mean_p"] - 0.9) < 1e-6
    assert abs(top[0]["realised"] - 0.5) < 1e-6   # visibly miscalibrated


def test_enabled_flag_stays_false_until_ready(tmp_path):
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(10):
        store.record(role="Architect", model="only", task_class="hg",
                     question_id=f"x{i}", brier=0.15)
    rep = build_routing_report(
        store, candidates_by_role={"Architect": ["only", "frontier"]})
    assert rep["empirical_routing_enabled_now"] is False
    assert rep["readiness"]["blockers"]
