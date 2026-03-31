"""Deferred work queue + local model fallbacks — keeps the loop productive 24/7.

When Claude is unavailable (rate-limited or down), work is:
  1. Enqueued for execution when Claude returns
  2. Handled locally via the model fallback ladder (best available model)

When Claude becomes available again, the queue drains immediately — highest
priority items first. No work is ever silently dropped.

Claude downtime is tracked and patterns recorded to Hermes for observability.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.work_queue")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
LOCAL_MODEL = os.getenv("LOCAL_FALLBACK_MODEL", "qwen3.5:4b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")


# ── Local Ollama helper ──

async def _call_local_model(prompt: str, model: str = None, max_tokens: int = 500) -> str:
    """Call local Ollama model via the fallback ladder. Tries best available model."""
    if model:
        # Explicit model requested — use it directly
        try:
            import httpx
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{OLLAMA_URL}/api/generate", json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                })
                if r.status_code == 200:
                    return r.json().get("response", "")
        except Exception as e:
            logger.debug(f"Local model call ({model}) failed: {e}")
        return ""

    # Use fallback ladder: try models in quality order
    fallback_models = [
        "devstral-small-2",           # Best local tool use (24B, SWE-bench leader, fits 16GB VRAM)
        "qwen3:14b",                  # Best local all-rounder (9GB, matches 32B quality)
        "gpt-oss:20b",                # Fast reasoning (140 tok/s)
        "deepseek-r1:14b",            # Deep chain-of-thought
        LOCAL_MODEL,                   # Fast classification fallback (qwen3.5:4b)
    ]
    import httpx
    for m in fallback_models:
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{OLLAMA_URL}/api/generate", json={
                    "model": m,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                })
                if r.status_code == 200:
                    content = r.json().get("response", "")
                    if content:
                        logger.info(f"Local fallback: {m} succeeded ({len(content)} chars)")
                        return content
        except Exception as e:
            logger.debug(f"Local model {m} failed: {e}")
            continue
    return ""


# ── Deferred Work Queue ──

class DeferredWorkQueue:
    """Queue work when Claude is unavailable, execute when it returns.

    Backed by SQLite so items survive process restarts. Priority-ordered
    drain ensures the most important deferred work runs first.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._initialized = False

    async def _ensure_table(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS deferred_work_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_type TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT,
                    executed_at TEXT
                )
            """)
            from tools.db_utils import commit_with_retry
            await commit_with_retry(db, operation="work_queue schema")
        self._initialized = True

    async def enqueue(self, work_type: str, prompt: str, priority: int = 5) -> None:
        """Add work to the deferred queue. Lower priority number = higher priority."""
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            # Cap at 50 pending items — drop lowest priority if full
            row = await (await db.execute(
                "SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'pending'"
            )).fetchone()
            from tools.db_utils import execute_with_retry, commit_with_retry
            if row and row[0] >= 50:
                # Delete lowest priority (highest number) pending item
                await execute_with_retry(
                    db,
                    "DELETE FROM deferred_work_queue WHERE id = ("
                    "  SELECT id FROM deferred_work_queue WHERE status = 'pending' "
                    "  ORDER BY priority DESC, created_at ASC LIMIT 1"
                    ")",
                    operation="work_queue enqueue_trim",
                )
            await execute_with_retry(
                db,
                "INSERT INTO deferred_work_queue (work_type, prompt, priority, created_at) "
                "VALUES (?, ?, ?, ?)",
                (work_type, prompt, priority, datetime.now(timezone.utc).isoformat()),
                operation="work_queue enqueue",
            )
            await commit_with_retry(db, operation="work_queue enqueue")
        logger.info(f"Work queued: type={work_type}, priority={priority}")

    async def drain(self, max_items: int = 5) -> list[dict]:
        """Return and mark up to max_items pending items, highest priority first."""
        await self._ensure_table()
        items = []
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            rows = await (await db.execute(
                "SELECT id, work_type, prompt, priority, created_at "
                "FROM deferred_work_queue "
                "WHERE status = 'pending' "
                "ORDER BY priority ASC, created_at ASC "
                "LIMIT ?",
                (max_items,),
            )).fetchall()
            for row in rows:
                items.append({
                    "id": row[0],
                    "work_type": row[1],
                    "prompt": row[2],
                    "priority": row[3],
                    "created_at": row[4],
                })
                from tools.db_utils import execute_with_retry, commit_with_retry
                await execute_with_retry(
                    db,
                    "UPDATE deferred_work_queue SET status = 'draining' WHERE id = ?",
                    (row[0],),
                    operation="work_queue drain",
                )
            if items:
                from tools.db_utils import commit_with_retry
                await commit_with_retry(db, operation="work_queue drain")
        return items

    async def mark_done(self, item_id: int, result: str = "") -> None:
        """Mark a drained item as completed."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                db,
                "UPDATE deferred_work_queue SET status = 'done', result = ?, "
                "executed_at = ? WHERE id = ?",
                (result[:2000], datetime.now(timezone.utc).isoformat(), item_id),
                operation="work_queue mark_done",
            )
            await commit_with_retry(db, operation="work_queue mark_done")

    async def mark_failed(self, item_id: int, error: str = "") -> None:
        """Mark a drained item as failed — it goes back to pending for retry."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                db,
                "UPDATE deferred_work_queue SET status = 'pending', "
                "result = ? WHERE id = ?",
                (f"failed: {error[:500]}", item_id),
                operation="work_queue mark_failed",
            )
            await commit_with_retry(db, operation="work_queue mark_failed")

    async def size(self) -> int:
        """Return count of pending items."""
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            row = await (await db.execute(
                "SELECT COUNT(*) FROM deferred_work_queue WHERE status = 'pending'"
            )).fetchone()
            return row[0] if row else 0

    async def get_status(self) -> dict:
        """Return queue status."""
        await self._ensure_table()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            counts = {}
            rows = await (await db.execute(
                "SELECT status, COUNT(*) FROM deferred_work_queue GROUP BY status"
            )).fetchall()
            for r in rows:
                counts[r[0]] = r[1]
        return {"pending": counts.get("pending", 0),
                "done": counts.get("done", 0),
                "draining": counts.get("draining", 0)}


# ── Claude Downtime Tracker ──

class ClaudeDowntimeTracker:
    """Track Claude availability patterns for Hermes observability."""

    def __init__(self):
        self._outage_start: Optional[float] = None
        self._items_queued_during_outage = 0
        self._total_outages = 0
        self._total_downtime_seconds = 0.0
        self._last_outage_duration = 0.0

    def mark_unavailable(self) -> None:
        """Called when Claude becomes unavailable."""
        if self._outage_start is None:
            self._outage_start = time.monotonic()
            self._items_queued_during_outage = 0
            self._total_outages += 1
            logger.info("Claude downtime tracker: outage started")

    def mark_available(self) -> None:
        """Called when Claude becomes available again."""
        if self._outage_start is not None:
            duration = time.monotonic() - self._outage_start
            self._last_outage_duration = duration
            self._total_downtime_seconds += duration
            logger.info(
                f"Claude downtime tracker: outage ended after {duration:.0f}s, "
                f"{self._items_queued_during_outage} items queued"
            )
            self._outage_start = None

    def item_queued(self) -> None:
        """Track an item being queued during an outage."""
        self._items_queued_during_outage += 1

    @property
    def is_in_outage(self) -> bool:
        return self._outage_start is not None

    def get_status(self) -> dict:
        current_duration = 0.0
        if self._outage_start is not None:
            current_duration = time.monotonic() - self._outage_start
        return {
            "in_outage": self.is_in_outage,
            "current_outage_seconds": round(current_duration, 1),
            "items_queued_this_outage": self._items_queued_during_outage,
            "total_outages": self._total_outages,
            "total_downtime_seconds": round(self._total_downtime_seconds, 1),
            "last_outage_duration_seconds": round(self._last_outage_duration, 1),
        }

    async def record_to_hermes(self) -> None:
        """Record outage pattern to Hermes for long-term pattern learning."""
        if self._total_outages == 0:
            return
        try:
            from tools.hermes_memory import get_hermes_memory
            hermes = get_hermes_memory()
            avg_duration = (self._total_downtime_seconds / max(1, self._total_outages))
            await hermes.record_learning(
                key="claude_downtime_pattern",
                value=(
                    f"Total outages: {self._total_outages}, "
                    f"avg duration: {avg_duration:.0f}s, "
                    f"total downtime: {self._total_downtime_seconds:.0f}s, "
                    f"last outage: {self._last_outage_duration:.0f}s"
                ),
                confidence=0.85,
                source="work_queue",
            )
        except Exception as e:
            logger.debug(f"Failed to record downtime to Hermes: {e}")


# ── Local Model Fallback Functions ──

async def local_fallback_hypothesis_gen(
    pipeline_state: str, existing_names: list[str], focus_context: str = ""
) -> list[dict]:
    """Generate hypotheses using local Qwen model when Claude is down.

    Not as creative as Claude, but keeps the hypothesis pipeline moving.
    Returns list of hypothesis dicts ready for create_hypothesis().
    """
    prompt = (
        "You are a sports betting research system. Generate 2 UNCONVENTIONAL hypotheses "
        "that exploit dimensions Vegas models lack columns for: team identity/cohesion, "
        "roster sociology, ref biases, scheme geometry, SGP correlation mispricing, "
        "media narrative effects, calendar quirks. Do NOT generate rest/B2B/weather/home "
        "underdog hypotheses — those are already priced correctly.\n\n"
        f"Current pipeline:\n{pipeline_state[:500]}\n\n"
        f"Existing hypothesis names (avoid duplicates): {json.dumps(existing_names[:20])}\n\n"
        "Respond ONLY with JSON:\n"
        '{"hypotheses": [{"name": "unique_snake_case", "thesis": "testable statement", '
        '"sport": "basketball_nba", "market_type": "spreads", "edge_threshold": 0.015}]}'
    )

    response = await _call_local_model(prompt, max_tokens=400)
    if not response:
        return []

    try:
        # Extract JSON
        if "{" in response:
            start = response.index("{")
            end = response.rindex("}") + 1
            parsed = json.loads(response[start:end])
            return parsed.get("hypotheses", [])
    except (json.JSONDecodeError, ValueError):
        logger.debug("Local fallback hypothesis gen: failed to parse JSON")
    return []


async def local_fallback_interpret(backtest_data: list[dict]) -> dict:
    """Interpret backtest results locally when Claude is down.

    Simple rules-based interpretation — not creative but correct.
    """
    reject_ids = []
    insights_parts = []

    for h in backtest_data:
        signals = h.get("signals", 0)
        events = h.get("events", 0)
        avg_edge = h.get("avg_edge", 0)
        hit_rate = h.get("hit_rate", 0)
        resolved = h.get("wins", 0) + h.get("losses", 0)

        if events >= 50 and signals == 0:
            reject_ids.append(h["id"])
            insights_parts.append(f"{h['name']}: 0 signals in {events} events -> reject")
        elif resolved >= 30 and avg_edge < -0.03:
            reject_ids.append(h["id"])
            insights_parts.append(f"{h['name']}: negative edge ({avg_edge:.3f}) -> reject")
        elif signals > 0 and resolved >= 20 and hit_rate > 0.53:
            insights_parts.append(f"{h['name']}: promising ({hit_rate:.1%} hit rate)")

    return {
        "reject": reject_ids,
        "modify": [],
        "insights": "; ".join(insights_parts) if insights_parts else "Insufficient data for interpretation",
    }


async def local_fallback_deep_work(db) -> dict:
    """Basic self-analysis when Claude is down.

    Queries DB for pipeline metrics and applies rule-based fixes.
    No creative hypothesis generation, but maintenance continues.
    """
    actions = {"reject_ids": [], "new_hypotheses": [], "pipeline_issues": []}
    if not db:
        return actions

    try:
        # Find zero-signal hypotheses with enough events
        cursor = await db.execute("""
            SELECT h.hypothesis_id, h.name, COUNT(*) as events,
                   SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as sigs
            FROM hypotheses h
            JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
            WHERE h.status = 'backtesting'
            GROUP BY h.hypothesis_id
            HAVING events >= 50 AND sigs = 0
        """)
        for r in await cursor.fetchall():
            actions["reject_ids"].append(r[0])

        if actions["reject_ids"]:
            actions["pipeline_issues"].append(
                f"Local fallback rejected {len(actions['reject_ids'])} zero-signal hypotheses"
            )
    except Exception as e:
        logger.debug(f"Local fallback deep work DB query failed: {e}")

    return actions


# ── Singletons ──

_work_queue: Optional[DeferredWorkQueue] = None
_downtime_tracker: Optional[ClaudeDowntimeTracker] = None


def get_work_queue() -> DeferredWorkQueue:
    global _work_queue
    if _work_queue is None:
        _work_queue = DeferredWorkQueue()
    return _work_queue


def get_downtime_tracker() -> ClaudeDowntimeTracker:
    global _downtime_tracker
    if _downtime_tracker is None:
        _downtime_tracker = ClaudeDowntimeTracker()
    return _downtime_tracker
