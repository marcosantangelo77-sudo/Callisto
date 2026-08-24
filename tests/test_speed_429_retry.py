"""SPEED run 8 (2026-08-23): upstream 429 retries in place before failover.

Defect this pins: _post_with_retry treated every <500 status as fatal for the
endpoint. Under Portal capacity pressure the ox_alpha proxy (the ~10x faster
persistent-process path, speed run 7) returns transient 429s — measured 4 of
9 calls on one night — and each one threw the call onto the ~12-20s fresh-fork
CLI failover path anyway, discarding the run-7 win exactly when capacity is
tightest.

Invariants:
  1. A 429 is retried against the SAME endpoint; a later success is returned
     without any failover.
  2. Non-429 4xx still raise immediately (no in-place retry).
  3. Exhausted 429 attempts propagate the error to the caller — the existing
     failover chain in ProviderRouter.complete is untouched.
  4. Retry-After delta-seconds is honoured but capped; garbage/missing header
     falls back to a small default.
"""
import asyncio
import time

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
    err = httpx.HTTPStatusError(f"{status}", request=req, response=resp)
    return err


def _run(post_fn, attempts=2):
    ep = object()
    return asyncio.run(inference._post_with_retry(
        post_fn, ep, {}, timeout=5.0, attempts=attempts))


def test_429_retried_in_place_then_success():
    """A 429 followed by success never leaves this endpoint."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429)
        return "ok", {}

    t0 = time.monotonic()
    content, usage = _run(post_fn)
    assert content == "ok"
    assert calls["n"] == 2                      # retried same endpoint
    assert time.monotonic() - t0 >= inference._429_DEFAULT_BACKOFF_S


def test_non_429_4xx_raises_immediately():
    """401/404 etc. must not be retried in place — unchanged semantics."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        raise _status_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        _run(post_fn)
    assert calls["n"] == 1


def test_429_exhaustion_propagates_to_failover():
    """All attempts 429 -> the exception propagates (failover chain decides)."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        raise _status_error(429)

    with pytest.raises(httpx.HTTPStatusError):
        _run(post_fn)
    assert calls["n"] == 2                      # bounded, not infinite


def test_retry_after_capped_and_defaulted():
    assert inference._retry_after_seconds(
        _ErrResp(429, "120")) == inference._429_MAX_TOTAL_WAIT_S
    assert inference._retry_after_seconds(
        _ErrResp(429)) == inference._429_DEFAULT_BACKOFF_S
    assert inference._retry_after_seconds(
        _ErrResp(429, "soon")) == inference._429_DEFAULT_BACKOFF_S


def test_5xx_backoff_timing_unchanged():
    """The pre-existing 5xx path keeps its 0.5s backoff and 2-attempt shape."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(500)
        return "ok", {}

    t0 = time.monotonic()
    content, _ = _run(post_fn)
    assert content == "ok"
    elapsed = time.monotonic() - t0
    assert calls["n"] == 2
    assert 0.4 <= elapsed < 2.0                 # one ~0.5s backoff, no more


def test_429_honours_small_retry_after():
    """Retry-After: 0 means an immediate second attempt (Portal-style)."""
    calls = {"n": 0}

    async def post_fn(ep, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(429, "0")
        return "ok", {}

    t0 = time.monotonic()
    content, _ = _run(post_fn)
    assert content == "ok"
    assert time.monotonic() - t0 < 0.4          # no default backoff applied
