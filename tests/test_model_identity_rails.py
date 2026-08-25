"""Canonical model identity for the two OX transport rails.

`ox_alpha_proxy` (persistent HTTP) and `ox_alpha` (fresh-fork Hermes CLI)
serve the SAME model — Nous `stealth/ox-alpha`. The router must treat them
as ONE model choice, not two independent models:

1. Both endpoint configs declare model_identity="nous/stealth/ox-alpha"
   while keeping their existing display `model` values and wire targets.
2. Candidate grouping preserves configured transport priority (proxy first,
   CLI failover) and never splits one identity across the list.
3. Empirical-routing candidates dedupe to ONE scoring candidate per identity
   (empirical routing itself stays disabled by default).
4. Endpoints without model_identity keep exact legacy behaviour.

All offline; fakes only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402


@pytest.fixture(scope="module")
def real_router():
    return inference.ProviderRouter()


def _router_with_proxy(monkeypatch):
    monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OX_ALPHA_PROXY_API_KEY", "test-token")
    return inference.ProviderRouter()


IDENTITY = "nous/stealth/ox-alpha"


class TestDeclaration:
    def test_both_ox_rails_share_identity(self, real_router):
        for name in ("ox_alpha", "ox_alpha_proxy"):
            ep = real_router.endpoints[name]
            assert ep.model_identity == IDENTITY, name

    def test_display_models_preserved(self, real_router):
        assert real_router.endpoints["ox_alpha"].model == "ox-alpha"
        assert real_router.endpoints["ox_alpha_proxy"].model == "stealth/ox-alpha"

    def test_wire_target_preserved(self, real_router):
        extra = real_router.endpoints["ox_alpha"].extra
        assert extra.get("provider") == "nous"
        assert extra.get("model") == "stealth/ox-alpha"

    def test_unrelated_endpoints_have_no_identity(self, real_router):
        for name, ep in real_router.endpoints.items():
            if name not in ("ox_alpha", "ox_alpha_proxy"):
                assert ep.model_identity is None, name


class TestGrouping:
    def test_rails_grouped_with_proxy_first(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        cands = r.candidates_for("research_synthesis")
        i_proxy = cands.index("ox_alpha_proxy")
        i_cli = cands.index("ox_alpha")
        # proxy first, CLI directly after it as its failover rail
        assert i_cli == i_proxy + 1

    @pytest.mark.parametrize("tc", [
        "hypothesis_generation", "research_synthesis", "screening",
        "extraction", "classification", "backtest_interpretation",
        "promotion_judgment", "adversarial_review",
    ])
    def test_identity_never_splits_in_any_class(self, monkeypatch, tc):
        r = _router_with_proxy(monkeypatch)
        cands = r.candidates_for(tc)
        gap = abs(cands.index("ox_alpha") - cands.index("ox_alpha_proxy"))
        assert gap == 1, (tc, cands)

    def test_unresolved_proxy_leaves_cli_untouched(self, monkeypatch):
        for v in ("OX_ALPHA_PROXY_BASE_URL", "OX_ALPHA_PROXY_API_KEY",
                  "OX_ALPHA_PROXY_MODEL"):
            monkeypatch.delenv(v, raising=False)
        r = inference.ProviderRouter()
        cands = r.candidates_for("research_synthesis")
        assert "ox_alpha_proxy" not in cands
        assert cands[-1] == "ox_alpha"


class TestScoringCandidate:
    def test_scoring_name_shared_across_rails(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        assert (r.scoring_model_name("ox_alpha_proxy")
                == r.scoring_model_name("ox_alpha") == IDENTITY)

    def test_empirical_candidates_dedupe_to_one(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        r.empirical_routing_enabled = True  # flip ONLY inside this test
        try:
            cands = r._candidates_as_models(
                ["gpu1", "frontier", "ox_alpha_proxy", "ox_alpha"])
            ox = [c.name for c in cands if "ox" in c.name]
            assert ox == [IDENTITY], ox
        finally:
            r.empirical_routing_enabled = False

    def test_route_order_single_choice_per_identity(self, monkeypatch):
        """With empirical routing on and both rails measured under the same
        canonical name, the decision must still be one model whose tier keeps
        proxy-first ordering intact."""
        from tools.routing.policy import ThompsonRoutingPolicy

        class FakeStore:
            def __init__(self):
                self.recs = []
            def records_for(self, role, model):
                return self.recs
            def summary(self, role=None):
                return {}

        r = _router_with_proxy(monkeypatch)
        r.empirical_routing_enabled = True
        store = FakeStore()
        store.summary = lambda role=None: {}
        r.score_store = store
        r._routing_policy = ThompsonRoutingPolicy(store=store)
        try:
            ordered, meta = r.route_order(
                "research_synthesis",
                ["gpu1", "frontier", "ox_alpha_proxy", "ox_alpha"],
                role="research_synthesis")
        finally:
            r.empirical_routing_enabled = False
        # no measurements -> basis configured, order untouched
        assert meta["basis"] in ("configured", "unmeasured")
        i_p, i_c = ordered.index("ox_alpha_proxy"), ordered.index("ox_alpha")
        assert i_c == i_p + 1 or i_p > i_c or i_p < i_c  # shape preserved
        # strict: rails stay adjacent
        assert abs(i_p - i_c) == 1


class TestMissingIdentityCompat:
    def test_missing_identity_endpoints_stand_alone(self):
        cfg = {
            "providers": {
                "a": {"backend": "openai_compat", "base_url": "http://x/v1",
                      "model": "m-a"},
                "b": {"backend": "openai_compat", "base_url": "http://y/v1",
                      "model": "m-b"},
            },
            "routing": {"task_classes": {"screening": ["a", "b"]}},
        }
        import tempfile, os, yaml
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(cfg, f)
            path = f.name
        try:
            r = inference.ProviderRouter(config_path=path)
        finally:
            os.unlink(path)
        assert all(ep.model_identity is None
                   for ep in r.endpoints.values())
        assert r.candidates_for("screening") == ["a", "b"]
        assert (r.scoring_model_name("a"),
                r.scoring_model_name("b")) == ("m-a", "m-b")

    def test_historic_scores_not_touched(self, tmp_path):
        """No migration: existing score rows keyed by other labels are left
        exactly as they are."""
        from tools.routing.scores import ModelScoreStore
        store = ModelScoreStore(path=tmp_path / "s.jsonl")
        rec = store.record(role="r", model="hermes-cli", task_class="t",
                           question_id="q", brier=0.4)
        assert store.load_all() == [rec]  # untouched, unaliased
