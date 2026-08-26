"""Ollama embedding HTTP client.

Extracted from tools/embeddings.py.
"""

import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text:latest")

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def embed_text(text: str) -> list[float]:
    """Get embedding vector for a single text string via Ollama."""
    client = _get_client()
    resp = await client.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings", [])
    if not embeddings:
        raise ValueError(f"No embedding returned for text: {text[:50]}...")
    return embeddings[0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed multiple texts in batches."""
    all_embeddings = []
    client = _get_client()
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = await client.post(
            f"{OLLAMA_BASE}/api/embed",
            json={"model": EMBED_MODEL, "input": batch},
        )
        resp.raise_for_status()
        data = resp.json()
        all_embeddings.extend(data.get("embeddings", []))
    return all_embeddings
