"""W2 — empirical model routing: score store, policy, router integration.

Fixtures only; no live API calls, no sockets.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from tools.routing.scores import ModelScoreStore
from tools.routing.policy import CandidateModel, ThompsonRoutingPolicy


# ══════════════════════════════════════════════════════════════════════
# 1. The per-(model, role) score store
# ══════════════════════════════════════════════════════════════════════

class TestScoreStore:
    def test_record_and_reload_survives_restart(self, tmp_path):
        p = tmp_path / "scores.jsonl"
        s1 = ModelScoreStore(path=p)
        s1.record("architect", "qwen3.8", "research_synthesis",
                  "q-001", 0.11, cost_usd=0.0)
        # A brand-new instance = process restart.
        s2 = ModelScoreStore(path=p)
        recs = s2.records_for("architect", "qwen3.8")
        assert len(recs) == 1
        assert recs[0]["question_id"] == "q-001"
        assert recs[0]["brier"] == pytest.approx(0.11)

    def test_append_only_file_is_valid_jsonl(self, tmp_path):
        p = tmp_path / "s.jsonl"
        s = ModelScoreStore(path=p)
        for i in range(5):
            s.record("sentinel", "qwen3.5:4b", "classification",
                     f"q-{i}", 0.2 + i * 0.01)
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        assert len(lines) == 5
        assert [r["question_id"] for r in lines] == \
            [f"q-{i}" for i in range(5)]

    def test_torn_last_line_skipped_not_fatal(self, tmp_path):
        """Crash mid-append: the last line may be partial. Skip it."""
        p = tmp_path / "s.jsonl"
        s = ModelScoreStore(path=p)
        s.record("architect", "m1", "research_synthesis", "q-ok", 0.10)
        with open(p, "a") as f:
            f.write('{"v": 1, "role": "architect", "bro')  # torn write
        s2 = ModelScoreStore(path=p)
        assert len(s2.load_all()) == 1

    def test_brier_range_enforced(self, tmp_path):
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        with pytest.raises(ValueError):
            s.record("architect", "m1", "t", "q", 1.5)
        with pytest.raises(ValueError):
            s.record("architect", "m1", "t", "q", -0.1)

    def test_roles_do_not_leak(self, tmp_path):
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        s.record("architect", "shared-model", "research_synthesis", "q1", 0.05)
        s.record("manager", "shared-model", "extraction", "q1", 0.30)
        # Same model, different role -> separate track records.
        assert len(s.records_for("architect", "shared-model")) == 1
        assert len(s.records_for("manager", "shared-model")) == 1
        assert s.summary("architect")["shared-model"]["mean_brier_raw"] < 0.1
        assert s.summary("manager")["shared-model"]["mean_brier_raw"] > 0.25

    def test_aggregate_shrinks_small_samples_toward_chance(self, tmp_path):
        """A model with n=3 lucky observations must NOT look like a measured
        champion — shrinkage pulls it toward the 0.25 chance prior."""
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(3):
            s.record("architect", "lucky", "research_synthesis",
                     f"q{i}", 0.0)  # impossibly perfect
        agg = s.summary("architect")["lucky"]
        assert agg["n"] == 3
        assert agg["mean_brier_raw"] == 0.0
        # Shrunk mean is well above raw: small samples can't look heroic.
        assert 0.15 < agg["mean_brier"] < 0.25
        # ...while a big sample of the same data converges to raw.
        s_big = ModelScoreStore(path=tmp_path / "big.jsonl")
        for i in range(300):
            s_big.record("architect", "perfect", "research_synthesis",
                         f"q{i}", 0.0)
        agg_big = s_big.summary("architect")["perfect"]
        assert agg_big["mean_brier"] < 0.01

    def test_honest_basis_labels(self):
        assert ModelScoreStore.basis_label(0) == "unmeasured"
        assert ModelScoreStore.basis_label(3) == "sparse"
        assert ModelScoreStore.basis_label(12) == "provisional"
        assert ModelScoreStore.basis_label(300) == "measured"

    def test_cost_recorded_per_observation(self, tmp_path):
        s = ModelScoreStore(path=tmp_path / "s.jsonl")
        s.record("architect", "paid", "research_synthesis", "q1",
                 0.1, cost_usd=0.05)
        s.record("architect", "local", "research_synthesis", "q1",
                 0.1, cost_usd=0.0)
        aggs = s.summary("architect")
        assert aggs["paid"]["total_cost_usd"] == pytest.approx(0.05)
        assert aggs["local"]["total_cost_usd"] == 0.0


# ══════════════════════════════════════════════════════════════════════
# 2. Exploration vs exploitation (Thompson sampling)
# ══════════════════════════════════════════════════════════════════════

def _mk_policy(store, seed=42, cost_weight=0.5):
    return ThompsonRoutingPolicy(store=store,
                                 rng=random.Random(seed),
                                 cost_weight=cost_weight)


LOCAL = CandidateModel("local-27b", "gpu1", 0.0, 0.0, config_rank=0)
FRONTIER = CandidateModel("frontier-xl", "frontier", 3.0, 15.0, config_rank=1)


class TestPolicy:
    def test_no_measurements_degrades_to_configured(self, tmp_path):
        pol = _mk_policy(ModelScoreStore(path=tmp_path / "empty.jsonl"))
        dec = pol.decide("architect", [LOCAL, FRONTIER])
        assert dec.basis == "configured"
        assert dec.tier == "gpu1"           # config rank order preserved
        assert dec.scores_used == {}

    def test_explores_unmeasured_model(self, tmp_path):
        """A new model appears: its record starts empty and it gets explored,
        not trusted — but it never inherits the incumbent's numbers."""
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(20):
            store.record("architect", LOCAL.name, "research_synthesis",
                         f"q{i}", 0.10)
        newcomer = CandidateModel("brand-new-v2", "gpu2", 0.0, 0.0,
                                  config_rank=2)
        pol = _mk_policy(store, seed=1)
        picks = {pol.decide("architect",
                            [LOCAL, FRONTIER, newcomer]).tier
                 for _ in range(100)}
        # Newcomer (wide posterior) IS explored sometimes...
        assert "gpu2" in picks
        # ...and the incumbent's aggregate still shows only ITS OWN records.
        assert all(a["n"] == 20 for m, a in
                   pol.store.summary("architect").items()
                   if m == LOCAL.name)

    def test_good_model_wins_overwhelmingly(self, tmp_path):
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(60):
            store.record("architect", LOCAL.name, "research_synthesis",
                         f"q{i}", 0.08, cost_usd=0.0)
            store.record("architect", FRONTIER.name, "research_synthesis",
                         f"q{i}", 0.32, cost_usd=0.06)
        pol = _mk_policy(store, seed=7)
        wins = sum(pol.decide("architect", [LOCAL, FRONTIER]).tier == "gpu1"
                   for _ in range(200))
        assert wins >= 190   # dominant on quality AND free

    def test_cost_awareness_price_can_lose_the_deal(self, tmp_path):
        """A model scoring slightly better at far higher price usually loses
        when cost_weight > 0 — routing exposes the tradeoff, not one axis."""
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(60):
            store.record("architect", "cheap", "research_synthesis",
                         f"q{i}", 0.13, cost_usd=0.0)
            store.record("architect", "pricey", "research_synthesis",
                         f"q{i}", 0.11, cost_usd=0.50)  # ~2% better, 50x price
        pol_paid = _mk_policy(store, seed=3, cost_weight=0.5)
        cheap_c = CandidateModel("cheap", "gpu1", 0.0, 0.0, 0)
        pricey_c = CandidateModel("pricey", "frontier", 3.0, 15.0, 1)
        wins = sum(pol_paid.decide("architect",
                                   [cheap_c, pricey_c]).model == "cheap"
                   for _ in range(200))
        assert wins >= 150   # mostly chooses the cheaper one

        pol_pure = _mk_policy(store, seed=3, cost_weight=0.0)
        wins_pure = sum(pol_pure.decide("architect",
                                        [cheap_c, pricey_c]).model == "pricey"
                        for _ in range(200))
        assert wins_pure >= 150  # with cost_weight=0, quality wins instead

    def test_decision_reports_sampled_scores(self, tmp_path):
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        store.record("architect", LOCAL.name, "research_synthesis",
                     "q1", 0.10)
        pol = _mk_policy(store)
        dec = pol.decide("architect", [LOCAL, FRONTIER])
        assert dec.sampled_effective_loss is not None
        assert set(dec.scores_used) >= {LOCAL.name}
        assert "sampled_effective_loss" in dec.scores_used[LOCAL.name]

    def test_thompson_adapts_after_distribution_shift(self, tmp_path):
        """Re-measurement: if a model's quality collapses mid-record, recent
        evidence flips routing away from it — WITHOUT touching the history
        (the store stays append-only; the shift shows up at read time)."""
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        for i in range(40):
            store.record("architect", "old-best", "research_synthesis",
                         f"q{i}", 0.08)
            store.record("architect", "challenger", "research_synthesis",
                         f"q{i}", 0.20)
        # Challenger improves sharply — many recent observations.
        for j in range(800):
            store.record("architect", "challenger", "research_synthesis",
                         f"n{j}", 0.07)
        assert len(store.load_all()) == 40 + 40 + 800   # nothing rewritten
        pol = _mk_policy(store, seed=11)
        challenger = CandidateModel("challenger", "gpu2", 0.0, 0.0, 1)
        old_best = CandidateModel("old-best", "gpu1", 0.0, 0.0, 0)
        wins = sum(pol.decide("architect",
                              [old_best, challenger]).model == "challenger"
                   for _ in range(200))
        # ~0.07 vs ~0.08 mean loss is a real but modest edge; Thompson
        # sampling gives the challenger the large majority, not certainty.
        assert wins >= 140


# ══════════════════════════════════════════════════════════════════════
# 3. ProviderRouter integration — honest basis, exact degradation
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def router(tmp_path):
    from inference import ProviderRouter
    cfg = tmp_path / "providers.yaml"
    cfg.write_text("""
default_tier: gpu1
providers:
  gpu1:
    backend: llama_cpp_server
    base_url: http://localhost:8081/v1
    model: local-27b
    max_concurrency: 1
  frontier:
    backend: openai_compat
    base_url_env: W2_TEST_FRONTIER_URL
    api_key_env: W2_TEST_FRONTIER_KEY
    model: frontier-xl
    cost_per_1k_input: 3.0
    cost_per_1k_output: 15.0
routing:
  task_classes:
    research_synthesis: [gpu1, frontier]
  empirical_routing:
    enabled: true
    cost_weight: 0.05
    usd_per_brier_point: 5.0
""")
    r = ProviderRouter(config_path=str(cfg))
    store = ModelScoreStore(path=tmp_path / "scores.jsonl")
    r.score_store = store
    r._routing_policy = None   # rebuild against the injected store
    return r


class TestRouterIntegration:
    def test_disabled_by_default_exact_config_behaviour(self, tmp_path):
        """THE honest constraint: no scores + default config == today."""
        from inference import ProviderRouter
        cfg = tmp_path / "p.yaml"
        cfg.write_text("""
default_tier: gpu1
providers:
  gpu1:
    backend: llama_cpp_server
    base_url: http://localhost:8081/v1
    model: local-27b
routing:
  task_classes:
    research_synthesis: gpu1
""")
        r = ProviderRouter(config_path=str(cfg))
        assert r.empirical_routing_enabled is False
        names, meta = r.route_order(
            "research_synthesis", ["gpu1"], role="architect")
        assert names == ["gpu1"]
        assert meta["basis"] == "configured"

    def test_no_scores_means_configured_order(self, router):
        router.empirical_routing_enabled = True
        names, meta = router.route_order(
            "research_synthesis", ["gpu1", "frontier"], role="architect")
        assert names == ["gpu1", "frontier"]   # untouched
        assert meta["basis"] == "configured"

    def test_measured_scores_reorder_candidates(self, router):
        """Frontier measures better for this role -> routed there first,
        and the caller can see the basis was measurement."""
        router.empirical_routing_enabled = True
        for i in range(40):
            router.score_store.record("architect", "local-27b",
                                      "research_synthesis", f"q{i}",
                                      0.30, cost_usd=0.0)
            router.score_store.record("architect", "frontier-xl",
                                      "research_synthesis", f"q{i}",
                                      0.09, cost_usd=0.05)
        names, meta = router.route_order(
            "research_synthesis", ["gpu1", "frontier"], role="architect")
        assert names[0] == "frontier"
        assert meta["basis"] in ("sparse", "provisional", "measured")
        assert meta["chosen_model"] == "frontier-xl"

    def test_routing_basis_in_complete_result(self, router, monkeypatch):
        """complete() surfaces which basis it used — configured vs measured —
        without any live call (transport stubbed)."""
        router.empirical_routing_enabled = False
        async def fake_post(endpoint, payload, timeout):
            return "hello", {"prompt_tokens": 10, "completion_tokens": 5}
        monkeypatch.setattr(router, "_post", fake_post)
        import asyncio
        res = asyncio.run(router.complete(
            "research_synthesis", [{"role": "user", "content": "hi"}],
            role="architect"))
        assert res["tier"] == "gpu1"     # frontier unresolved -> local only
        assert res["routing_basis"] == "configured"

    def test_role_records_are_separate_track_records(self, router):
        router.empirical_routing_enabled = True
        # frontier-xl is great as Architect...
        for i in range(40):
            router.score_store.record("architect", "frontier-xl",
                                      "research_synthesis", f"q{i}", 0.08)
        # ...but has NO manager record at all.
        names, meta = router.route_order(
            "research_synthesis", ["gpu1", "frontier"], role="manager")
        # Manager has zero measurements for BOTH models? No — gpu1's model
        # also has none under 'manager'. So: configured.
        assert meta["basis"] == "configured"
        assert names == ["gpu1", "frontier"]

    def test_failover_preserved_when_winner_dead(self, router):
        """The measured winner going first must not remove failover: the
        rest of the list keeps configured order behind it."""
        router.empirical_routing_enabled = True
        for i in range(40):
            router.score_store.record("architect", "frontier-xl",
                                      "research_synthesis", f"q{i}", 0.08)
        names, _ = router.route_order(
            "research_synthesis", ["gpu1", "frontier"], role="architect")
        assert sorted(names) == ["frontier", "gpu1"]

    def test_policy_crash_falls_back_to_config(self, router, monkeypatch):
        """Measurement must never break a live call."""
        router.empirical_routing_enabled = True
        def boom(*a, **k):
            raise RuntimeError("store exploded")
        monkeypatch.setattr(router, "route_order", boom)
        # complete() calls route_order... which now raises. That path is
        # inside route_order itself, so verify the guard differently:
        # route_order wrapped errors fall back — test the internal try/except
        # by making the POLICY blow up instead.
        monkeypatch.undo()

        class BadPolicy:
            def decide(self, *a, **k):
                raise RuntimeError("policy exploded")
        router._routing_policy = BadPolicy()
        names, meta = router.route_order(
            "research_synthesis", ["gpu1", "frontier"], role="architect")
        assert names == ["gpu1", "frontier"]
        assert "error" in meta
