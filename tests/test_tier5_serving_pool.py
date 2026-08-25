"""Tests for the endpoint-pool redesign: concurrency limits, health
cooldowns, capability-based selection. Run:
    python3 -m pytest tests/test_tier5_serving_pool.py -q
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import time as _time
import inference  # noqa: E402


POOL_CFG = """
default_tier: gpu1
providers:
  gpu1:
    backend: llama_cpp_server
    base_url: http://localhost:9901/v1
    model: m-27b
    context_tokens: 32768
    structured_output: true
    tool_calls: true
    max_concurrency: 2
  gpu1_fast:
    backend: llama_cpp_server
    base_url: http://localhost:9902/v1
    model: m-4b
    context_tokens: 16384
    structured_output: false
    tool_calls: false
    max_concurrency: 1
  spark:
    backend: llama_cpp_server
    base_url: http://localhost:9903/v1
    model: m-120b
    context_tokens: 128000
    structured_output: true
    tool_calls: true
    max_concurrency: 4
routing:
  task_classes:
    research_synthesis: [gpu1, spark]
    hypothesis_generation: [gpu1, spark]
    adversarial_review: spark
    screening: gpu1_fast
    promotion_judgment: spark
"""


@pytest.fixture
def router(tmp_path):
    cfg = tmp_path / "pool.yaml"
    cfg.write_text(POOL_CFG)
    return inference.ProviderRouter(config_path=str(cfg))


class TestCapabilityRouting:
    def test_schema_requires_structured_output_endpoint(self, router):
        # screening -> gpu1_fast has structured_output: false
        assert router.candidates_for("screening", schema={"type": "object"}) == []
        assert router.pick_endpoint("screening", schema={"type": "object"}) is None
        assert router.pick_endpoint("screening").name == "gpu1_fast"

    def test_pool_order_and_failover_candidates(self, router):
        assert router.candidates_for("research_synthesis") == ["gpu1", "spark"]

    def test_alias_routes_to_canonical(self, router):
        assert router.canonical_task_class("deep_work") == "research_synthesis"
        assert router.canonical_task_class("reasoning") == "research_synthesis"
        assert router.canonical_task_class("hypothesis_gen") == "hypothesis_generation"
        assert router.canonical_task_class("review") == "adversarial_review"
        assert router.canonical_task_class("code_generation") == "research_synthesis"

    def test_alias_unknown_still_loud(self, router):
        with pytest.raises(inference.UnknownTaskClassError):
            router.canonical_task_class("deep_wrok")


class TestConcurrencyAndBackpressure:
    def test_semaphore_limits_in_flight(self, router, monkeypatch):
        """max_concurrency=2 on gpu1: a third concurrent request must wait."""
        active = 0
        peak = 0

        class SlowResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        class SlowClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1
                return SlowResp()

        monkeypatch.setattr(httpx, "AsyncClient", SlowClient)

        async def run():
            await asyncio.gather(*(
                router.complete(
                    "research_synthesis", [{"role": "user", "content": "x"}]
                )
                for _ in range(5)
            ))

        asyncio.run(run())
        assert peak <= 2, f"peak in-flight {peak} exceeded max_concurrency=2"

    def test_in_flight_counter_tracked(self, router, monkeypatch):
        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                assert router.states["gpu1"].in_flight == 1
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        asyncio.run(router.complete(
            "research_synthesis", [{"role": "user", "content": "x"}]))
        assert router.states["gpu1"].in_flight == 0


class TestHealthAndFailover:
    def test_failure_cooldown_and_recovery(self, router):
        st = router.states["gpu1"]
        assert st.available
        st.record_failure()
        assert not st.available          # cooling down
        st.cooldown_until = 0.0
        assert st.available              # recovered
        # exponential: second failure cools 4s vs first's 2s
        st.record_failure()
        st.consecutive_failures = 0
        st.record_failure()
        c1 = st.cooldown_until - _time.monotonic()
        assert abs(c1 - 2.0) < 0.5
        st.record_failure()  # consecutive=2 now
        c2 = st.cooldown_until - _time.monotonic()
        assert c2 > c1 * 1.5
        st.consecutive_failures = 0

    def test_dead_endpoint_skipped_after_cooldown(self, router):
        router.states["gpu1"].record_failure()
        # gpu1 cooling -> candidates skip it, spark serves
        assert router.candidates_for("research_synthesis") == ["spark"]

    def test_all_cooling_degrades_to_first(self, router):
        router.states["gpu1"].record_failure()
        router.states["spark"].record_failure()
        # degrade, don't crash: full ordered fallback list, first is used
        cands = router.candidates_for("research_synthesis")
        assert cands == ["gpu1", "spark"]
        assert router.pick_endpoint("research_synthesis").name == "gpu1"

    def test_complete_fails_over_and_marks_unhealthy(self, router, monkeypatch):
        calls = {"gpu1": 0, "spark": 0}

        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw):
                key = "gpu1" if ":9901" in url else "spark"
                calls[key] += 1
                if key == "gpu1":
                    raise httpx.ConnectError("refused")
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        result = asyncio.run(router.complete(
            "research_synthesis", [{"role": "user", "content": "x"}]))
        assert result["tier"] == "spark"
        # SPEED run 12 (restored by run 16 after the runs-14 recovery merge
        # dropped it): a CONNECT-phase refusal sends no bytes and is NOT
        # retried in place — one attempt, then fail over.
        assert calls["gpu1"] == 1
        assert router.states["gpu1"].consecutive_failures == 1
        # Next call skips the cooling gpu1 entirely.
        calls["gpu1"] = 0
        result2 = asyncio.run(router.complete(
            "research_synthesis", [{"role": "user", "content": "x"}]))
        assert result2["tier"] == "spark"
        assert calls["gpu1"] == 0  # not retried while cooling

    def test_check_health_marks_state(self, router, monkeypatch):
        class Dead:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx, "AsyncClient", Dead)
        report = asyncio.run(router.health_report())
        assert all(r["status"] == "error" for r in report.values())
        assert all(not router.states[n].available for n in router.endpoints)


class TestCostAccounting:
    def test_local_calls_cost_zero(self, router, monkeypatch):
        class Resp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 500, "completion_tokens": 200}}

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                return Resp()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        asyncio.run(router.complete(
            "research_synthesis", [{"role": "user", "content": "x"}]))
        snap = router.cost_ledger.snapshot()
        assert snap["total_cost_usd"] == 0.0
        assert snap["by_tier"]["gpu1"]["input_tokens"] == 500

    def test_status_shape(self, router):
        s = router.status()
        assert "gpu1" in s["endpoints"]
        ep = s["endpoints"]["gpu1"]
        assert ep["max_concurrency"] == 2
        assert ep["in_flight"] == 0
        assert s["cost"]["budget_usd"] is None


class TestLegacySurfaceIntact:
    def test_tier_for_still_works(self, router):
        assert router.tier_for("screening").base_url == "http://localhost:9902/v1"
        assert router.tier_for("deep_work").name == "gpu1"  # alias + first of list

    def test_ladder_helpers_untouched(self):
        assert callable(inference.escalate_with_ladder)
        assert callable(inference.OllamaInference)
