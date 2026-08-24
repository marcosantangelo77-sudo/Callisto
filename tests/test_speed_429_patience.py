"""SPEED run 9 — 429 patience: honour Portal's Retry-After up to a bounded
ceiling instead of discarding the fast endpoint at the first capacity blip.

Defect pinned here: run 8 capped Retry-After at 10s, so Portal's actual
`Retry-After: 30` capacity 429 DECLINED the in-place retry and failed over.
Measured live (2026-08-23/24): the CLI fork that receives the very same call
survives because its own session waits out the same capacity window (~30s)
and returns a good answer ~29-31s later. The router's HTTP path instead paid
the ~10-14s fresh-fork cost AND got the answer later than a patient proxy
retry would have. Patience is not slower than failover here — it is what the
winning path already does implicitly.

Invariants:
  1. A 429 with Retry-After <= _429_MAX_TOTAL_WAIT_S retries in place
     (run 8 behaviour preserved).
  2. A 429 with Retry-After ABOVE the cap sleeps min(Retry-After,
     _429_PATIENCE_S) and retries the SAME endpoint, bounded by attempts.
  3. Total time spent sleeping on one endpoint never exceeds
     _429_PATIENCE_S + one default backoff — patience is bounded.
  4. Non-429 4xx semantics unchanged; exhaustion still propagates to the
     existing failover chain; no caching; no cutoff interaction; the
     adversary stays its own call. Only WHERE/WHEN the identical completion
     is served changes.
"""
import asyncio
import httpx
import pytest

import inference


class _ErrResp:
    def __init__(self, status: int, retry_after: str = ""):
        self.status_code = status
        self.headers = httpx.Headers(
            {"Retry-After": retry_after} if retry_after else {})


def _status_error(status: int, retry_after: str = ""):
    resp = _ErrResp(status, retry_after)
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def _run(post_fn):
    return asyncio.run(inference._post_with_retry(
        post_fn, object(), {}, timeout=5.0))


def test_portal_style_30s_retry_after_retries_in_place():
    """The live defect: Retry-After: 30 must not discard the fast endpoint."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429, "30")
        return "ok", {}

    content, _ = _run(post_fn)          # would raise pre-run-9
    assert content == "ok"
    assert calls["n"] == 2


def test_patience_is_bounded_even_with_hostile_headers():
    """A huge Retry-After cannot stall the caller beyond the patience cap."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429, "100000")
        return "ok", {}

    import time
    t0 = time.monotonic()
    content, _ = _run(post_fn)
    elapsed = time.monotonic() - t0
    assert content == "ok"
    assert elapsed < inference._429_PATIENCE_S + 5


def test_small_retry_after_still_fast():
    """Run 8's fast path: Retry-After within the old cap behaves identically."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429, "5")
        return "ok", {}

    content, _ = _run(post_fn)
    assert content == "ok"
    assert calls["n"] == 2


def test_exhaustion_still_propagates():
    """All attempts 429 -> propagate; failover chain decides, unchanged."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        raise _status_error(429, "30")

    with pytest.raises(httpx.HTTPStatusError):
        _run(post_fn)
    assert calls["n"] == 2              # bounded by attempts, as before


def test_non_429_unchanged():
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        raise _status_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        _run(post_fn)
    assert calls["n"] == 1
