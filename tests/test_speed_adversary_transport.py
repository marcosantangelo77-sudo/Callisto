"""SPEED run 16 (2026-08-24): schema-bearing calls may ride best-effort
openai_compat endpoints.

Defect this pins: `candidates_for()` excluded every endpoint declaring
structured_output=False from schema-bearing calls UNLESS its backend was
hermes_cli. The adversary is the pipeline's ONLY schema-bearing routed call
(agp/adversary.py passes schema=VERDICT_JSON_SCHEMA), so the critic could
never be served by ox_alpha_proxy — same model, same Portal OAuth as the
exempted CLI, measured warm at 1.2-2.4s vs ~12-14s fork startup — and every
sealed question paid fork cost on one of its three serial rounds. Measured
before this run:

    candidates_for("adversarial_review")             -> ['gpu1', 'ox_alpha_proxy', 'ox_alpha']
    candidates_for("adversarial_review", schema=...) -> ['gpu1', 'ox_alpha']

Policy after run 16: the exemption follows the CAPABILITY CONTRACT, not the
backend brand. An endpoint that honestly declares structured_output=False is
JSON-in-text best-effort exactly like the CLI: no enforcement block is sent
(_payload omits response_format), the shared tolerant parser judges the
reply, battery-D3 fail-closed semantics protect the caller either way.
llama_cpp_server stays excluded when declared False (pinned here and in
test_tier5_serving_pool.py): that declaration means this server lacks the
grammar constraint the backend exists to provide.

Correctness bounds: which MODEL serves is unchanged (proxy and CLI are the
same stealth/ox-alpha); the adversary remains its own separate call; no
caching; nothing crosses a retrodiction cutoff; no gate moves.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402

SCHEMA = {"type": "object",
          "properties": {"objections": {"type": "array"}}}


def _no_proxy_env(monkeypatch) -> None:
    for v in ("OX_ALPHA_PROXY_BASE_URL", "OX_ALPHA_PROXY_API_KEY",
              "OX_ALPHA_PROXY_MODEL"):
        monkeypatch.delenv(v, raising=False)
    # SPEED run 17: isolate health persistence from the real user state dir.
    import tempfile
    monkeypatch.setenv("CALLISTO_STATE_DIR",
                       tempfile.mkdtemp(prefix="adv_transport_state_"))
    monkeypatch.setenv("CALLISTO_ROUTER_HEALTH", "1")


class TestAdmission:
    def test_proxy_admitted_for_schema_bearing_calls(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        cands = r.candidates_for("adversarial_review", schema=SCHEMA)
        assert "ox_alpha_proxy" in cands
        # configured order puts the proxy ahead of the CLI fork
        assert cands.index("ox_alpha_proxy") < cands.index("ox_alpha")

    def test_hermes_cli_still_admitted(self, monkeypatch):
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        cands = r.candidates_for("adversarial_review", schema=SCHEMA)
        assert "ox_alpha" in cands

    def test_true_declared_endpoints_keep_priority(self, monkeypatch):
        """gpu1 declares structured_output=true; admission order is
        unchanged for capable endpoints."""
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        assert r.endpoints["gpu1"].structured_output is True
        cands = r.candidates_for("adversarial_review", schema=SCHEMA)
        assert cands[0] == "gpu1"

    def test_unresolved_env_endpoints_stay_skipped(self, monkeypatch):
        """frontier has no static base_url; without its env it stays out
        of schema-bearing candidates exactly as before."""
        _no_proxy_env(monkeypatch)
        monkeypatch.delenv("FRONTIER_BASE_URL", raising=False)
        r = inference.ProviderRouter()
        assert "frontier" not in r.candidates_for(
            "adversarial_review", schema=SCHEMA)

    def test_llama_false_declared_still_excluded(self, tmp_path):
        """The policy boundary: a llama_cpp_server that declares False lacks
        its grammar constraint and gains nothing from best-effort admission
        that its own operator could not fix server-side. Exclusion stays."""
        cfg = tmp_path / "p.yaml"
        cfg.write_text("""
default_tier: local
providers:
  local:
    backend: llama_cpp_server
    base_url: http://localhost:9901/v1
    model: m
    context_tokens: 4096
    structured_output: false
routing:
  task_classes:
    adversarial_review: local
""")
        r = inference.ProviderRouter(config_path=str(cfg))
        assert r.candidates_for("adversarial_review", schema=SCHEMA) == []


class TestPayloadHonesty:
    def test_no_enforcement_block_for_declared_false(self, monkeypatch):
        """Best-effort endpoints get the CLI's contract: NO response_format
        is attached, so the request cannot imply enforcement that will not
        happen (and stricter OpenAI-compat providers are not 400'd)."""
        _no_proxy_env(monkeypatch)
        r = inference.ProviderRouter()
        ep = r.endpoints["ox_alpha_proxy"]
        assert ep.structured_output is False
        p = r._payload(ep, [{"role": "user", "content": "hi"}], SCHEMA,
                       None, None)
        assert "response_format" not in p
        assert p["model"] == ep.model
        assert p["temperature"] == ep.temperature

    def test_enforcement_block_kept_for_declared_true(self):
        """Byte-compat for capable endpoints (pre-run-16 behaviour)."""
        cfg = Path(__file__).parent.parent / "config" / "providers.yaml"
        r = inference.ProviderRouter(config_path=str(cfg))
        ep = r.endpoints["gpu1"]
        assert ep.structured_output is True
        p = r._payload(ep, [{"role": "user", "content": "hi"}], SCHEMA,
                       None, None)
        rf = p["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == SCHEMA


POOL_CFG = """
default_tier: enforce_ep
providers:
  enforce_ep:
    backend: openai_compat
    base_url: http://127.0.0.1:9901/v1
    model: m-enforce
    context_tokens: 32000
    structured_output: true
  besteffort_ep:
    backend: openai_compat
    base_url: http://127.0.0.1:9902/v1
    model: m-besteffort
    context_tokens: 128000
    structured_output: false
routing:
  task_classes:
    adversarial_review: [enforce_ep, besteffort_ep]
"""


@pytest.fixture
def pool_router(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CALLISTO_ROUTER_HEALTH", "1")
    cfg = tmp_path / "pool.yaml"
    cfg.write_text(POOL_CFG)
    return inference.ProviderRouter(config_path=str(cfg))


class TestFilterConsistency:
    def test_fallback_admits_besteffort_when_all_cooling(self, pool_router):
        """All-cooling-down fallback uses the SAME capability rule; the
        three filters must never drift apart again."""
        for name in ("enforce_ep", "besteffort_ep"):
            for _ in range(4):
                pool_router.states[name].record_failure()
        assert pool_router.states["enforce_ep"].available is False
        cands = pool_router.candidates_for("adversarial_review", schema=SCHEMA)
        # pre-run-16 this filter DROPPED the false-declared endpoint
        # entirely; it must survive with its configured priority
        assert cands == ["enforce_ep", "besteffort_ep"]

    def test_pick_endpoint_aligned_with_candidates(self, pool_router):
        """pick_endpoint used to keep a STRICTER filter than candidates_for
        (it skipped even hermes_cli). One rule now."""
        for _ in range(4):
            pool_router.states["enforce_ep"].record_failure()
        ep = pool_router.pick_endpoint("adversarial_review", schema=SCHEMA)
        assert ep is not None and ep.name == "besteffort_ep"


class _FakeResp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


VERDICT_BODY = {
    "choices": [{"message": {
        "content": '{"objections": []}'}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


class TestEndToEnd:
    def test_schema_bearing_call_reaches_besteffort_secondary(
            self, pool_router, monkeypatch):
        """Integration: enforcing primary dead -> best-effort secondary
        serves the schema-bearing call. Captured request bodies prove the
        honesty rule per hop: enforcement block ONLY on the true-declared
        endpoint."""
        sent: list[tuple[str, dict]] = []

        class FakeClient(httpx.AsyncClient):
            async def post(self, url, **kw):
                sent.append((url, kw.get("json") or {}))
                if ":9901" in url:
                    raise httpx.ConnectError("dead primary", request=None)
                return _FakeResp(VERDICT_BODY)

        monkeypatch.setattr(inference.httpx, "AsyncClient", FakeClient)
        res = asyncio.run(pool_router.complete(
            "adversarial_review",
            [{"role": "user", "content": "attack this"}],
            schema=SCHEMA, timeout=30))
        assert res["tier"] == "besteffort_ep"
        assert res["parsed_json"] == {"objections": []}
        urls = [u for u, _ in sent]
        assert any(":9901" in u for u in urls)
        assert any(":9902" in u for u in urls)
        by_port = {":9901" in u: p for u, p in sent}
        assert "response_format" in by_port[True]    # true-declared hop
        assert "response_format" not in by_port[False]  # best-effort hop
