"""Wiki endpoint handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.
"""

from __future__ import annotations

import aiosqlite
from fastapi import HTTPException
from typing import Optional


async def wiki_stats():
    """Get wiki compilation statistics."""
    from tools.knowledge_wiki import get_wiki

    from api import memory

    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        return await wiki.get_stats(db)


async def wiki_articles(domain: Optional[str] = None, limit: int = 50):
    """List wiki articles, optionally filtered by domain."""
    from tools.knowledge_wiki import get_wiki

    from api import memory

    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        articles = await wiki.list_articles(db, domain=domain, limit=limit)
        return {"count": len(articles), "articles": articles}


async def wiki_article(topic: str):
    """Get a specific wiki article by topic slug."""
    from tools.knowledge_wiki import get_wiki

    from api import memory

    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        article = await wiki.get_article(db, topic)
        if not article:
            raise HTTPException(status_code=404, detail=f"Article '{topic}' not found")
        return article


async def wiki_search(q: str, limit: int = 10):
    """Search wiki articles by keyword."""
    from tools.knowledge_wiki import get_wiki

    from api import memory

    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        results = await wiki.search(db, q, limit=limit)
        return {"query": q, "count": len(results), "results": results}


async def wiki_contradictions(unresolved_only: bool = True):
    """Get wiki contradiction findings."""
    from tools.knowledge_wiki import get_wiki

    from api import memory

    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        items = await wiki.get_contradictions(db, unresolved_only=unresolved_only)
        return {"count": len(items), "contradictions": items}
