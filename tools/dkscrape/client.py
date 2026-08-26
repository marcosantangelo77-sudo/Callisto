"""
Shared HTTP clients, rate limiting, and low-level GET helpers.
"""
import asyncio
import logging
import time
from typing import Optional

import httpx

# curl_cffi is the preferred HTTP client — it impersonates a real browser TLS
# fingerprint and bypasses Akamai/Cloudflare bot detection on the nash endpoint.
# If not installed, we fall back to httpx (which will 403 on old endpoints).
try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

logger = logging.getLogger("callisto.dk_scraper")

# Rate limiting
_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

# Shared clients
_client: Optional[httpx.AsyncClient] = None
_cffi_session: Optional["CffiSession"] = None  # type: ignore[name-defined]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.draftkings.com/",
}


def _get_client() -> httpx.AsyncClient:
    """Legacy httpx client (fallback when curl_cffi unavailable)."""
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


async def _rate_limited_get(url: str) -> httpx.Response:
    """GET with rate limiting via legacy httpx — 1 request per 2 seconds."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    client = _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp


def _cffi_get_sync(url: str) -> dict:
    """Synchronous GET via curl_cffi with Chrome impersonation. Returns parsed JSON."""
    session = _get_cffi_session()
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def _nash_get(url: str) -> dict:
    """
    Async wrapper around the synchronous curl_cffi GET.
    Uses asyncio.to_thread() so the event loop isn't blocked.
    Rate-limited to 1 request per 2 seconds.
    """
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    return await asyncio.to_thread(_cffi_get_sync, url)


def _dk_american_odds(price: float) -> int:
    """Convert DraftKings decimal price to American odds."""
    if price >= 2.0:
        return round((price - 1) * 100)
    elif price > 1.0:
        return round(-100 / (price - 1))
    else:
        return -10000  # Edge case


def _parse_nash_american_odds(odds_str: str) -> int:
    """
    Parse American odds string from the Nash endpoint.

    The Nash API returns displayOdds.american as strings that may use
    the Unicode MINUS SIGN (U+2212, '−') instead of a regular ASCII
    hyphen-minus (U+002D, '-'). Examples: '−112', '+150', '−5.5'.
    """
    if not odds_str:
        return 0
    # Replace Unicode minus (U+2212) and EN DASH (U+2013) with ASCII minus
    cleaned = odds_str.replace("\u2212", "-").replace("\u2013", "-").replace("+", "")
    try:
        return int(round(float(cleaned)))
    except (ValueError, TypeError):
        return 0
