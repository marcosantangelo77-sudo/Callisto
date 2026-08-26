"""HTTP client plumbing for the Action Network scraper."""

import asyncio
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("callisto.actionnet.http")

# curl_cffi is the preferred HTTP client — it impersonates a real browser TLS
# fingerprint and bypasses Cloudflare/Akamai bot detection.
# If not installed, we fall back to httpx.
try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

_last_request_time: float = 0.0
RATE_LIMIT_SECONDS = 2.0

_client: Optional[httpx.AsyncClient] = None
_cffi_session: Optional["CffiSession"] = None  # type: ignore[name-defined]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.actionnetwork.com/",
}


def _get_client() -> httpx.AsyncClient:
    """Get or create an httpx async client (fallback when curl_cffi unavailable)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5)
    return _client


def _get_cffi_session() -> "CffiSession":  # type: ignore[name-defined]
    """Get or create a curl_cffi session with Chrome TLS impersonation."""
    global _cffi_session
    if _cffi_session is None:
        _cffi_session = CffiSession(impersonate="chrome131")
    return _cffi_session


async def close_client() -> None:
    """Close HTTP clients and free resources."""
    global _client, _cffi_session
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
    if _cffi_session is not None:
        try:
            _cffi_session.close()
        except Exception as e:
            logger.info(f"cffi session close error (non-critical): {e}")
        _cffi_session = None


def _cffi_get_sync(url: str) -> dict:
    """Synchronous GET via curl_cffi with Chrome impersonation. Returns parsed JSON."""
    session = _get_cffi_session()
    resp = session.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def rate_limited_get(url: str) -> dict:
    """
    Async GET with rate limiting. Prefers curl_cffi, falls back to httpx.
    Returns parsed JSON dict.
    """
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        await asyncio.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    if _HAS_CURL_CFFI:
        return await asyncio.to_thread(_cffi_get_sync, url)
    else:
        client = _get_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()
