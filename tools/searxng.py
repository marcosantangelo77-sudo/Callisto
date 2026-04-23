"""
SearXNG search tool for Callisto.

Self-hosted metasearch — zero cost, no API key, no query limits.
Aggregates results from Google, Bing, DuckDuckGo, and others.
Falls back to Brave Search API if SearXNG is unavailable.
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

# SearXNG instance URL — default is local Docker instance
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")

# Shared client for connection reuse
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def close_client() -> None:
    """Close the shared httpx client."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def searxng_search(
    query: str, count: int = 5, categories: str = "general"
) -> dict:
    """
    Search the web via a local SearXNG instance.

    Args:
        query: Search query string.
        count: Number of results to return.
        categories: Search categories — "general", "science", "news", "it", etc.

    Returns:
        Dict with "results" list, each tagged with source_class: SECONDARY.
    """
    params = {
        "q": query,
        "format": "json",
        "categories": categories,
        "pageno": 1,
    }

    client = _get_client()
    try:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.ConnectError, httpx.HTTPStatusError, httpx.TimeoutException) as e:
        return {"error": f"SearXNG unavailable: {e}", "results": []}

    raw_results = data.get("results", [])
    results = []
    seen_urls = set()
    for item in raw_results:
        url = item.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "description": item.get("content", ""),
            "engine": item.get("engine", ""),
            "source_class": "SECONDARY",
        })
        if len(results) >= count:
            break

    return {"query": query, "result_count": len(results), "results": results}


async def searxng_available() -> bool:
    """Check if the SearXNG instance is reachable."""
    client = _get_client()
    try:
        resp = await client.get(f"{SEARXNG_URL}/healthz", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False
