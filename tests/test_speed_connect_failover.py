"""SPEED (ported 2026-08-24): connect-phase failures fail over immediately.

Defect this pins: _post_with_retry retried EVERY httpx.TransportError in
place — sleep 0.5s, attempt again. For ConnectError / ConnectTimeout no bytes
were ever sent, so the second attempt against the same dead socket carries
zero information; it only taxed every dead-hop probe 0.5s (measured 0.528s on
loopback refusals).

Invariants:
  1. A ConnectError / ConnectTimeout propagates after exactly ONE attempt —
     no in-place retry, no backoff sleep.
  2. Read/write-phase transport errors keep today's exact shape: retried in
     place with the 0.5s backoff (transient server-side slowness is real).
  3. 5xx semantics unchanged (timing pin repeated here as a tripwire).
  4. Through ProviderRouter.complete, a dead-first/live-second candidate pair
     serves from the live endpoint and records failure state for the corpse.
"""
import asyncio
import time

import httpx
import pytest

import inference


class _ErrResp:
    def __init__(self, status: int):
        self.status_code = status
        self.headers = httpx.Headers()


def _status_error(status: int):
    return httpx.HTTPStatusError(
        f"{status}", request=httpx.Request("POST", "http://test/v1/x"),
        response=_ErrResp(status))


def _run(post_fn, attempts=2):
    ep = object()
    return asyncio.run(inference._post_with_retry(
        post_fn, ep, {}, timeout=5.0, attempts=attempts))


@pytest.mark.parametrize("exc_factory", [
    lambda: httpx.ConnectError("Connection refused"),
    lambda: httpx.ConnectTimeout("connect timed out"),
])
def test_connect_phase_fails_over_after_one_attempt(exc_factory):
    """No bytes sent → nothing transient to recover → immediate propagation."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        await asyncio.sleep(0.013)      # loopback-refusal scale
        raise exc_factory()

    t0 = time.monotonic()
    with pytest.raises(httpx.TransportError):
        _run(post_fn)
    assert calls["n"] == 1              # ONE attempt, not two
    assert time.monotonic() - t0 < 0.3  # no 0.5s backoff inside this endpoint


def test_read_timeout_still_retried_in_place():
    """Read-phase errors mean a server TOOK the request — the pre-existing
    retry-with-backoff shape is preserved exactly."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("read timed out")
        return "ok", {}

    t0 = time.monotonic()
    content, _ = _run(post_fn)
    assert content == "ok"
    assert calls["n"] == 2
    assert time.monotonic() - t0 >= 0.5   # the usual one backoff


def test_5xx_backoff_timing_unchanged_tripwire():
    """Tripwire: the connect-failover edit must not disturb the 5xx path."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(500)
        return "ok", {}

    t0 = time.monotonic()
    content, _ = _run(post_fn)
    assert content == "ok"
    assert calls["n"] == 2
    assert 0.4 <= time.monotonic() - t0 < 2.0


def test_dead_first_live_second_serves_and_cools(tmp_path, monkeypatch):
    """Integration through ProviderRouter.complete: refused first candidate,
    live second candidate — content served by the live one, corpse's state
    records the failure so the existing cooldown ladder engages."""

    async def fake_post(endpoint, payload, timeout):
        if endpoint.name == "dead":
            await asyncio.sleep(0.013)
            raise httpx.ConnectError(
                f"[Errno 61] Connection refused ({endpoint.base_url})")
        return "live-answer", {"prompt_tokens": 1, "completion_tokens": 1}

    cfg = tmp_path / "providers.yaml"
    cfg.write_text("""
default_tier: pair
providers:
  dead:
    backend: openai_compat
    base_url: http://127.0.0.1:9
    model: m-dead
    context_tokens: 1024
    max_concurrency: 1
  alive:
    backend: openai_compat
    base_url: http://127.0.0.1:1
    model: m-alive
    context_tokens: 1024
    max_concurrency: 1
routing:
  task_classes:
    research_synthesis: [dead, alive]
""")
    router = inference.ProviderRouter(config_path=str(cfg))
    monkeypatch.setattr(router, "_post", fake_post)

    resp = asyncio.run(router.complete("research_synthesis",
                                       [{"role": "user", "content": "q"}]))
    assert resp["content"] == "live-answer"
    assert resp["tier"] == "alive"
    st = router.states["dead"]
    assert st.consecutive_failures == 1
    # corpse is cooling: a second call must skip probing it entirely
    assert not st.available or st.cooldown_until > time.monotonic() - 2

    calls = {"dead": 0, "alive": 0}

    async def counting_post(endpoint, payload, timeout):
        calls[endpoint.name] += 1
        return "live-answer", {}

    monkeypatch.setattr(router, "_post", counting_post)
    resp2 = asyncio.run(router.complete(
        "research_synthesis", [{"role": "user", "content": "q"}]))
    assert resp2["tier"] == "alive"
    assert calls["dead"] == 0           # skipped while cooling, not probed
