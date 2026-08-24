"""SPEED run 7 — ox_alpha_proxy: same model over a persistent local process.

The hermes_cli backend forks ONE FRESH INTERPRETER PER COMPLETION (~12s of
process startup measured on this machine). `hermes proxy start --provider
nous` serves the SAME Portal model over OpenAI-compatible HTTP from one
long-lived process (warm calls measured 1.2-2.4s). These tests pin:

1. DECLARATION — providers.yaml declares ox_alpha_proxy, env-overridable,
   honest capabilities (structured_output stays false; the proxy does not
   enforce schemas either).
2. ORDERING — when resolvable it sits ahead of ox_alpha in every task class;
   ox_alpha remains the LAST-resort everywhere.
3. DEGRADATION — with no OX_ALPHA_PROXY_BASE_URL set the endpoint is
   _unresolved and routing is byte-identical to the pre-run7 lists.
4. DISPATCH — ProviderRouter._post reaches an OpenAI-compatible endpoint
   through a monkeypatched transport (no real socket: the no_socket barrier
   exists because live testing 403'd this machine's SEC budget; loopback
   would pass that guard but we keep the suite hermetic anyway).
5. NO GATE MOVED — nothing here touches confidence, caching, cutoffs or the
   adversary's separate call.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402


def _router_with_proxy(monkeypatch, base_url="http://127.0.0.1:1/v1"):
    monkeypatch.setenv("OX_ALPHA_PROXY_BASE_URL", base_url)
    monkeypatch.setenv("OX_ALPHA_PROXY_API_KEY", "test-token")
    return inference.ProviderRouter()


class TestDeclaration:
    def test_declared_when_env_set(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        assert "ox_alpha_proxy" in r.endpoints
        ep = r.endpoints["ox_alpha_proxy"]
        assert ep.backend == "openai_compat"
        assert not ep.extra.get("_unresolved")
        assert ep.base_url == "http://127.0.0.1:1/v1"

    def test_unresolved_without_env(self, monkeypatch):
        for v in ("OX_ALPHA_PROXY_BASE_URL", "OX_ALPHA_PROXY_API_KEY",
                  "OX_ALPHA_PROXY_MODEL"):
            monkeypatch.delenv(v, raising=False)
        r = inference.ProviderRouter()
        ep = r.endpoints["ox_alpha_proxy"]
        assert ep.extra.get("_unresolved"), (
            "without base URL the proxy must be unresolved, not half-configured")
        # ...and routing then behaves exactly like the pre-run7 pool:
        cands = r.candidates_for("research_synthesis")
        assert "ox_alpha_proxy" not in cands
        assert cands[-1] == "ox_alpha"

    def test_honest_capabilities(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        ep = r.endpoints["ox_alpha_proxy"]
        # The PROXY does not enforce json_schema response_format any more than
        # the CLI does — declaring true would be a capability lie.
        assert ep.structured_output is False
        assert ep.tool_calls is False
        assert ep.cost_per_1k_input == 0.0 and ep.cost_per_1k_output == 0.0


TASK_CLASSES = [
    "hypothesis_generation", "research_synthesis", "screening", "extraction",
    "classification", "backtest_interpretation", "promotion_judgment",
    "adversarial_review",
]


class TestOrdering:
    def test_proxy_ahead_of_cli_in_every_class(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        for tc in TASK_CLASSES:
            names = r.task_classes[tc]
            names = names if isinstance(names, list) else [names]
            assert "ox_alpha_proxy" in names, tc
            assert names.index("ox_alpha_proxy") < names.index("ox_alpha"), (
                f"{tc}: proxy must be tried before fresh-fork CLI")

    def test_ox_alpha_still_last_resort(self, monkeypatch):
        r = _router_with_proxy(monkeypatch)
        for tc in TASK_CLASSES:
            names = r.task_classes[tc]
            names = names if isinstance(names, list) else [names]
            assert names[-1] == "ox_alpha", tc

    def test_adversary_review_still_routes(self, monkeypatch):
        """Hard rule: the adversary stays its OWN call — this change only
        re-orders transport, so adversarial_review must still resolve."""
        r = _router_with_proxy(monkeypatch)
        assert r.candidates_for("adversarial_review")


class TestDispatch:
    def test_post_reaches_openai_compat_endpoint(self, monkeypatch):
        """_post builds the right request against an OpenAI-compatible
        endpoint, using a stubbed transport (no real socket)."""

        received: dict = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "{\"ok\": true}"}}],
                        "usage": {}}

        class FakeClient:
            def __init__(self, router):
                self.router = router

            async def post(self, url, *, json=None, headers=None, timeout=None):
                received["url"] = url
                received["payload"] = json
                received["auth"] = headers.get("Authorization")
                return FakeResp()

        r = _router_with_proxy(monkeypatch)
        ep = r.endpoints["ox_alpha_proxy"]

        class FakeClient:
            # exposes is_closed=False so _shared_client treats it as live
            is_closed = False

            async def post(self, url, *, json=None, headers=None, timeout=None):
                received["url"] = url
                received["payload"] = json
                received["auth"] = headers.get("Authorization")
                return FakeResp()

        r._http_client = FakeClient()
        # bind to the loop asyncio.run will create, else _shared_client
        # rebuilds a real httpx client (which would open a socket)
        import asyncio as _aio
        loop_holder: dict = {}

        async def _bind():
            FakeClient._bound_loop = _aio.get_running_loop()
            return await r._post(ep, {"model": ep.model, "messages": []},
                                 timeout=30.0)

        content, usage = _aio.run(_bind())
        assert received["url"] == f"{ep.base_url}/chat/completions"
        assert received["payload"]["model"] == ep.model
        assert received["auth"] == "Bearer test-token"
        assert content == "{\"ok\": true}"

    def test_absent_proxy_fails_over(self, monkeypatch):
        """Proxy down => instant connection error => the endpoint records the
        failure and cools down rather than hanging or retrying forever. The
        chain still ends at ox_alpha (ordering pinned in TestOrdering)."""
        r = _router_with_proxy(monkeypatch, "http://127.0.0.1:9/v1")  # port 9: nothing listens
        for name in ("gpu1", "gpu1_fast", "frontier"):
            if name in r.endpoints:
                for _ in range(4):
                    r.states[name].record_failure()

        import tools.pipeline.hermes_cli as hc

        class FakeCli:
            async def complete(self, messages, *, role="", binary=None,
                               cwd="/tmp", timeout_s=240.0):
                return {"content": "{\"ok\": true}", "rc": 0, "stderr": ""}

        monkeypatch.setattr(hc, "hermes_complete", FakeCli().complete)

        res = asyncio.run(r.complete(
            "research_synthesis",
            [{"role": "user", "content": 'Reply {"ok": true}'}],
            role="research_synthesis"))
        # served by the CLI tier after the proxy failed fast
        assert res["tier"] == "ox_alpha"
        assert r.states["ox_alpha_proxy"].consecutive_failures >= 1
