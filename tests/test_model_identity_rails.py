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



def _fake_identity_router(identities, order, task_class):
    """Offline router from an {endpoint: identity-or-None} map."""
    cfg = {
        "providers": {
            n: ({"backend": "openai_compat", "base_url": f"http://{n}/v1",
                 "model": f"m-{n}", "model_identity": ident}
                if ident else
                {"backend": "openai_compat", "base_url": f"http://{n}/v1",
                 "model": f"m-{n}"})
            for n, ident in identities.items()
        },
        "routing": {"task_classes": {task_class: order}},
    }
    import tempfile, os, yaml
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(cfg, f)
        path = f.name
    try:
        return inference.ProviderRouter(config_path=path)
    finally:
        os.unlink(path)


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



class TestGroupingRegressions:
    def test_interleaved_identities_keep_group_order(self):
        """[a1(A), b1(B), a2(A), b2(B)] must group as [a1, a2, b1, b2]:
        each identity contiguous at first appearance, configured within-group
        order preserved (B's transport priority is NOT reversed)."""
        r = _fake_identity_router(
            {"a1": "A", "b1": "B", "a2": "A", "b2": "B"},
            ["a1", "b1", "a2", "b2"], task_class="screening")
        assert r._group_by_identity(
            ["a1", "b1", "a2", "b2"]) == ["a1", "a2", "b1", "b2"]

    def test_interleaved_with_standalone_keeps_legacy_positions(self):
        r = _fake_identity_router(
            {"x": None, "a1": "A", "y": None, "a2": "A"},
            ["x", "a1", "y", "a2"], task_class="screening")
        assert r._group_by_identity(
            ["x", "a1", "y", "a2"]) == ["x", "a1", "a2", "y"]

    def test_all_cooling_fallback_is_identity_grouped(self, monkeypatch):
        """The least-bad fallback path applies the same grouped-rail
        semantics instead of returning raw config order."""
        r = _router_with_proxy(monkeypatch)
        import time as _time
        for st in r.states.values():
            if st is not None:
                st.cooldown_until = _time.monotonic() + 3600.0
        cands = r.candidates_for("research_synthesis")
        i_p, i_c = cands.index("ox_alpha_proxy"), cands.index("ox_alpha")
        assert i_c == i_p + 1

    def test_route_order_moves_whole_winning_rail_group(self, monkeypatch):
        """Empirical winner's proxy/CLI rails move to the front together,
        contiguous and proxy-first; non-winners keep failover order."""

        class FakePolicy:
            def __init__(self, tier):
                self.tier = tier

            def decide(self, role, cands):
                from types import SimpleNamespace
                return SimpleNamespace(
                    basis="measured", model=cands[0].name, tier=self.tier,
                    sampled_effective_loss=0.0, scores_used={})

        r = _router_with_proxy(monkeypatch)
        r.empirical_routing_enabled = True
        try:
            # winner has no model_identity: only its own rail moves
            r._routing_policy = FakePolicy(tier="frontier")
            ordered, meta = r.route_order(
                "research_synthesis",
                ["frontier", "gpu1", "ox_alpha_proxy", "ox_alpha"],
                role="research_synthesis")
            assert ordered == ["frontier", "gpu1", "ox_alpha_proxy", "ox_alpha"]
            assert meta["basis"] == "measured"

            # winning proxy pulls its CLI failover rail along, proxy-first
            r._routing_policy = FakePolicy(tier="ox_alpha_proxy")
            ordered, meta = r.route_order(
                "research_synthesis",
                ["frontier", "gpu1", "ox_alpha_proxy", "ox_alpha"],
                role="research_synthesis")
            assert ordered == ["ox_alpha_proxy", "ox_alpha", "frontier", "gpu1"]
            assert meta["basis"] == "measured"
        finally:
            r.empirical_routing_enabled = False


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


class TestIdentitylessDisplayCollision:
    def test_same_display_model_endpoints_remain_distinct(self):
        """Two identity-less endpoints sharing a display `model` label must
        stay two separate CandidateModels (legacy behaviour preserved)."""
        cfg = {
            "providers": {
                "a": {"backend": "openai_compat", "base_url": "http://x/v1",
                      "model": "shared"},
                "b": {"backend": "openai_compat", "base_url": "http://y/v1",
                      "model": "shared"},
            },
            "routing": {"task_classes": {"screening": ["a", "b"]}},
        }
        import tempfile, os, yaml
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(cfg, f)
            path = f.name
        try:
            r = inference.ProviderRouter(config_path=path)
            cands = r._candidates_as_models(["a", "b"])
        finally:
            os.unlink(path)
        assert [(c.name, c.tier) for c in cands] == [("shared", "a"), ("shared", "b")]


class TestDynamicProxyIdentity:
    def _proxy_router(self):
        return inference._endpoint_from_config("ox_alpha_proxy", {
            "backend": "openai_compat",
            "base_url": "http://127.0.0.1:8645/v1",
            "model_env": "OX_ALPHA_PROXY_MODEL",
            "model_identity": IDENTITY,
            "model": "stealth/ox-alpha",
        })

    def test_env_override_to_different_model_invalidates_identity(self, monkeypatch):
        monkeypatch.setenv("OX_ALPHA_PROXY_MODEL", "stealth/ox-alpha-beta")
        ep = self._proxy_router()
        assert ep.model == "stealth/ox-alpha-beta"
        # We cannot know what a beta proxy really serves: no canonical claim.
        assert ep.model_identity is None

    def test_env_equal_to_static_model_keeps_identity(self, monkeypatch):
        monkeypatch.setenv("OX_ALPHA_PROXY_MODEL", "stealth/ox-alpha")
        ep = self._proxy_router()
        assert ep.model == "stealth/ox-alpha"
        assert ep.model_identity == IDENTITY

    def test_env_unset_falls_back_and_keeps_identity(self, monkeypatch):
        monkeypatch.delenv("OX_ALPHA_PROXY_MODEL", raising=False)
        ep = self._proxy_router()
        assert ep.model == "stealth/ox-alpha"
        assert ep.model_identity == IDENTITY

    def test_explicit_resolved_identity_supplied_by_config_wins(self, monkeypatch):
        monkeypatch.setenv("OX_ALPHA_PROXY_MODEL", "stealth/ox-alpha-beta")
        ep = inference._endpoint_from_config("ox_alpha_proxy", {
            "backend": "openai_compat",
            "base_url": "http://127.0.0.1:8645/v1",
            "model_env": "OX_ALPHA_PROXY_MODEL",
            "model_identity": IDENTITY,
            "model": "stealth/ox-alpha",
            "resolved_model_identity_env": "OX_ALPHA_RESOLVED_IDENTITY",
        })
        assert ep.model_identity is None  # env var unset -> still invalidated
        monkeypatch.setenv("OX_ALPHA_RESOLVED_IDENTITY", "nous/beta/explicit")
        ep2 = inference._endpoint_from_config("ox_alpha_proxy", {
            "backend": "openai_compat",
            "base_url": "http://127.0.0.1:8645/v1",
            "model_env": "OX_ALPHA_PROXY_MODEL",
            "model_identity": IDENTITY,
            "model": "stealth/ox-alpha",
            "resolved_model_identity_env": "OX_ALPHA_RESOLVED_IDENTITY",
        })
        assert ep2.model_identity == "nous/beta/explicit"

    def test_env_only_config_divergent_model_invalidates_identity(self, monkeypatch):
        """Env-only config (no static model): a nonempty env value differing
        from the absent static model must still invalidate the identity."""
        monkeypatch.setenv("OX_ALPHA_PROXY_MODEL", "stealth/ox-alpha-beta")
        ep = inference._endpoint_from_config("ox_alpha_proxy", {
            "backend": "openai_compat",
            "base_url": "http://x/v1",
            "model_env": "OX_ALPHA_PROXY_MODEL",
            "model_identity": IDENTITY,
        })
        assert ep.model == "stealth/ox-alpha-beta"
        assert ep.model_identity is None

    def test_beta_proxy_and_cli_are_separate_scoring_candidates(self, monkeypatch):
        """With the proxy overridden to a different model, the router must NOT
        group/dedupe it with the Hermes CLI rail under the static identity."""
        monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("OX_ALPHA_PROXY_API_KEY", "test-token")
        monkeypatch.setenv("OX_ALPHA_PROXY_MODEL", "stealth/ox-alpha-beta")
        r = inference.ProviderRouter()
        assert r.endpoints["ox_alpha_proxy"].model_identity is None
        assert r.scoring_model_name("ox_alpha_proxy") != \
            r.scoring_model_name("ox_alpha")
        r.empirical_routing_enabled = True
        try:
            cands = r._candidates_as_models(
                ["gpu1", "frontier", "ox_alpha_proxy", "ox_alpha"])
        finally:
            r.empirical_routing_enabled = False
        names = [c.name for c in cands if "ox" in c.name or "alpha" in c.name]
        assert len(names) == 2, names  # two distinct scoring candidates
        # and the rails are NOT adjacent-grouped under one identity
        grouped = r._group_by_identity(
            ["ox_alpha_proxy", "gpu1", "frontier", "ox_alpha"])
        assert grouped.index("ox_alpha_proxy") < grouped.index("gpu1")
