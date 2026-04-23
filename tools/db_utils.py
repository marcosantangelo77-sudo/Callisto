"""
Database utilities — retry wrappers and write serialization for SQLite.

PREFERRED PATH: ``tools.db_writer.WriteCoordinator``. The single-writer
coordinator owns one connection per DB inside this process and serialises
every write through one queue, so there is never any in-process writer-writer
contention to retry. The legacy retry helpers below auto-detect a running
coordinator and delegate to it (see ``execute_with_retry`` and
``commit_with_retry``); they only fall back to the old per-connection retry
loop when no coordinator has been started yet (eg. ad-hoc query scripts).

LEGACY PATH (when no coordinator is up):

1. execute_with_retry(): Exponential backoff retry for any DB write
2. WriteLock: asyncio.Lock-based serialization for hot-path writers (per-process)
3. commit_with_retry(): Retry wrapper specifically for commits
"""

import asyncio
import logging
import random
import re
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.db_utils")


def _coord_for(db: aiosqlite.Connection):
    """Return the WriteCoordinator covering this connection's DB, if running.

    The coordinator path is opt-in via the ``_callisto_db_path`` attribute set
    by ``tools.schema.open_db``. Connections opened with raw ``aiosqlite.connect``
    won't carry the tag and will fall through to the legacy retry path — this
    is intentional so ad-hoc query scripts and modules using their own
    connection tuning aren't silently rerouted.

    Returns ``None`` on any failure so the legacy path stays a strict superset.
    """
    try:
        db_path = getattr(db, "_callisto_db_path", None)
        if not db_path:
            return None
        from tools.db_writer import get_writer_if_running
        return get_writer_if_running(db_path)
    except Exception:
        return None


class _CoordCursor:
    """Cursor-shaped shim returned by execute_with_retry when delegating.

    Callers that only read ``.lastrowid`` / ``.rowcount`` keep working without
    code changes. Callers that try to iterate the cursor will get an explicit
    error pointing them at the coordinator's read path (which is: don't —
    reads should use a direct connection, the coordinator only owns writes).
    """

    def __init__(self, value: int):
        # We can't tell INSERT lastrowid apart from UPDATE rowcount at the
        # cursor level — the coordinator collapses both into one int. Mirror
        # it onto both attributes so existing callers see what they expect.
        self.lastrowid = value if value > 0 else 0
        self.rowcount = value

    async def fetchone(self):  # pragma: no cover - not the coordinator's job
        raise RuntimeError(
            "Cursor returned by the WriteCoordinator path supports only "
            "lastrowid/rowcount. For SELECTs, use a direct read connection."
        )

    async def fetchall(self):  # pragma: no cover
        raise RuntimeError(
            "Cursor returned by the WriteCoordinator path supports only "
            "lastrowid/rowcount. For SELECTs, use a direct read connection."
        )


# SECURITY (audit C-5): SQLite identifiers (table / column / index names) cannot
# be parameterized via ? placeholders. Anywhere we splice an identifier into a
# query string, the splice MUST go through this validator. The check is strict
# (ASCII letters / digits / underscore only, max 64 chars, no leading digit) and
# rejects any other character — including quotes, semicolons, spaces, dashes,
# unicode lookalikes, and dotted "schema.table" forms (use schema-qualified
# logic at the caller instead).
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def safe_ident(name: str) -> str:
    """Return ``name`` if it is a safe SQLite identifier; raise ValueError otherwise.

    Use as: ``f"SELECT COUNT(*) FROM {safe_ident(table)}"``.
    The function never quotes — it asserts. Callers that want belt-and-braces can
    additionally compare against an explicit allowlist set before passing in.
    """
    if not isinstance(name, str) or not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name

# Shared write lock — all hot-path writers acquire this before bulk writes.
# This eliminates writer-writer contention at the application level.
_write_lock: Optional[asyncio.Lock] = None


def get_write_lock() -> asyncio.Lock:
    """Get the global write lock singleton."""
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    return _write_lock


async def execute_with_retry(
    db: aiosqlite.Connection,
    sql: str,
    params: tuple = (),
    max_retries: int = 5,
    operation: str = "",
) -> aiosqlite.Cursor:
    """Execute a write. Routes through the WriteCoordinator when active.

    PREFERRED: when the WriteCoordinator is running for this DB (started in
    api.py lifespan), the call is queued onto the coordinator and the result
    is returned via a shim that exposes ``.lastrowid`` and ``.rowcount`` —
    no per-connection writer-writer contention can happen.

    LEGACY FALLBACK: when no coordinator is running (eg. ad-hoc query
    scripts), the original exponential-backoff retry loop runs against the
    caller's own connection. Backoff: 0.5s, 1s, 2s, 4s, 8s + jitter.
    """
    coord = _coord_for(db)
    if coord is not None:
        try:
            value = await coord.execute(sql, params)
            return _CoordCursor(value)
        except Exception:
            # If the coordinator path raises, fall through to the legacy retry
            # against the caller's connection so a transient coordinator hiccup
            # doesn't take the whole call site down.
            logger.warning(
                f"WriteCoordinator failed for {operation or 'execute'}; "
                "falling back to direct execute."
            )

    for attempt in range(max_retries):
        try:
            cursor = await db.execute(sql, params)
            return cursor
        except Exception as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                wait = min(0.5 * (2 ** attempt), 8) + random.uniform(0, 0.5)
                logger.warning(
                    f"DB locked on {operation or 'execute'} "
                    f"(attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
            else:
                raise
    # Should never reach here, but just in case
    return await db.execute(sql, params)


async def commit_with_retry(
    db: aiosqlite.Connection,
    max_retries: int = 5,
    operation: str = "",
) -> None:
    """Commit a write. No-op when the coordinator path is in use.

    The coordinator commits internally after each ``execute`` /
    ``executemany`` / ``transaction``, so a follow-on ``commit`` from the
    caller's own connection is unnecessary (and would commit nothing on a
    connection that didn't itself write). When no coordinator is running,
    falls back to the original retry loop.
    """
    if _coord_for(db) is not None:
        return
    for attempt in range(max_retries):
        try:
            await db.commit()
            return
        except Exception as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                wait = min(0.5 * (2 ** attempt), 8) + random.uniform(0, 0.5)
                logger.warning(
                    f"DB locked on commit ({operation or 'unknown'}) "
                    f"(attempt {attempt + 1}/{max_retries}), "
                    f"retrying in {wait:.1f}s"
                )
                await asyncio.sleep(wait)
            else:
                raise


async def bulk_write(
    db: aiosqlite.Connection,
    statements: list[tuple[str, tuple]],
    operation: str = "",
) -> int:
    """
    Execute multiple writes under the global write lock with retry.

    Acquires the write lock, executes all statements, commits once.
    This is the safest way to do bulk writes in Callisto.

    Args:
        db: aiosqlite connection
        statements: list of (sql, params) tuples
        operation: description for logging

    Returns:
        Number of statements successfully executed
    """
    lock = get_write_lock()
    async with lock:
        count = 0
        for sql, params in statements:
            try:
                await execute_with_retry(db, sql, params, operation=operation)
                count += 1
            except Exception as e:
                logger.warning(f"Bulk write failed on statement {count + 1}: {e}")
        if count > 0:
            await commit_with_retry(db, operation=operation)
        return count
