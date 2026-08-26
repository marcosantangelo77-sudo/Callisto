"""Shared HTTP pool, payload builder, and health probe for ProviderRouter.

Extracted from ``inference_router.ProviderRouter`` so ``complete()`` and
``candidates_for()`` stay on the facade (CALLISTO_LOCAL_ONLY strip +
``hermes_cli`` last-resort are AST-pinned there). Completions stay HTTP.
Do not point MODEL_LADDER at ProviderRouter. Do not import
``tools.autonomous``. Hermes is the agent runtime, not a kernel transport.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncio as _asyncio
import httpx

from inference_kernel import logger
from tools.infrouter.config import EndpointConfig


def shared_client(router) -> httpx.AsyncClient:
    """Process/router-wide pooled AsyncClient. Rebuilt if the running
    event loop changed (asyncio transports are loop-bound). A client that
    does not expose ``is_closed`` (test doubles) is treated as spent, so
    opaque stand-ins keep the legacy fresh-client-per-call shape."""
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    current = router._http_client
    spent = (current is None
             or bool(getattr(current, "is_closed", True))
             or getattr(current, "_bound_loop", None) is not loop)
    if spent:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=8,
                keepalive_expiry=120.0,
            ),
        )
        client._bound_loop = loop  # type: ignore[attr-defined]
        router._http_client = client
    return router._http_client


def reset_shared_client(router) -> None:
    router._http_client = None


async def aclose_client(router) -> None:
    """Close the shared pool (graceful shutdown / tests)."""
    client = router._http_client
    router._http_client = None
    if client is not None and not getattr(client, "is_closed", True):
        await client.aclose()


def build_payload(
    endpoint: EndpointConfig,
    messages: list[dict],
    schema: Optional[dict],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> dict:
    payload: dict[str, Any] = {
        "model": endpoint.model,
        "messages": messages,
        "temperature": (
            temperature if temperature is not None else endpoint.temperature
        ),
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if schema is not None:
        # Structured output. llama-server supports json_schema in
        # response_format; hosted OpenAI-compat APIs accept it too.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "callisto_output", "schema": schema},
        }
    return payload


async def post(router, endpoint: EndpointConfig, payload: dict,
               timeout: float) -> tuple[str, dict]:
    headers = {"Content-Type": "application/json"}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    # SPEED run 2026-08-23: one shared AsyncClient (connection pool) instead
    # of a fresh client per request. A fresh client pays TCP connect + TLS
    # handshake every call — measured ~0.3s extra per call against a remote
    # TLS host, on top of inference time, for every completion and health
    # probe. The shared client reuses pooled keep-alive connections.
    # Per-request timeout still overrides the client default; failover
    # semantics are unchanged (errors propagate to _post_with_retry).
    client = router._shared_client()
    resp = await client.post(
        f"{endpoint.base_url}/chat/completions", json=payload,
        headers=headers, timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        logger.warning(
            f"ProviderRouter: malformed completion response from "
            f"endpoint {endpoint.name}: keys={list(data)}"
        )
        content = ""
    usage = data.get("usage") or {}
    return content, usage


async def check_health(router, name: str, timeout: float = 5.0) -> dict:
    """Probe one endpoint with a minimal chat request."""
    ep = router.endpoints[name]
    if ep.backend == "hermes_cli":
        # No HTTP to probe: healthy iff the binary resolves. A real ping
        # would burn a ~14s CLI session per health pass.
        from tools.pipeline.hermes_cli import hermes_available
        if hermes_available():
            router.states[name].record_success()
            return {"endpoint": name, "status": "ok"}
        router.states[name].record_failure()
        return {"endpoint": name, "status": "error",
                "error": "hermes CLI binary not found"}
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    payload = {
        "model": ep.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        client = router._shared_client()
        resp = await client.post(
            f"{ep.base_url}/chat/completions", json=payload,
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        router.states[name].record_success()
        return {"endpoint": name, "status": "ok"}
    except Exception as e:
        router.states[name].record_failure()
        return {"endpoint": name, "status": "error", "error": str(e)}


async def health_report(router) -> dict:
    results = await _asyncio.gather(
        *(router.check_health(n) for n in router.endpoints),
        return_exceptions=True,
    )
    out = {}
    for name, r in zip(router.endpoints, results):
        out[name] = r if isinstance(r, dict) else {
            "endpoint": name, "status": "error", "error": repr(r)}
    return out
