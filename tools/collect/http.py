"""
Shared HTTP client singleton for all collectors.

SECURITY (audit H-10): initialization is serialized by an asyncio.Lock so
concurrent collect_* calls cannot leak clients / exhaust local sockets.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx

_client: Optional[httpx.AsyncClient] = None
_client_lock: Optional[asyncio.Lock] = None


def _get_client_lock() -> asyncio.Lock:
    """Lazy-init the asyncio.Lock so we don't bind to a non-existent loop at import."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client() -> httpx.AsyncClient:
    """Get-or-create the shared httpx client.

    SECURITY (audit H-10): the previous synchronous double-check could race when
    two concurrent collect_* calls hit the singleton at the same time, leaking
    a client and (under sustained load) exhausting local sockets. The init is
    now serialized by an asyncio.Lock and the function is async; callers must
    `await` it. The lock-acquisition cost is one TLS-cheap op compared to the
    network call that follows, so contention is irrelevant.
    """
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    lock = _get_client_lock()
    async with lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, max_redirects=5)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
