"""
Database utilities — retry wrappers and write serialization for SQLite.

SQLite's WAL mode allows concurrent reads but writer-writer contention
still causes "database is locked" errors. This module provides:

1. execute_with_retry(): Exponential backoff retry for any DB write
2. WriteLock: asyncio.Lock-based serialization for hot-path writers
3. commit_with_retry(): Retry wrapper specifically for commits

All production writes should go through these utilities to prevent
silent data loss from lock contention.
"""

import asyncio
import logging
import random
import re
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.db_utils")


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
    """
    Execute a SQL statement with retry on database lock.

    Exponential backoff: 0.5s, 1s, 2s, 4s, 8s + jitter.
    Same pattern proven in hypothesis.py and task_queue.py.

    Args:
        db: aiosqlite connection
        sql: SQL statement
        params: query parameters
        max_retries: maximum retry attempts
        operation: human-readable description for logging

    Returns:
        aiosqlite.Cursor from the successful execution
    """
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
    """
    Commit with retry on database lock.

    A failed commit can lose an entire batch of writes. This ensures
    commits succeed even under contention.
    """
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
