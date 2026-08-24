"""SPEED (ported 2026-08-24): ProviderRouter shares one pooled AsyncClient.

Defect this pins: _post() and check_health() used to build a fresh
httpx.AsyncClient per request — every model call paid TCP connect + TLS
handshake from scratch (~0.3s measured against a remote TLS host), on top of
inference time, for every call of every run.

Invariants:
  1. Two completions on the same router use the SAME AsyncClient instance
     (connection reuse is possible).
  2. The caller's per-call timeout still overrides the client default.
  3. A client left over from a dead event loop is not reused (asyncio
     transports are loop-bound) — a new loop gets a fresh pool.
"""
import asyncio

import httpx
import pytest

import inference


@pytest.fixture()
def router():
    return inference.ProviderRouter()


class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _patch_post(monkeypatch, log):
    class FakeClient(httpx.AsyncClient):
        async def post(self, url, **kw):
            log.append({"url": url, "timeout": kw.get("timeout")})
            return _FakeResp()

    monkeypatch.setattr(inference.httpx, "AsyncClient", FakeClient)


def test_same_client_reused_across_calls(router, monkeypatch):
    """Two completions in the same event loop must use the SAME client."""
    log = []
    _patch_post(monkeypatch, log)
    ep = next(iter(router.endpoints.values()))

    async def run():
        await router._post(ep, {"model": ep.model}, timeout=5.0)
        first = router._http_client
        await router._post(ep, {"model": ep.model}, timeout=5.0)
        return first

    first = asyncio.run(run())
    assert first is not None
    assert router._http_client is first
    assert len(log) == 2


def test_per_request_timeout_still_applied(router, monkeypatch):
    """The shared pool must not flatten the caller's timeout to the default."""
    log = []
    _patch_post(monkeypatch, log)
    ep = next(iter(router.endpoints.values()))
    asyncio.run(router._post(ep, {"model": ep.model}, timeout=7.25))
    assert log[0]["timeout"] == pytest.approx(7.25)


def test_new_event_loop_gets_fresh_pool(router, monkeypatch):
    """A pooled client bound to a finished loop must never be reused."""
    log = []
    _patch_post(monkeypatch, log)
    ep = next(iter(router.endpoints.values()))

    async def one():
        return await router._post(ep, {"model": ep.model}, timeout=5.0)

    asyncio.run(one())
    first = router._http_client
    asyncio.run(one())
    assert router._http_client is not first


def test_health_probe_uses_shared_pool_too(router, monkeypatch):
    """check_health had the same fresh-client defect; pin it closed."""
    log = []
    _patch_post(monkeypatch, log)
    name = next(iter(router.endpoints))

    async def run():
        await router.check_health(name)
        before = router._http_client
        await router.check_health(name)
        return before

    before = asyncio.run(run())
    assert router._http_client is before
    assert len(log) == 2


def test_aclose_shuts_pool_down(router, monkeypatch):
    log = []
    _patch_post(monkeypatch, log)
    ep = next(iter(router.endpoints.values()))

    async def run():
        await router._post(ep, {"model": ep.model}, timeout=5.0)
        client = router._http_client
        await router.aclose()
        return client.is_closed

    assert asyncio.run(run()) is True
    assert router._http_client is None
