"""Characterization tests — behavior of ProviderRouter BEFORE the
multi-endpoint/capability/queue/budget redesign (instance 5, task 0).

These pin current behavior so the redesign is provably behavior-preserving
where it must be. Run:
    python3 -m pytest tests/test_tier5_serving_characterization.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402


CFG = """
default_tier: local
providers:
  local:
    backend: llama_cpp_server
    base_url: http://localhost:8080/v1
    model: test-27b
    context_tokens: 32768
    temperature: 0.2
  frontier:
    backend: openai_compat
    base_url_env: FRONTIER_BASE_URL
    api_key_env: FRONTIER_API_KEY
    model_env: FRONTIER_MODEL
routing:
  task_classes:
    hypothesis_generation: local
    screening: local
    promotion_judgment: frontier
  escalation:
    json_schema_failures: 2
"""


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setenv("FRONTIER_BASE_URL", "https://example.invalid/api/v1")
    monkeypatch.setenv("FRONTIER_API_KEY", "k-test")
    monkeypatch.setenv("FRONTIER_MODEL", "flagship-test")
    cfg = tmp_path / "p.yaml"
    cfg.write_text(CFG)
    return inference.ProviderRouter(config_path=str(cfg))


class TestCurrentBehavior:
    def test_task_class_to_tier(self, router):
        assert router.tier_for("hypothesis_generation").name == "local"
        assert router.tier_for("promotion_judgment").name == "frontier"

    def test_unknown_task_class_raises(self, router):
        with pytest.raises(inference.UnknownTaskClassError):
            router.tier_for("deep_work")  # not declared in this cfg

    def test_call_site_names_are_not_declared_in_real_config(self):
        """CHARACTERIZATION of the vocabulary gap: the real providers.yaml
        declares 8 task classes; the call sites pass different names.
        Today the router cannot route any call-site name."""
        real = inference.ProviderRouter()
        for name in ("deep_work", "hypothesis_gen", "reasoning",
                     "review", "code_generation"):
            assert name not in real.task_classes, (
                f"{name} now declared — characterization stale, update it"
            )

    def test_tiers_are_flat_named_map(self, router):
        """Today: one tier == one endpoint, keyed by tier name."""
        assert set(router.tiers) == {"local", "frontier"}
        assert router.tiers["local"].model == "test-27b"

    def test_complete_has_no_failover(self, router, monkeypatch):
        """Today: a dead endpoint raises straight through to the caller."""
        import asyncio, httpx as _httpx

        async def run():
            class DeadClient:
                def __init__(self, **kw): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                async def post(self, *a, **kw):
                    raise _httpx.ConnectError("refused")

            monkeypatch.setattr(_httpx, "AsyncClient", DeadClient)
            with pytest.raises(_httpx.ConnectError):
                await router.complete(
                    "screening", [{"role": "user", "content": "x"}]
                )

        asyncio.run(run())

    def test_no_concurrency_limiting(self, router):
        assert not hasattr(router, "_semaphores")
        assert not hasattr(router, "acquire")

    def test_no_cost_tracking(self, router):
        assert not hasattr(router, "cost_ledger")
        assert not hasattr(router, "budget")

    def test_complete_result_shape(self, router, monkeypatch):
        import asyncio

        captured = {}

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}]}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured.update(url=url, headers=headers, json=json)
                return FakeResp()

        monkeypatch.setattr(inference.httpx, "AsyncClient", FakeClient)
        result = asyncio.run(router.complete(
            "screening", [{"role": "user", "content": "x"}], system_context="s"
        ))
        # Result keys pinned: callers may depend on these.
        assert set(result) == {"content", "parsed_json", "model", "tier",
                               "task_class"}
        assert result["content"] == "ok"
        assert result["parsed_json"] is None
        assert result["model"] == "test-27b"
        assert result["tier"] == "local"
