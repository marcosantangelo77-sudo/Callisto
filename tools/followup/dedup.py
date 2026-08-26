"""tools.followup.dedup — semantic near-duplicate detection for followups."""

from __future__ import annotations

import logging
from typing import Optional

import aiosqlite

from tools.followup.env import dedup_threshold, dedup_window_seconds
from tools.followup.quality import strip_followup_header as _strip_followup_header

logger = logging.getLogger("callisto.followup_guard")


async def find_near_duplicate(
    db: aiosqlite.Connection,
    followup_query: str,
    window_seconds: Optional[int] = None,
    threshold: Optional[float] = None,
) -> Optional[int]:
    """Return the task_id of a near-duplicate task in the recent window, or None.

    The check embeds ``followup_query`` and compares against recent queries
    embedded on the fly (no persistent store for followup dedup — the
    cardinality is low so a linear scan over last-hour rows is cheap).

    Any failure in embedding/DB path returns ``None`` (dedup best-effort;
    never block a followup on vector-store flakes).
    """
    window = window_seconds if window_seconds is not None else dedup_window_seconds()
    thr = threshold if threshold is not None else dedup_threshold()

    try:
        # Refresh WAL
        try:
            await db.commit()
        except Exception:
            pass
        cur = await db.execute(
            "SELECT task_id, query FROM task_queue "
            "WHERE created_at > datetime('now', ?) "
            "ORDER BY created_at DESC LIMIT 200",
            (f"-{int(window)} seconds",),
        )
        rows = await cur.fetchall()
    except Exception as e:
        logger.debug("dedup: task_queue scan failed: %r", e)
        return None

    if not rows:
        return None

    # First pass: exact-normalised-string match. Catches the
    # "LLM emitted literally the same next-step twice" pattern without
    # any embedding calls.
    target_norm = _strip_followup_header(followup_query).lower().strip()
    for task_id, q in rows:
        if _strip_followup_header(q or "").lower().strip() == target_norm:
            return int(task_id)

    # Second pass: embedding cosine. Lazy-import to keep the followup
    # package import-cheap for test paths that don't need Ollama.
    try:
        from tools.embeddings import embed_text, cosine_similarity
    except Exception as e:
        logger.debug("dedup: embeddings unavailable: %r", e)
        return None

    try:
        target_vec = await embed_text(target_norm)
    except Exception as e:
        logger.debug("dedup: embed(target) failed: %r", e)
        return None

    best_id = None
    best_sim = 0.0
    for task_id, q in rows:
        if not q:
            continue
        try:
            cand_vec = await embed_text(_strip_followup_header(q))
        except Exception:
            continue
        sim = cosine_similarity(target_vec, cand_vec)
        if sim > best_sim:
            best_sim = sim
            best_id = int(task_id)

    if best_id is not None and best_sim >= thr:
        logger.info(
            "dedup: followup matched task %d at cosine=%.3f (>= %.2f)",
            best_id, best_sim, thr,
        )
        return best_id
    return None
