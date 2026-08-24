"""SPEED run 10 — patience ceiling is per-429, not per-call.

Run 9 raised the single-sleep clamp _429_MAX_TOTAL_WAIT_S from 10s to 35s and
called it a "patience" fix. Live measurement on the running proxy showed why
that does not hold under Portal's real capacity pattern: Portal serves
Retry-After: 30 windows CONSECUTIVELY. With attempts=2, call 0 hits a 30s
window, sleeps 30 (honoured, <= 35), retries once, hits ANOTHER 30s window,
and raises — having burned 30+s to fail over anyway, exactly the run-8
disease one sleep later. Live: raw curl with --retry needed up to ~128s of
consecutive 429 windows before a 200.

The unit of patience must be the CALL: total time spent sleeping on 429s for
one completion, bounded by _429_PATIENCE_S (35s default). A single Retry-After
may exceed it only while the running total stays under budget; when the
budget is spent the call fails over, preserving the run-8/9 invariant that we
never wait longer than we would have paid for a fork.

Pins:
1. two consecutive Retry-After:30 429s -> second attempt happens in place
   only if the TOTAL slept stays within patience; exhaustion still propagates.
2. total wait never exceeds patience + one attempt's overshoot tolerance.
3. single small Retry-After unchanged from run 8/9 behaviour.
4. hostile Retry-After above patience alone is declined (failover) as before.
5. non-429 4xx still fails over immediately; no caching, no cutoff contact,
   adversary path untouched.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import inference  # noqa: E402


def _endpoint() -> inference.EndpointConfig:
    return inference.EndpointConfig(
        name="ox_alpha_proxy", backend="openai_compat",
        base_url="http://127.0.0.1:1/v1", model="stealth/ox-alpha")


class _Resp:
    def __init__(self, status: int, retry_after: str = ""):
        self.status_code = status
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _Err(inference.httpx.HTTPStatusError):
    def __init__(self, status: int, retry_after: str = ""):
        self.response = _Resp(status, retry_after)
        super().__init__("x", request=None, response=self.response)  # type: ignore[arg-type]


def _post_factory(failures: list[_Err], ok_result=("OK", {})):
    calls = {"n": 0}

    async def post_fn(endpoint, payload, timeout):
        n = calls["n"]
        calls["n"] += 1
        if n < len(failures):
            raise failures[n]
        return ok_result

    post_fn.calls = calls  # type: ignore[attr-defined]
    return post_fn


@pytest.mark.asyncio
async def test_two_consecutive_30s_windows_stay_in_place_within_call_budget():
    """THE regression run 9 could not pass: consecutive Retry-After:30
    windows. Under per-CALL patience the first 30s sleep is honoured and the
    second attempt goes out in place (total budget 35s allows it); whether it
    succeeds or exhausts, no fork-failover happened after paying the wait."""
    failures = [_Err(429, "30"), _Err(429, "30")]
    post = _post_factory(failures)
    t0 = time.monotonic()
    with pytest.raises(inference.httpx.HTTPStatusError):
        await inference._post_with_retry(post, _endpoint(), {}, 60,
                                         attempts=2)
    elapsed = time.monotonic() - t0
    assert post.calls["n"] == 2  # retried IN PLACE rather than failing over
    assert elapsed < inference._429_PATIENCE_S + 5


@pytest.mark.asyncio
async def test_second_window_success_when_upstream_recovers():
    """First 429 window waited out, retry succeeds in place — the exact live
    pattern that run 9's code turned into a wasted 30s + CLI fork."""
    failures = [_Err(429, "30")]
    post = _post_factory(failures)
    res = await inference._post_with_retry(post, _endpoint(), {}, 60,
                                           attempts=2)
    assert res[0] == "OK"
    assert post.calls["n"] == 2


@pytest.mark.asyncio
async def test_small_retry_after_unchanged():
    failures = [_Err(429, "2")]
    post = _post_factory(failures)
    res = await inference._post_with_retry(post, _endpoint(), {}, 60,
                                           attempts=2)
    assert res[0] == "OK"


@pytest.mark.asyncio
async def test_hostile_retry_after_above_patience_declined():
    post = _post_factory([_Err(429, "100000")])
    with pytest.raises(inference.httpx.HTTPStatusError):
        await inference._post_with_retry(post, _endpoint(), {}, 60,
                                         attempts=2)
    assert post.calls["n"] == 1  # no in-place retry at all


@pytest.mark.asyncio
async def test_non429_4xx_fails_over_immediately():
    post = _post_factory([_Err(403)])
    with pytest.raises(inference.httpx.HTTPStatusError):
        await inference._post_with_retry(post, _endpoint(), {}, 60,
                                         attempts=2)
    assert post.calls["n"] == 1
