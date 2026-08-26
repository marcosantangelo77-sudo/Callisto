"""Shared httpx async client for the news ingestion package.

One pooled client per process — httpx pools per-host so a single instance
is fine for all providers.
"""
from __future__ import annotations

from typing import Optional

import httpx

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                # A real UA avoids RotoWire's bot interstitial.
                "User-Agent": (
                    "Mozilla/5.0 (compatible; Callisto/1.0; "
                    "+https://github.com/callisto-bot)"
                ),
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def facade_get_client() -> httpx.AsyncClient:
    """Resolve the HTTP client via the ``tools.news_ingestion`` facade when
    it is loaded, so legacy monkeypatching of ``ni._get_client`` keeps
    working; otherwise fall back to the shared pooled client here."""
    import sys

    mod = sys.modules.get("tools.news_ingestion")
    if mod is not None:
        fn = getattr(mod, "_get_client", None)
        if fn is not None:
            return fn()
    return get_client()
