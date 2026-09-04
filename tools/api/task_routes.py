"""Task/session/world/context-sync/restart route handler bodies (moved from
api.py, slice 4).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``memory``, ``queue``)
via a late ``from api import ...`` inside the function body to avoid a
circular import at module load time.

CRITICAL GATING CONTRACT (pinned by tests/test_api_slice4.py):
  * POST /task stays on the public-write allowlist via public_endpoint().
  * GET /task/{id}, /task/{id}/chain, /session/{id}, /world/{domain},
    /tasks keep require_admin_or_loopback.
  * POST /context/sync keeps require_admin.
  * /admin/restart keeps require_admin_or_loopback + confirm=YES.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
from typing import Optional

import aiosqlite
from fastapi import HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("callisto.api")


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class TaskSubmission(BaseModel):
    query: str = Field(..., min_length=1, max_length=20000)
    priority: int = Field(default=0, ge=-10, le=10)


class TaskResponse(BaseModel):
    task_id: int


class ContextSync(BaseModel):
    session_summary: str = Field(..., min_length=1, max_length=20000)
    actionable_queries: list[str] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------------------
# Wiki task short-circuit helper
# ---------------------------------------------------------------------------

async def wiki_task_short_circuit(query: str) -> Optional[dict]:
    """Look up a pre-existing wiki answer for ``query``.

    Returns a result-dict suitable for ``queue.complete_task(...)`` when a
    high-similarity match is found, else None. All failures return None —
    the task proceeds normally through the orchestrator.
    """
    from api import memory

    try:
        threshold = float(os.getenv("CALLISTO_TASK_SHORT_CIRCUIT_THRESHOLD", "0.88"))
        from tools.knowledge_wiki import get_wiki
        wiki = get_wiki()
        async with aiosqlite.connect(memory.db_path) as wdb:
            await wdb.execute("PRAGMA busy_timeout = 20000")
            hits = await wiki.search(wdb, query, top_k=1, min_similarity=0.0)
        if not hits:
            return None
        top = hits[0]
        sim = top.get("similarity")
        if not isinstance(sim, (int, float)) or sim < threshold:
            return None
        return {
            "short_circuited": True,
            "wiki_topic": top.get("topic"),
            "wiki_title": top.get("title"),
            "wiki_similarity": round(sim, 4),
            "wiki_domain": top.get("domain"),
            "wiki_confidence": top.get("confidence"),
            "conclusion": top.get("summary") or top.get("content"),
            "confidence_score": top.get("confidence") or 0.5,
            "domain": top.get("domain") or "GENERAL",
            "source": "wiki_short_circuit",
        }
    except Exception as e:
        logger.debug(f"Wiki task short-circuit skipped: {e}")
        return None


# ---------------------------------------------------------------------------
# Handler bodies
# ---------------------------------------------------------------------------

async def submit_task(submission: TaskSubmission) -> TaskResponse:
    """Submit a query for AGP session processing.

    Writes are auth-gated: without this, a caller could queue arbitrary LLM
    work against the billing account. GET /task/{id} is already gated, so
    writes must match.

    Wiki task short-circuit (feat/wiki-in-the-loop, 2026-04-22):
      Before enqueueing, embed the query and semantic-search the wiki. If
      a high-similarity (>0.88) article exists, create the task in COMPLETED
      state immediately with the wiki article as its result — saving ~5min
      orchestrator cycles on duplicate queries. Toggle:
      ``CALLISTO_TASK_SHORT_CIRCUIT=1`` (default on).
    """
    from api import queue

    try:
        # Short-circuit pass — safe on any failure (returns None).
        short_circuit_result = None
        if os.getenv("CALLISTO_TASK_SHORT_CIRCUIT", "1") == "1":
            short_circuit_result = await wiki_task_short_circuit(submission.query)

        from tools.api.workers import _post_task_orchestrator_forbidden
        if _post_task_orchestrator_forbidden(short_circuit_result):
            raise HTTPException(
                status_code=403,
                detail="CALLISTO_LOCAL_ONLY forbids POST /task orchestrator "
                       "(Claude/hosted). Use callisto ask --backend gpu1 "
                       "or unset CALLISTO_LOCAL_ONLY.",
            )

        task_id = await queue.submit_task(submission.query, submission.priority)

        if short_circuit_result is not None:
            try:
                await queue.complete_task(task_id, short_circuit_result)
                logger.info(
                    f"Task {task_id} SHORT-CIRCUITED via wiki "
                    f"(topic={short_circuit_result.get('wiki_topic')}, "
                    f"sim={short_circuit_result.get('wiki_similarity')})"
                )
            except Exception as e:
                # If we can't mark it complete, leave it PENDING — the worker
                # will pick it up normally. Not fatal.
                logger.warning(f"Short-circuit complete_task failed for {task_id}: {e}")

        return TaskResponse(task_id=task_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /task failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


async def get_task(task_id: int):
    """Get task status and result."""
    from api import queue

    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def get_task_chain(task_id: int):
    """Return the full followup tree rooted at ``task_id``'s 0-depth ancestor.

    Enables "where did this task come from / what else did it spawn?"
    debugging. Includes total cost and max-depth so a runaway chain is
    visible at a glance.

    Loopback-or-admin gated: same auth posture as GET /task/{id} since
    the chain leaks the same query text.
    """
    from api import memory

    from tools.followup_guard import get_chain_tree
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        tree = await get_chain_tree(db, task_id)
    if tree.get("error") == "task_not_found":
        raise HTTPException(status_code=404, detail="Task not found")
    return tree


async def get_session(session_id: str):
    """Get a sealed AGP session with full provenance.

    Returns 409 CONFLICT if the stored seal_hash fails verification — the
    session exists but its content has been tampered with or corrupted.
    """
    from agp import AGPSealTampered
    from api import memory

    try:
        session = await memory.get_session(session_id)
    except AGPSealTampered as e:
        logger.error("Seal tamper detected on GET /session/%s: %s", session_id, e)
        raise HTTPException(
            status_code=409,
            detail=f"Session {session_id} seal failed verification (tampered or corrupted)",
        )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def query_world(
    domain: str,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    limit: int = 50,
):
    """Query a domain world. When ``keyword`` is present, retrieval is
    SEMANTIC (vector similarity) with a keyword-LIKE fallback; otherwise
    the recent-first ordering is used.

    Loopback-or-admin gated: world memory can contain tagged research
    (financial, signal, synthesis) we don't want to leak to unauth'd callers
    if CALLISTO_BIND_HOST is ever set non-loopback.

    SECURITY (audit 2026-04-21): `limit` is hard-capped at 500 to prevent
    memory-exhaustion via `?limit=1000000`.
    """
    from agp import Domain
    from api import memory

    try:
        domain_enum = Domain(domain.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {[d.value for d in Domain]}",
        )
    # Cap limit defensively — anything beyond 500 materialises gigabytes on
    # well-populated domains and is almost never a legitimate query.
    # Also coerces limit to int to reject `?limit=foo`.
    limit = max(1, min(int(limit), 500))
    results = await memory.query_world(
        domain_enum, keyword=keyword, min_confidence=min_confidence, limit=limit
    )
    return {"domain": domain_enum.value, "count": len(results), "entries": results}


async def list_tasks(status: Optional[str] = None, limit: int = 10):
    """List recent tasks from the queue.

    Loopback-or-admin gated: task rows embed the original user query text and
    session_ids, which leak conversation content if reachable non-loopback.
    `/task/{id}` was already gated; this brings the bulk listing in line.
    """
    from api import queue

    # Refresh WAL snapshot to see externally-committed rows
    try:
        await queue._db.commit()
    except Exception:
        pass
    limit = max(1, min(int(limit), 500))
    rows = await queue._db.execute_fetchall(
        """SELECT task_id, query, status, priority, session_id,
                  created_at, started_at, completed_at
           FROM task_queue
           ORDER BY created_at DESC LIMIT ?""",
        (limit,)
    )
    columns = ["task_id", "query", "status", "priority", "session_id",
               "created_at", "started_at", "completed_at"]
    tasks = [dict(zip(columns, row)) for row in rows]
    if status:
        tasks = [t for t in tasks if t["status"] == status.upper()]
    return {"count": len(tasks), "tasks": tasks}


async def sync_context(ctx: ContextSync):
    """Receive context from a Claude Code session. Queues actionable items."""
    from api import queue

    submitted = []
    for q in ctx.actionable_queries:
        if not q or len(q) > 20000:
            raise HTTPException(status_code=422, detail="actionable_queries entries must be 1-20000 chars")
        task_id = await queue.submit_task(q, priority=1)
        submitted.append(task_id)
    return {
        "received": True,
        "tasks_submitted": len(submitted),
        "task_ids": submitted,
    }


async def admin_restart(confirm: str = "", set_restart_task=None):
    """Graceful restart — exits process, watchdog brings it back with new code.

    Requires confirm=YES to prevent accidental restarts.
    Without watchdog.bat running, this will KILL the system with no relaunch.

    Auth: admin-token OR loopback.  Previously required CALLISTO_ADMIN_TOKEN
    unconditionally, which meant localhost scripts (and the human using curl)
    had no restart path when the token was unset — forcing reliance on the
    signal file and the watchdog picking it up.  Loopback-allowed restores
    an in-process restart path even with the token unset.

    ``set_restart_task`` is injected by api.py so its shutdown handler can
    still cancel the delayed-exit task cleanly (audit H-14).
    """
    # SECURITY: timing-safe equality (audit C-2). Token is "YES" — short, but pattern is
    # what matters: never use `==` or `!=` on auth-adjacent strings.
    if not _secrets.compare_digest(confirm, "YES"):
        raise HTTPException(
            status_code=400,
            detail="Add ?confirm=YES to actually restart. WARNING: without watchdog, system will not relaunch.",
        )
    logger.info("RESTART REQUESTED via /admin/restart — shutting down gracefully")
    from tools import telegram
    send_msg = "Callisto restarting (code reload requested)"
    try:
        await telegram.alert_system(send_msg)
    except Exception as e:
        logger.info(f"Telegram restart notification failed (non-critical): {e}")

    # Give time for this response to be sent, then exit
    async def _delayed_exit():
        await asyncio.sleep(1)
        logger.info("Exiting for restart...")
        os._exit(0)

    # Track task so shutdown handler can cancel it cleanly (audit H-14).
    restart_task = asyncio.create_task(_delayed_exit())
    if set_restart_task is not None:
        set_restart_task(restart_task)
    return {"status": "restarting", "message": "Watchdog will restart with new code in ~15 seconds"}
