"""Shared constants and HTTP helpers for the free prop scrapers."""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger("callisto.prop_scraper_free")

try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    CffiSession = None  # type: ignore[assignment]
    _HAS_CURL_CFFI = False

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# Rate limiting (shared across all sources within this package)
_last_dk_request: float = 0.0
_last_fd_request: float = 0.0
_last_mgm_request: float = 0.0
_RATE_LIMIT = 2.0

_cffi_session = None


def get_cffi_session():
    global _cffi_session
    if _cffi_session is None and _HAS_CURL_CFFI:
        _cffi_session = CffiSession(impersonate="chrome131")
    return _cffi_session


async def cffi_get(url: str) -> dict:
    """Rate-limited GET via curl_cffi with Chrome TLS impersonation."""
    global _last_dk_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_dk_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_dk_request = time.monotonic()

    def _do():
        session = get_cffi_session()
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_do)


def parse_nash_american_odds(odds_str: str) -> int:
    """Parse American odds string from Nash (handles Unicode minus)."""
    if not odds_str:
        return 0
    cleaned = odds_str.replace("\u2212", "-").replace("\u2013", "-").replace("+", "")
    try:
        return int(round(float(cleaned)))
    except (ValueError, TypeError):
        return 0


def close_shared_sessions() -> None:
    """Close and reset the shared curl_cffi session, if any."""
    global _cffi_session
    if _cffi_session is not None:
        try:
            _cffi_session.close()
        except Exception:
            pass
        _cffi_session = None
