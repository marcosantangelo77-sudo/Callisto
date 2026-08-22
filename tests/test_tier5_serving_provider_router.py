"""Tier 5 serving/inference — ProviderRouter tests (instance 5).

Targeted subset only; run with:
    python3 -m pytest tests/test_tier5_serving_provider_router.py -x -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

import inference  # noqa: E402


@pytest.fixture
def router_cfg(tmp_path):
    cfg = tmp_path / "providers_test.yaml"
    cfg.write_text(
        """
default_tier: local
providers:
  local:
    backend: llama_cpp_server
    base_url: http://localhost:8080/v1
    api_key_env: null
    model: test-27b
    context_tokens: 4096
    temperature: 0.2
  local_fast:
    backend: llama_cpp_server
    base_url: http://localhost:8081/v1
    model: test-4b
    context_tokens: 2048
    temperature: 0.1
  frontier:
    backend: openai_compat
    base_url_env: FRONTIER_BASE_URL
    api_key_env: FRONTIER_API_KEY
    model_env: FRONTIER_MODEL
    context_tokens: 200000
routing:
  task_classes:
    hypothesis_generation: local
    screening: local_fast
    promotion_judgment: frontier
  escalation:
    json_schema_failures: 2
    confidence_below: 0.60
"""
    )
    return cfg


@pytest.fixture
def router(router_cfg, monkeypatch):
    monkeypatch.setenv("FRONTIER_BASE_URL", "https://example.invalid/api/v1")
    monkeypatch.setenv("FRONTIER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("FRONTIER_MODEL", "flagship-test")
    return inference.ProviderRouter(config_path=str(router_cfg))


class TestTaskClassRouting:
    def test_task_class_maps_to_tier(self, router):
        assert router.tier_for("hypothesis_generation").name == "local"
        assert router.tier_for("screening").name == "local_fast"
        assert router.tier_for("promotion_judgment").name == "frontier"

    def test_unknown_task_class_is_loud(self, router):
        # A typo'd task_class must never silently fall back to default tier.
        with pytest.raises(inference.UnknownTaskClassError):
            router.tier_for("hypothsis_generation")

    def test_frontier_env_resolution(self, router):
        tier = router.tier_for("promotion_judgment")
        assert tier.base_url == "https://example.invalid/api/v1"
        assert tier.model == "flagship-test"
        assert tier.api_key == "test-key-not-real"

    def test_missing_frontier_base_url_raises(self, router_cfg, monkeypatch):
        monkeypatch.delenv("FRONTIER_BASE_URL", raising=False)
        router = inference.ProviderRouter(config_path=str(router_cfg))
        # Unresolved env-backed endpoint: construction stays local-only-safe,
        # but asking for it raises LOUDLY instead of silently falling back.
        with pytest.raises(RuntimeError, match="base_url"):
            router.tier_for("promotion_judgment")
        # And it must not appear in candidates for routing.
        assert "frontier" not in router.candidates_for("promotion_judgment")


class TestPayloadShape:
    def test_schema_becomes_json_schema_response_format(self, router):
        tier = router.tier_for("hypothesis_generation")
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        payload = router._payload(tier, [{"role": "user", "content": "hi"}], schema, None, None)
        rf = payload["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == schema
        assert payload["model"] == "test-27b"
        assert payload["temperature"] == 0.2

    def test_no_schema_no_response_format(self, router):
        tier = router.tier_for("screening")
        payload = router._payload(tier, [], None, None, 512)
        assert "response_format" not in payload
        assert payload["max_tokens"] == 512

    def test_system_context_prepended(self, router):
        msgs = router.build_messages([{"role": "user", "content": "q"}], "sys")
        assert [m["role"] for m in msgs] == ["system", "user"]

    def test_complete_posts_to_tier_url(self, router, monkeypatch):
        import asyncio

        async def run():
            captured = {}

            class FakeResp:
                status_code = 200
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"choices": [{"message": {"content": '{"answer": 42}'}}]}

            class FakeClient:
                def __init__(self, **kw):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                async def post(self, url, json=None, headers=None):
                    captured["url"] = url
                    return FakeResp()

            import httpx as _httpx
            monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
            return await router.complete(
                "screening",
                [{"role": "user", "content": "classify"}],
                system_context="be terse",
            ), captured

        result, captured = asyncio.run(run())
        assert result["parsed_json"] == {"answer": 42}
        assert result["tier"] == "local_fast"
        assert captured["url"].startswith("http://localhost:8081/v1")

    def test_real_config_loads(self):
        """The actual repo providers.yaml must parse and every LOCAL task
        class must resolve. The frontier endpoint is env-backed: unresolved
        here, and asking for it must raise LOUDLY rather than silently
        falling back. Call-site legacy names must also route (vocabulary
        bridge)."""
        real = inference.ProviderRouter()
        # Vocabulary bridge: call-site names are accepted.
        for legacy in ("deep_work", "hypothesis_gen", "reasoning",
                       "review", "code_generation"):
            assert real.canonical_task_class(legacy) in real.task_classes
        for tc in list(real.task_classes) + [
            real.canonical_task_class(x) for x in
            ("deep_work", "hypothesis_gen", "reasoning", "review",
             "code_generation")
        ]:
            try:
                real.tier_for(tc)
            except RuntimeError:
                # Only env-unresolved endpoints may fail this way.
                names = real.task_classes[tc]
                names = names if isinstance(names, list) else [names]
                assert any(
                    real.endpoints[n].extra.get("_unresolved") for n in names
                ), f"task class {tc} failed without an unresolved-endpoint excuse"
            except inference.UnknownTaskClassError:
                pytest.fail(f"declared task_class {tc} failed lookup")
        # Local endpoints must be usable with no env vars at all.
        assert not any(
            ep.extra.get("_unresolved")
            for n, ep in real.endpoints.items()
            if not ep.backend.startswith("openai") or ep.base_url
        )


class TestLegacySurfaceIntact:
    """escalate_with_ladder / OllamaInference importers must keep working."""

    def test_ladder_and_parse_helpers_exist(self):
        assert callable(inference.escalate_with_ladder)
        assert callable(inference._parse_json_response)
        assert callable(inference.OllamaInference)

    def test_vendored_validator_wired(self):
        validate = inference._get_hermes_validator()
        ok, err = validate(
            {"name": "f", "arguments": {"n": True}},
            [{
                "type": "function",
                "function": {
                    "name": "f",
                    "parameters": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                    },
                },
            }],
        )
        # The upstream validator passed bools as ints; jsonschema must not.
        assert not ok and err is not None
