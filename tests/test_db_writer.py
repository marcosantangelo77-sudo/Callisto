"""Smoke + concurrency tests for the WriteCoordinator (single-writer pattern).

These tests verify the contract that closes the recurring "database is locked"
class of bugs: under concurrent writes from many producers, no exception leaks
out and every write is durable.
"""

import asyncio
import os
import sqlite3
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from tools.db_writer import WriteCoordinator, get_writer, stop_all, tag_connection
from tools.db_utils import execute_with_retry


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    # Pre-create schema
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER, who TEXT)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    yield db_path
    await stop_all()


@pytest.mark.asyncio
async def test_single_execute_returns_lastrowid(tmp_db):
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        rid = await coord.execute("INSERT INTO t (v, who) VALUES (?, ?)", (1, "a"))
        assert rid == 1
        rid2 = await coord.execute("INSERT INTO t (v, who) VALUES (?, ?)", (2, "b"))
        assert rid2 == 2
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_executemany_bulk(tmp_db):
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        rows = [(i, f"p{i}") for i in range(500)]
        rc = await coord.executemany(
            "INSERT INTO t (v, who) VALUES (?, ?)", rows
        )
        # SQLite executemany rowcount can be -1 on some builds; allow both.
        assert rc in (500, -1)
        # Verify durability via a fresh connection.
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM t")
            n = (await cur.fetchone())[0]
        assert n == 500
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_transaction_atomic(tmp_db):
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        results = await coord.transaction([
            ("INSERT INTO t (v, who) VALUES (?, ?)", (10, "x")),
            ("INSERT INTO t (v, who) VALUES (?, ?)", (20, "y")),
        ])
        assert len(results) == 2
        assert all(r > 0 for r in results)
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_transaction_rollback_on_error(tmp_db):
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        # First op succeeds, second op references nonexistent column → fail.
        with pytest.raises(Exception):
            await coord.transaction([
                ("INSERT INTO t (v, who) VALUES (?, ?)", (99, "ok")),
                ("INSERT INTO t (nope) VALUES (?)", ("bad",)),
            ])
        # Verify the first op was rolled back (transaction is atomic).
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM t WHERE v = 99")
            n = (await cur.fetchone())[0]
        assert n == 0, "Rollback should have undone the first insert"
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_concurrent_writes_no_lock_errors(tmp_db):
    """The whole point: 100 concurrent producers, ZERO 'database is locked'."""
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        async def producer(n):
            for i in range(20):
                await coord.execute(
                    "INSERT INTO t (v, who) VALUES (?, ?)", (i, f"p{n}-{i}")
                )

        # 100 producers × 20 inserts = 2000 writes.
        await asyncio.gather(*[producer(p) for p in range(100)])

        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM t")
            n = (await cur.fetchone())[0]
        assert n == 2000, f"Expected 2000 rows, got {n}"

        stats = coord.stats()
        assert stats["writes_total"] >= 2000
        assert stats["writes_failed"] == 0
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_db_utils_routes_to_coordinator(tmp_db):
    """execute_with_retry on a tagged connection auto-routes through coord."""
    coord = await get_writer(tmp_db)
    try:
        async with aiosqlite.connect(tmp_db) as conn:
            tag_connection(conn, tmp_db)
            cur = await execute_with_retry(
                conn,
                "INSERT INTO t (v, who) VALUES (?, ?)",
                (777, "via-execute_with_retry"),
                operation="route_test",
            )
            assert cur.lastrowid > 0

        # Confirm the row landed.
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM t WHERE v=777")
            n = (await cur.fetchone())[0]
        assert n == 1

        # Confirm coordinator stats incremented (proves we routed, not bypassed).
        assert coord.stats()["writes_total"] >= 1
    finally:
        await stop_all()


@pytest.mark.asyncio
async def test_db_utils_falls_back_when_no_coordinator(tmp_db):
    """Untagged connection → legacy direct retry path still works."""
    async with aiosqlite.connect(tmp_db) as conn:
        # No coordinator started, no tag — must still write successfully.
        cur = await execute_with_retry(
            conn,
            "INSERT INTO t (v, who) VALUES (?, ?)",
            (888, "legacy"),
            operation="fallback_test",
        )
        await conn.commit()
        assert cur.lastrowid > 0
