"""
Persistent task queue backed by SQLite.

Named task_queue.py (not queue.py) to avoid collision with Python stdlib.
Shares callisto.db with the memory system; uses WAL mode.
"""

import json
import os
import random
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _record_lock_hit(operation: str) -> None:
    """Mirror SQLite 'database is locked' retries into the metrics registry.

    Isolated helper so the retry loop body doesn't depend on the metrics
    module being importable — failures here are swallowed.
    """
    try:
        from tools.metrics import record_db_lock_hit
        record_db_lock_hit(operation)
    except Exception:
        pass

TASK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_queue (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'TIMEOUT')),
    priority INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_queue_poll
    ON task_queue(status, priority DESC, created_at);
"""


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class TaskQueue:
    """Persistent task queue using SQLite."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        # Tag for WriteCoordinator routing (single-writer pattern). When the
        # coordinator is up, submit_task / get_next / complete_task all funnel
        # their writes through it instead of competing on the local connection.
        try:
            self._db._callisto_db_path = os.path.abspath(self.db_path)
        except Exception:
            pass
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA wal_autocheckpoint = 1000")
        await self._db.execute("PRAGMA journal_size_limit = 67108864")
        await self._db.execute("PRAGMA busy_timeout = 120000")
        # Per-statement DDL avoids the EXCLUSIVE lock that executescript holds
        # for the duration of the script.
        for stmt in (s.strip() for s in TASK_SCHEMA_SQL.split(";") if s.strip()):
            await self._db.execute(stmt)
        await self._db.commit()
        await self._recover_stuck_tasks()

    async def _recover_stuck_tasks(self) -> None:
        """On startup, fail any tasks stuck in PROCESSING — the previous
        process died before completing them."""
        import logging
        logger = logging.getLogger("callisto.task_queue")
        try:
            cursor = await self._db.execute(
                """UPDATE task_queue
                   SET status = 'FAILED',
                       error = 'Recovered on restart: stuck in PROCESSING',
                       completed_at = ?
                   WHERE status = 'PROCESSING'""",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._db.commit()
            if cursor.rowcount > 0:
                logger.warning(f"Recovered {cursor.rowcount} stuck task(s)")
        except Exception as e:
            logger.debug(f"Stuck task recovery: {e}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def submit_task(self, query: str, priority: int = 0) -> int:
        """Submit a new task. Returns task_id.

        Routes through the WriteCoordinator when active so /task POST never
        competes with the autonomous loop's writes for the SQLite writer lock.
        Falls back to the original retry loop when no coordinator is running.
        """
        import asyncio as _asyncio
        # Fast path: WriteCoordinator (single in-process writer).
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        if coord is not None:
            return await coord.execute(
                """INSERT INTO task_queue (query, priority, created_at)
                   VALUES (?, ?, ?)""",
                (query, priority, datetime.now(timezone.utc).isoformat()),
            )
        # Legacy path (ad-hoc scripts where coordinator never started).
        for attempt in range(8):
            try:
                cursor = await self._db.execute(
                    """INSERT INTO task_queue (query, priority, created_at)
                       VALUES (?, ?, ?)""",
                    (query, priority, datetime.now(timezone.utc).isoformat()),
                )
                await self._db.commit()
                return cursor.lastrowid
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    _record_lock_hit("task_queue")
                    wait = min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5)
                    await _asyncio.sleep(wait)
                else:
                    raise

    async def get_next(self) -> Optional[dict]:
        """Atomically claim the next pending task. Returns task dict or None.

        Read of the next-pending row uses the local connection (reads don't
        contend in WAL); the claiming UPDATE routes through the
        WriteCoordinator when active so it never competes with other writers.
        """
        import asyncio as _asyncio
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        # Commit to release any implicit read transaction — refreshes WAL snapshot
        # so we can see rows inserted by external processes (e.g. direct DB writes)
        try:
            await self._db.commit()
        except Exception:
            pass
        for attempt in range(8):
            try:
                row = await self._db.execute_fetchall(
                    """SELECT task_id, query, priority FROM task_queue
                       WHERE status = 'PENDING'
                       ORDER BY priority DESC, created_at ASC
                       LIMIT 1""",
                )
                if not row:
                    return None

                task_id, query, priority = row[0]
                now = datetime.now(timezone.utc).isoformat()

                if coord is not None:
                    rowcount = await coord.execute(
                        """UPDATE task_queue
                           SET status = 'PROCESSING', started_at = ?
                           WHERE task_id = ? AND status = 'PENDING'""",
                        (now, task_id),
                    )
                    if rowcount == 0:
                        return None
                else:
                    cursor = await self._db.execute(
                        """UPDATE task_queue
                           SET status = 'PROCESSING', started_at = ?
                           WHERE task_id = ? AND status = 'PENDING'""",
                        (now, task_id),
                    )
                    await self._db.commit()
                    if cursor.rowcount == 0:
                        return None

                return {"task_id": task_id, "query": query, "priority": priority}
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    _record_lock_hit("task_queue")
                    wait = min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5)
                    await _asyncio.sleep(wait)
                else:
                    raise

    async def complete_task(
        self, task_id: int, result: dict, session_id: Optional[str] = None
    ) -> None:
        """Mark a task as completed with its result.

        Routes through the WriteCoordinator when active.
        """
        import asyncio as _asyncio
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        if coord is not None:
            await coord.execute(
                """UPDATE task_queue
                   SET status = 'COMPLETED', result = ?, session_id = ?,
                       completed_at = ?
                   WHERE task_id = ?""",
                (
                    json.dumps(result, ensure_ascii=False),
                    session_id,
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                ),
            )
            return
        for attempt in range(8):
            try:
                await self._db.execute(
                    """UPDATE task_queue
                       SET status = 'COMPLETED', result = ?, session_id = ?,
                           completed_at = ?
                       WHERE task_id = ?""",
                    (
                        json.dumps(result, ensure_ascii=False),
                        session_id,
                        datetime.now(timezone.utc).isoformat(),
                        task_id,
                    ),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    _record_lock_hit("task_queue")
                    await _asyncio.sleep(min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5))
                else:
                    raise

    async def fail_task(self, task_id: int, error: str) -> None:
        """Mark a task as failed with error details.

        Routes through the WriteCoordinator when active.
        """
        import asyncio as _asyncio
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        if coord is not None:
            await coord.execute(
                """UPDATE task_queue
                   SET status = 'FAILED', error = ?, completed_at = ?
                   WHERE task_id = ?""",
                (error, datetime.now(timezone.utc).isoformat(), task_id),
            )
            return
        for attempt in range(8):
            try:
                await self._db.execute(
                    """UPDATE task_queue
                       SET status = 'FAILED', error = ?, completed_at = ?
                       WHERE task_id = ?""",
                    (error, datetime.now(timezone.utc).isoformat(), task_id),
                )
                await self._db.commit()
                return
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    _record_lock_hit("task_queue")
                    await _asyncio.sleep(min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5))
                else:
                    raise

    async def timeout_task(
        self,
        task_id: int,
        error: str,
        result: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> None:
        """Mark a task as TIMEOUT — distinct from FAILED.

        Stores any partial session state in ``result`` so downstream retry
        logic can see how far the orchestrator got before the watchdog
        yanked it. Falls back to ``fail_task`` semantics if the DB schema
        hasn't migrated yet (CHECK constraint rejects 'TIMEOUT').
        """
        import asyncio as _asyncio
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        now = datetime.now(timezone.utc).isoformat()
        result_json = json.dumps(result, ensure_ascii=False) if result else None

        async def _run(is_timeout: bool) -> None:
            status = "TIMEOUT" if is_timeout else "FAILED"
            if coord is not None:
                await coord.execute(
                    """UPDATE task_queue
                       SET status = ?, error = ?, result = ?, session_id = ?,
                           completed_at = ?
                       WHERE task_id = ?""",
                    (status, error, result_json, session_id, now, task_id),
                )
                return
            for attempt in range(8):
                try:
                    await self._db.execute(
                        """UPDATE task_queue
                           SET status = ?, error = ?, result = ?, session_id = ?,
                               completed_at = ?
                           WHERE task_id = ?""",
                        (status, error, result_json, session_id, now, task_id),
                    )
                    await self._db.commit()
                    return
                except Exception as e:
                    if "locked" in str(e).lower() and attempt < 7:
                        _record_lock_hit("task_queue")
                        await _asyncio.sleep(
                            min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5)
                        )
                    else:
                        raise

        try:
            await _run(is_timeout=True)
        except Exception as e:
            # CHECK constraint failure on pre-migration DBs — fall back.
            if "CHECK constraint" in str(e) or "constraint failed" in str(e).lower():
                await _run(is_timeout=False)
            else:
                raise

    async def get_task(self, task_id: int) -> Optional[dict]:
        """Get task by ID."""
        # Refresh WAL snapshot so we see externally-committed rows
        try:
            await self._db.commit()
        except Exception:
            pass
        rows = await self._db.execute_fetchall(
            "SELECT * FROM task_queue WHERE task_id = ?", (task_id,)
        )
        if not rows:
            return None

        columns = [
            "task_id", "query", "status", "priority", "result", "error",
            "session_id", "created_at", "started_at", "completed_at",
        ]
        task = dict(zip(columns, rows[0]))
        if task["result"]:
            task["result"] = json.loads(task["result"])
        return task
