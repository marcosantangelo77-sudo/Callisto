"""
Brave Search API tool for Callisto.

Provides web search capability with AGP source class tagging.
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def brave_search(
    query: str, count: int = 5, freshness: Optional[str] = None
) -> dict:
    """
    Search the web via Brave Search API.

    Args:
        query: Search query string.
        count: Number of results (max 20).
        freshness: Time filter — "pd" (past day), "pw" (past week),
                   "pm" (past month), "py" (past year), or None.

    Returns:
        Dict with "results" list, each tagged with source_class: SECONDARY.
    """
    if not BRAVE_API_KEY or BRAVE_API_KEY == "your-brave-api-key-here":
        return {"error": "BRAVE_API_KEY not configured", "results": []}

    params = {"q": query, "count": min(count, 20)}
    if freshness:
        params["freshness"] = freshness

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(BRAVE_SEARCH_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    web_results = data.get("web", {}).get("results", [])
    results = []
    for item in web_results:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "age": item.get("age", ""),
            "source_class": "SECONDARY",
        })

    return {"query": query, "result_count": len(results), "results": results}


def brave_search_sync(query: str, count: int = 5) -> dict:
    """Synchronous wrapper for Hermes-compatible tool use."""
    import asyncio
    return asyncio.run(brave_search(query, count))
