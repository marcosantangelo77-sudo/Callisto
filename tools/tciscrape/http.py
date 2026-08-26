"""Shared async HTTP client and rate-limited ESPN request helper."""

from typing import Optional

import httpx

ESPN_BASE_TIMEOUT = 15.0

_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=ESPN_BASE_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Callisto/1.0)"},
        )
    return _client


async def _espn_get(url: str) -> dict:
    """Rate-limited ESPN API request."""
    client = await _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def close_client() -> None:
    """Close the shared HTTP client (used by tests / shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
