"""HTTP layer for odds-api.io — authenticated GET with 429 backoff.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
"""

import asyncio
import logging
import random
from typing import Optional

import httpx

from tools.odds_io.config import (
    ODDS_API_IO_BASE,
    ODDS_API_IO_KEY,
    get_client,
)
from tools.odds_io.usage import check_budget, increment_usage

logger = logging.getLogger("callisto.odds_api_io")

# Backoff config — 429 handling. Prior behavior: single call, return
# {"error": "rate limit"} on first 429. Problem: the next five retries in the
# scheduler all tripped the same 429 one second apart and produced 429 storms
# against odds-api.io that counted toward the hourly quota. With exponential
# backoff + Retry-After honoring, a 429 costs one sleep and (typically) one
# retry instead of six wasted calls.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 16.0
BACKOFF_MAX_RETRIES = 3


def compute_backoff(attempt: int, retry_after: Optional[str]) -> float:
    """Return sleep duration in seconds for this retry attempt."""
    if retry_after:
        # Retry-After is either an integer-seconds value or an HTTP date.
        try:
            return min(BACKOFF_MAX_SECONDS, float(retry_after))
        except (TypeError, ValueError):
            pass
    # Exponential with jitter: 1, 2, 4, 8, 16 (cap)
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))
    return base + random.uniform(0, min(1.0, base * 0.1))


async def api_get(endpoint: str, params: Optional[dict] = None) -> dict | list:
    """
    Make an authenticated GET request to Odds-API.io with 429 backoff.

    429 responses trigger exponential backoff (honoring Retry-After) up to
    BACKOFF_MAX_RETRIES attempts. After exhausting retries the call returns
    an {"error": "rate limit ..."} sentinel — the @tracked_ingestion
    decorator recognizes this pattern and logs status='rate_limited' so the
    health check can differentiate quota exhaustion from real failures.

    Returns parsed JSON (dict or list) on success.
    Returns {"error": "..."} on failure.
    """
    budget_err = check_budget()
    if budget_err:
        return {"error": budget_err}

    params = params or {}
    params["apiKey"] = ODDS_API_IO_KEY
    client = get_client()

    url = f"{ODDS_API_IO_BASE}{endpoint}"

    for attempt in range(BACKOFF_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            increment_usage()
            return resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass

            if status == 429 and attempt < BACKOFF_MAX_RETRIES:
                retry_after = e.response.headers.get("Retry-After")
                sleep_s = compute_backoff(attempt, retry_after)
                logger.warning(
                    f"Odds-API.io 429 on {endpoint} (attempt {attempt + 1}/"
                    f"{BACKOFF_MAX_RETRIES + 1}); sleeping {sleep_s:.2f}s"
                )
                await asyncio.sleep(sleep_s)
                continue

            # Non-retryable or retries exhausted
            logger.error(f"Odds-API.io HTTP {status} on {endpoint}: {body}")
            if status == 401:
                return {"error": "Invalid ODDS_API_IO_KEY — check your API key"}
            if status == 429:
                # Retries exhausted — return rate_limited sentinel so
                # @tracked_ingestion tags the run correctly.
                return {"error": f"rate limit: exhausted {BACKOFF_MAX_RETRIES} retries on {endpoint}"}
            if status == 403:
                return {"error": f"Access denied (bookmaker limit?): {body}"}
            return {"error": f"HTTP {status}: {body or 'Unknown error'}"}
        except httpx.TimeoutException:
            logger.error(f"Odds-API.io timeout on {endpoint}")
            return {"error": "Request timeout — odds-api.io did not respond in 20s"}
        except Exception as e:
            logger.error(f"Odds-API.io error on {endpoint}: {e}")
            return {"error": str(e)}

    # Should be unreachable — loop always returns or retries
    return {"error": "rate limit: backoff loop exited unexpectedly"}
