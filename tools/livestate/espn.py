"""ESPN HTTP access + per-sport rate-limit backoff ladder.

Split out of ``tools/live_state.py``. All shared mutable state
(semaphore cache, HTTP client, per-sport backoff ladders) lives on the
``tools.live_state`` facade module so monkeypatching / resetting
attributes there keeps working exactly as before the split. Lookups go
through the facade at call time.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("callisto.live_state")


def _fs():
    """Return the facade module (lazy — avoids import cycles)."""
    from tools import live_state as fs

    return fs


def _get_semaphore() -> asyncio.Semaphore:
    fs = _fs()
    if fs._espn_semaphore is None:
        fs._espn_semaphore = asyncio.Semaphore(fs.ESPN_MAX_CONCURRENT)
    return fs._espn_semaphore


def reset_semaphore() -> None:
    """Drop the cached semaphore (used by tests between event loops)."""
    fs = _fs()
    fs._espn_semaphore = None


def _is_backed_off(sport: str) -> bool:
    """True if this sport is currently in its cooldown window."""
    until = _fs()._sport_backoff_until.get(sport, 0.0)
    return until > time.time()


def _apply_backoff(sport: str) -> float:
    """Escalate backoff for ``sport`` after a hard rate-limit. Returns
    the new cooldown length in seconds. Caps at the last ladder step.
    """
    fs = _fs()
    cur = fs._sport_backoff_step.get(sport, 0.0)
    # Find next step strictly greater than current; if none, hold at cap.
    next_step = fs.BACKOFF_STEPS_S[-1]
    for step in fs.BACKOFF_STEPS_S:
        if step > cur:
            next_step = step
            break
    fs._sport_backoff_step[sport] = next_step
    fs._sport_backoff_until[sport] = time.time() + next_step
    logger.warning(f"ESPN backoff for {sport} -> {next_step:.0f}s")
    return next_step


def _clear_backoff(sport: str) -> None:
    """Reset the backoff ladder for ``sport`` after a clean round."""
    fs = _fs()
    if sport in fs._sport_backoff_step:
        fs._sport_backoff_step.pop(sport, None)
        fs._sport_backoff_until.pop(sport, None)


async def _get_client() -> httpx.AsyncClient:
    fs = _fs()
    client = fs._client
    if client is not None and not client.is_closed:
        return client
    fs._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return fs._client


async def close_client() -> None:
    fs = _fs()
    client = fs._client
    if client is not None and not client.is_closed:
        await client.aclose()
        fs._client = None


def _is_active(event: dict) -> bool:
    """True iff the event is currently in-progress (not pre-game / final)."""
    status = (event.get("status") or {}).get("type") or {}
    state = (status.get("state") or "").lower()
    # ESPN state values: 'pre' (not started), 'in' (live), 'post' (final).
    return state == "in"


class _RateLimited(Exception):
    """Raised internally so the caller can escalate backoff for a sport."""


async def _list_active_events(sport_key: str) -> list[dict]:
    """Return the set of ESPN event dicts currently in-progress for a sport.

    Raises ``_RateLimited`` on HTTP 403/429 so the caller can apply per-sport
    exponential backoff. Other errors log and return ``[]`` (treat as empty
    round; do NOT back off — might be a transient DNS / connection blip).
    """
    fs = _fs()
    espn = fs.LIVE_SPORTS.get(sport_key)
    if not espn:
        return []
    category, league = espn
    url = f"{fs.ESPN_BASE}/{category}/{league}/scoreboard"
    client = await _get_client()
    sem = _get_semaphore()
    try:
        async with sem:
            resp = await client.get(url)
        if resp.status_code in (403, 429):
            raise _RateLimited(f"HTTP {resp.status_code} on scoreboard")
        resp.raise_for_status()
        data = resp.json()
    except _RateLimited:
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (403, 429):
            raise _RateLimited(str(e))
        logger.warning(f"ESPN scoreboard fetch failed for {sport_key}: {e}")
        return []
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch failed for {sport_key}: {e}")
        return []
    return [e for e in (data.get("events") or []) if _is_active(e)]


async def _fetch_event_summary(sport_key: str, event_id: str):
    """Return ESPN summary payload for a single event or None on failure.

    Raises ``_RateLimited`` on 403/429 — the sport-level caller decides how
    to fold it into the backoff ladder. Non-rate-limit failures are logged
    and return None (detector just sees no new state this tick).
    """
    fs = _fs()
    espn = fs.LIVE_SPORTS.get(sport_key)
    if not espn:
        return None
    category, league = espn
    url = f"{fs.ESPN_BASE}/{category}/{league}/summary"
    client = await _get_client()
    sem = _get_semaphore()
    try:
        async with sem:
            resp = await client.get(url, params={"event": event_id})
        if resp.status_code in (403, 429):
            raise _RateLimited(f"HTTP {resp.status_code} on summary")
        resp.raise_for_status()
        return resp.json()
    except _RateLimited:
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (403, 429):
            raise _RateLimited(str(e))
        logger.debug(f"ESPN summary fetch failed for {sport_key}/{event_id}: {e}")
        return None
    except Exception as e:
        logger.debug(f"ESPN summary fetch failed for {sport_key}/{event_id}: {e}")
        return None
