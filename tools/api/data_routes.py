"""Data/contextual route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``vector_store``,
``data_collector``) via a late ``from api import ...`` inside the function
body to avoid a circular import at module load time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException


async def get_scoreboard(sport: str):
    """Get live scoreboard from ESPN."""
    from tools.contextual_data import get_scoreboard as _get_scoreboard
    return await _get_scoreboard(sport)


async def get_weather(latitude: float, longitude: float, venue: str = ""):
    """Get weather forecast for a venue."""
    from tools.contextual_data import get_weather as _get_weather
    return await _get_weather(latitude, longitude, venue_name=venue)


def referee_info(refs: str, sport: str = "basketball_nba"):
    """Get referee tendency adjustments. Pass refs as comma-separated names."""
    from tools.contextual_data import get_referee_adjustment
    ref_list = [r.strip() for r in refs.split(",")]
    return get_referee_adjustment(ref_list, sport)


async def embedding_stats(collection: Optional[str] = None):
    """Get embedding store statistics."""
    from api import vector_store
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.get_collection_stats(collection)


async def embedding_search(collection: str, query: str, top_k: int = 10):
    """Search embeddings by text similarity."""
    from api import vector_store
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.search_text(collection, query, top_k)


async def data_collection_stats():
    """Get data collection statistics."""
    from api import data_collector
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    return await data_collector.get_collection_stats()
