"""Routing-layer improvement pass (2026-08-23) — provider/routing area.

Fixes verified here:
  W8  ThompsonRoutingPolicy.decide pools all task_classes under a role.
      Fixed: per-(role, task_class) comparison; an empty slice means the
      candidate is UNMEASURED for that call, inheriting nothing.
  W7  write_routing_scores duplicates rows on batch rerun. Fixed: dedupe
      on (role, model, task_class, question_id).
  Loop closure: routing-store identity must be lookable by the router
      (endpoint.model names, per role AND task_class).

Fixtures only; no sockets.
"""
from __future__ import annotations

import random

import pytest

from tools.routing.policy import CandidateModel, ThompsonRoutingPolicy
from tools.routing.scores import ModelScoreStore


A = CandidateModel(name="A", tier="t1", config_rank=0)
B = CandidateModel(name="B", tier="t2", config_rank=1)


def _wins(store, task_class=None, n_draws=100):
    wins = {"A": 0, "B": 0}
    for seed in range(n_draws):
        pol = ThompsonRoutingPolicy(store=store, rng=random.Random(seed))
        d = pol.decide("pipeline", [A, B], task_class=task_class)
        wins[d.model] += 1
    return wins


class TestTaskClassScoping:
    """W8: a classification specialist must not route synthesis calls."""

    def test_synthesis_routes_to_the_model_measured_on_synthesis(self, tmp_path):
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(30):
            store.record(role="pipeline", model="A",
                         task_class="research_synthesis",
                         question_id=f"r{i}", brier=0.20)
            store.record(role="pipeline", model="B",
                         task_class="classification",
                         question_id=f"c{i}", brier=0.05)
        # Pre-fix this routed to B 100/100 via pooling. Post-fix A dominates.
        wins = _wins(store, task_class="research_synthesis")
        assert wins["A"] > 60

    def test_classification_routes_to_the_specialist(self, tmp_path):
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(30):
            store.record(role="pipeline", model="A",
                         task_class="research_synthesis",
                         question_id=f"r{i}", brier=0.05)
            store.record(role="pipeline", model="B",
                         task_class="classification",
                         question_id=f"c{i}", brier=0.10)
        wins = _wins(store, task_class="classification")
        assert wins["B"] > 60   # B measured on THIS class -> B wins here too

    def test_unmeasured_slice_does_not_inherit_other_class_records(self, tmp_path):
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(30):
            store.record(role="pipeline", model="B",
                         task_class="classification",
                         question_id=f"c{i}", brier=0.01)
        # No synthesis records at all: both draw from the wide chance prior;
        # neither is trusted on the strength of its other-class record.
        wins = _wins(store, task_class="research_synthesis")
        # Sanity: draws happen at all (exploration), but no runaway winner
        # driven by cross-class evidence. With identical priors the split is
        # near-even up to seed noise.
        assert abs(wins["A"] - wins["B"]) <= 40

    def test_legacy_call_without_task_class_unchanged(self, tmp_path):
        """No task_class arg == old pooled behaviour, byte for byte."""
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(20):
            store.record(role="pipeline", model="A",
                         task_class="research_synthesis",
                         question_id=f"r{i}", brier=0.45)
            store.record(role="pipeline", model="B",
                         task_class="classification",
                         question_id=f"c{i}", brier=0.05)
        wins = {"A": 0, "B": 0}
        for seed in range(50):
            pol = ThompsonRoutingPolicy(store=store, rng=random.Random(seed))
            wins[pol.decide("pipeline", [A, B]).model] += 1
        assert wins["B"] > wins["A"]   # pooled: B's 0.05 record still counts


class TestWriteRoutingScoresDedupe:
    def _result(self, qid="q1"):
        from tools.retrodiction.batch import BatchResult
        return {qid: BatchResult(question_id=qid, status="scored",
                                 brier=0.05, predicted_probability=0.9)}

    def test_rerun_does_not_duplicate(self, tmp_path):
        from tools.retrodiction.batch import write_routing_scores
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        n1 = write_routing_scores(self._result(), store)
        n2 = write_routing_scores(self._result(), store)
        assert (n1, n2) == (1, 0)
        agg = store.summary("pipeline")["hermes-cli"]
        assert agg["n"] == 1
        assert agg["basis"] == "sparse"   # not inflated by phantom rows

    def test_dedupe_is_per_identity_not_global(self, tmp_path):
        from tools.retrodiction.batch import write_routing_scores
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        write_routing_scores(self._result(), store,
                             model="m1", task_class="research_synthesis")
        # Same question scored by another model / task class is a NEW obs.
        write_routing_scores(self._result(), store,
                             model="m2", task_class="research_synthesis")
        write_routing_scores(self._result(), store,
                             model="m1", task_class="classification")
        assert len(store.load_all()) == 3

    def test_nulls_still_never_written(self, tmp_path):
        from tools.retrodiction.batch import BatchResult, write_routing_scores
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        r = BatchResult(question_id="q-null", status="null", brier=None)
        assert write_routing_scores({"q-null": r}, store) == 0
        assert store.load_all() == []


class TestRouterPassesTaskClass:
    def test_route_order_forwards_task_class_to_policy(self, tmp_path, monkeypatch):
        from inference import ProviderRouter
        cfg = tmp_path / "p.yaml"
        cfg.write_text("""
default_tier: gpu1
providers:
  gpu1:
    backend: llama_cpp_server
    base_url: http://localhost:8081/v1
    model: local-27b
  gpu2:
    backend: llama_cpp_server
    base_url: http://localhost:8082/v1
    model: local-7b
routing:
  task_classes:
    research_synthesis: [gpu1, gpu2]
  empirical_routing:
    enabled: true
""")
        r = ProviderRouter(config_path=str(cfg))
        seen = {}

        class SpyPolicy:
            def decide(self, role, candidates, task_class=None):
                seen["task_class"] = task_class
                seen["role"] = role
                class D:
                    tier = "gpu1"; model = "local-27b"
                    basis = "configured"; sampled_effective_loss = None
                    scores_used = {}
                return D()

        r._routing_policy = SpyPolicy()
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        store.record("architect", "local-27b", "research_synthesis",
                     "q1", 0.2)
        r.score_store = store
        r.route_order("research_synthesis", ["gpu1", "gpu2"], role="architect")
        assert seen["task_class"] == "research_synthesis"
        assert seen["role"] == "architect"
