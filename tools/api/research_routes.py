"""Research-loop route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``research_loop``,
``data_collector``, ``hypothesis_generator``, ``logger``) via a late
``from api import ...`` inside the function body to avoid a circular
import at module load time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException


async def research_status():
    """Get research loop status."""
    from api import research_loop
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return research_loop.get_status()


async def research_pause():
    """Pause the research loop."""
    from api import research_loop
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return await research_loop.pause()


async def research_resume():
    """Resume the research loop."""
    from api import research_loop
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return await research_loop.resume()


async def research_local_only(enabled: bool = True):
    """Toggle local-only mode (no Claude Code calls)."""
    from api import research_loop
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return research_loop.set_local_only(enabled)


async def research_collect(sport: str = "basketball_nba", date: Optional[str] = None):
    """Manually trigger data collection for a sport."""
    from api import data_collector
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    scores = await data_collector.collect_scores(sport, date)
    box = await data_collector.collect_box_scores(sport, date)
    return {"scores": scores, "box_scores": box}


async def research_generate(sport: str = "basketball_nba", max_hypotheses: int = 20):
    """Manually trigger hypothesis generation."""
    from api import hypothesis_generator
    if not hypothesis_generator:
        raise HTTPException(status_code=503, detail="Hypothesis generator not initialized")
    created = await hypothesis_generator.generate_from_templates(
        sport=sport, max_hypotheses=max_hypotheses,
    )
    return {"generated": len(created), "hypotheses": created}


def _compile_patterns(patterns):
    """Compile regex patterns for batch-reject. Raises 400 on empty list."""
    if not patterns:
        raise HTTPException(status_code=400, detail="patterns list required")
    import re
    return [re.compile(p, re.IGNORECASE) for p in patterns]


async def batch_reject_hypotheses(request_body: dict):
    """Batch-reject draft hypotheses matching regex patterns.

    Body: {"patterns": ["rest|b2b", "weather"], "dry_run": true}
    Only operates on status='draft'. Returns count and sample of affected.
    """
    import logging
    from tools.schema import open_db

    logger = logging.getLogger("callisto.api")

    patterns = request_body.get("patterns", [])
    dry_run = request_body.get("dry_run", True)

    compiled = _compile_patterns(patterns)

    db = await open_db()
    try:
        cursor = await db.execute(
            "SELECT hypothesis_id, name, thesis, sport FROM hypotheses WHERE status = 'draft'"
        )
        rows = await cursor.fetchall()

        matched = []
        for row in rows:
            hid, name, thesis, sport = row
            text = f"{name or ''} {thesis or ''}"
            if any(p.search(text) for p in compiled):
                matched.append({"id": hid, "name": name, "sport": sport})

        if not dry_run and matched:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            ids = [m["id"] for m in matched]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i+500]
                placeholders = ",".join("?" * len(chunk))
                params = tuple([now] + chunk)
                await db.execute(
                    f"UPDATE hypotheses SET status = 'rejected', updated_at = ?, "
                    f"promoted_by = 'batch_purge:generic_edge' "
                    f"WHERE hypothesis_id IN ({placeholders})",
                    params,
                )
            await db.commit()
            logger.info(f"Batch rejected {len(matched)} generic draft hypotheses")

        by_sport = {}
        for m in matched:
            by_sport[m["sport"]] = by_sport.get(m["sport"], 0) + 1

        return {
            "matched": len(matched),
            "dry_run": dry_run,
            "by_sport": by_sport,
            "sample": [m["name"] for m in matched[:20]],
        }
    finally:
        await db.close()


async def get_research_sports():
    """Get all researched sports — all compete equally."""
    from tools.autonomous import RESEARCH_SPORTS
    return {"sports": RESEARCH_SPORTS}
