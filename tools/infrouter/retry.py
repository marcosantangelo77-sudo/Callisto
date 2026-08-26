"""In-place retry for one ProviderRouter endpoint (transient 5xx / 429).

SPEED run 8 (2026-08-23): upstream 429 (rate/capacity) retries in place.
Measured live: the ox_alpha proxy serves the SAME model as every later
failover tier, but a Portal-capacity 429 is transient — failing over on it
discarded the ~10x persistent-proxy win and landed every such call on the
~12-20s fresh-fork CLI path. Retry-in-place changes only WHERE the identical
completion is served; non-429 4xx still fail over immediately and exhaustion
still propagates to the existing failover chain. A Retry-After header is
honoured, capped at _429_MAX_TOTAL_WAIT_S so a hostile/lazy server cannot
stall a call.
"""

from __future__ import annotations

from typing import Optional

import asyncio
import httpx

from tools.infrouter.config import EndpointConfig

# A 429 with no Retry-After waits this long before the next in-place attempt.
_429_DEFAULT_BACKOFF_S = 1.0
# Never sleep longer than this on a Retry-After; a server demanding more
# backoff than we may spend fails over instead of stalling the caller.
_429_MAX_TOTAL_WAIT_S = 10.0


def _retry_after_seconds(response: httpx.Response) -> float:
    """Retry-After from a 429 response, in seconds, capped.

    Accepts delta-seconds (and ignores HTTP-date form — treat as default
    backoff rather than parsing dates). Missing/garbled header -> default.
    """
    raw = ""
    try:
        raw = response.headers.get("Retry-After") or ""
    except Exception:
        return _429_DEFAULT_BACKOFF_S
    try:
        val = float(raw.strip())
    except (ValueError, AttributeError):
        return _429_DEFAULT_BACKOFF_S
    if val < 0:
        return _429_DEFAULT_BACKOFF_S
    return min(val, _429_MAX_TOTAL_WAIT_S)


async def _post_with_retry(post_fn, endpoint: EndpointConfig, payload: dict,
                           timeout: float, attempts: int = 2) -> tuple[str, dict]:
    """Retry transient failures within one endpoint before failing over.
    Connection errors and 5xx retry; other HTTP errors do not.
    """
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        slept = False
        try:
            return await post_fn(endpoint, payload, timeout)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status < 500 and status != 429:
                raise
            last_exc = e
            if status == 429:
                retry_after = _retry_after_seconds(e.response)
                if retry_after > _429_MAX_TOTAL_WAIT_S:
                    raise  # server says: back off longer than we may wait
                await asyncio.sleep(retry_after)
                slept = True
        except (httpx.TransportError,) as e:
            last_exc = e
        if i < attempts - 1 and not slept:
            await asyncio.sleep(0.5 * (i + 1))
    assert last_exc is not None
    raise last_exc
