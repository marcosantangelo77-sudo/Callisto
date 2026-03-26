"""
Unified web search for Callisto.

Priority: SearXNG (free, self-hosted) → Brave Search API (paid fallback).
The orchestrator calls web_search() and doesn't need to know which backend is used.
"""

import logging
from typing import Optional

from tools.searxng import searxng_search, searxng_available
from tools.brave_search import brave_search

logger = logging.getLogger("callisto.search")

# Cache SearXNG availability to avoid repeated health checks within a session
_searxng_checked = False
_searxng_ok = False


async def _check_searxng() -> bool:
    """Check SearXNG availability once per process."""
    global _searxng_checked, _searxng_ok
    if not _searxng_checked:
        _searxng_ok = await searxng_available()
        _searxng_checked = True
        if _searxng_ok:
            logger.info("Search backend: SearXNG (self-hosted, free)")
        else:
            logger.info("Search backend: Brave Search API (SearXNG unavailable)")
    return _searxng_ok


async def web_search(
    query: str, count: int = 5, freshness: Optional[str] = None
) -> dict:
    """
    Search the web using the best available backend.

    Tries SearXNG first (free), falls back to Brave Search API.
    Returns standardized results with AGP source_class tagging.

    Args:
        freshness: "pd" (past day), "pw" (past week), "pm" (past month),
                   "py" (past year), or None for no filter.
    """
    if await _check_searxng():
        result = await searxng_search(query, count=count)
        if result.get("results"):
            return result
        # SearXNG returned no results — fall through to Brave
        logger.warning(f"SearXNG returned no results for '{query}', trying Brave")

    return await brave_search(query, count=count, freshness=freshness)


async def close_all_clients() -> None:
    """Close all search backend clients."""
    from tools.searxng import close_client as close_searxng
    from tools.brave_search import close_client as close_brave
    await close_searxng()
    await close_brave()
