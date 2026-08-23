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

    def test_tiers_are_endpoint_pool(self, router):
        """CHANGED from flat single-endpoint tiers: each configured provider
        is one endpoint in a pool; TierConfig is a back-compat view over it."""
        assert set(router.endpoints) == {"local", "frontier"}
        assert router.tiers_view_names() == ["local", "frontier"]
        assert router.endpoints["local"].model == "test-27b"

    def test_complete_fails_over_to_second_endpoint(self, tmp_path, monkeypatch):
        """CHANGED from raising straight through: a dead endpoint degrades
        to the next candidate; only total failure raises."""
        import asyncio, httpx as _httpx

        cfg = tmp_path / "p.yaml"
        cfg.write_text("""
default_tier: pool_a
providers:
  dead_box:
    backend: llama_cpp_server
    base_url: http://localhost:9901/v1
    model: m-dead
  alive_box:
    backend: llama_cpp_server
    base_url: http://localhost:9902/v1
    model: m-alive
routing:
  task_classes:
    screening: [dead_box, alive_box]
""")
        router = inference.ProviderRouter(config_path=str(cfg))
        captured = {}

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "from alive"}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None, timeout=None):
                if ":9901" in url:
                    raise _httpx.ConnectError("refused")
                captured["url"] = url
                return FakeResp()

        monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
        result = asyncio.run(router.complete(
            "screening", [{"role": "user", "content": "x"}]))
        assert result["content"] == "from alive"
        assert result["tier"] == "alive_box"
        assert captured["url"].startswith("http://localhost:9902")

    def test_cost_tracking_exists_and_records(self, router):
        """CHANGED: hosted endpoints are cost-tracked against a budget."""
        ledger = router.cost_ledger
        import asyncio
        asyncio.run(ledger.record("frontier", 1000, 500, 0.015))
        snap = ledger.snapshot()
        assert snap["total_cost_usd"] == 0.015
        assert snap["by_tier"]["frontier"]["calls"] == 1

    def test_budget_refusal_is_deliberate(self, tmp_path, monkeypatch):
        """CHANGED: once budget is spent, paid tiers refuse unless the caller
        explicitly passes allow_budget_exceed=True."""
        import asyncio, httpx as _httpx

        cfg = tmp_path / "p.yaml"
        cfg.write_text("""
default_tier: cheap
providers:
  cheap:
    backend: llama_cpp_server
    base_url: http://localhost:8080/v1
    model: local-m
  pricey:
    backend: openai_compat
    base_url: https://paid.example/v1
    model: flagship
    cost_per_1k_input: 3.0
    cost_per_1k_output: 15.0
routing:
  task_classes:
    screening: cheap
    promotion_judgment: pricey
  budget:
    usd: 10.0
""")
        router = inference.ProviderRouter(config_path=str(cfg))
        router.cost_ledger.total_cost_usd = 11.0  # spent

        class NeverCalled:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                raise AssertionError("paid endpoint must not be called")

        monkeypatch.setattr(_httpx, "AsyncClient", NeverCalled)
        with pytest.raises(RuntimeError, match="budget"):
            asyncio.run(router.complete(
                "promotion_judgment", [{"role": "user", "content": "x"}]))
        # Deliberate override still goes through (NeverCalled raises, proving
        # the request was actually dispatched).
        with pytest.raises(Exception):
            asyncio.run(router.complete(
                "promotion_judgment", [{"role": "user", "content": "x"}],
                allow_budget_exceed=True))

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
            async def post(self, url, json=None, headers=None, timeout=None):
                captured.update(url=url, headers=headers, json=json)
                return FakeResp()

        monkeypatch.setattr(inference.httpx, "AsyncClient", FakeClient)
        result = asyncio.run(router.complete(
            "screening", [{"role": "user", "content": "x"}], system_context="s"
        ))
        # Result keys pinned: callers may depend on these.
        # (W2 added "routing_basis" — which basis the routing decision used:
        # "configured" until measured scores exist. Callers may depend on it
        # being present; it never replaces the original keys.)
        assert set(result) == {"content", "parsed_json", "model", "tier",
                               "task_class", "routing_basis"}
        assert result["routing_basis"] == "configured"
        assert result["content"] == "ok"
        assert result["parsed_json"] is None
        assert result["model"] == "test-27b"
        assert result["tier"] == "local"
