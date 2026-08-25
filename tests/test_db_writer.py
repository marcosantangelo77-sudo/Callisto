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


@pytest.mark.asyncio
async def test_aiosqlite_routing_intercepts_raw_execute(tmp_db):
    """Even modules that bypass execute_with_retry and call ``await db.execute(write_sql)``
    + ``await db.commit()`` directly must route through the coordinator after
    install_aiosqlite_routing(). This is the path that broke v1 in production —
    cache_manager / hermes / health all do raw .execute() and were contending
    with the coordinator's owned connection on the writer lock.
    """
    from tools.db_writer import install_aiosqlite_routing
    install_aiosqlite_routing()
    coord = await get_writer(tmp_db)
    try:
        async with aiosqlite.connect(tmp_db) as conn:
            cur = await conn.execute(
                "INSERT INTO t (v, who) VALUES (?, ?)", (1234, "raw-route")
            )
            await conn.commit()
            assert cur.lastrowid > 0
        async with aiosqlite.connect(tmp_db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM t WHERE v=1234")
            n = (await cur.fetchone())[0]
        assert n == 1, "raw .execute() write should have landed"
        assert coord.stats()["writes_total"] >= 1, "coordinator didn't serve the write"
    finally:
        await stop_all()


@pytest.mark.asyncio
async def test_aiosqlite_routing_passes_reads_through(tmp_db):
    """Reads must NOT be routed (would deadlock the queue and lose result rows)."""
    from tools.db_writer import install_aiosqlite_routing
    install_aiosqlite_routing()
    await get_writer(tmp_db)
    try:
        async with aiosqlite.connect(tmp_db) as conn:
            await conn.execute("INSERT INTO t (v, who) VALUES (?, ?)", (5151, "seed"))
            await conn.commit()
        async with aiosqlite.connect(tmp_db) as conn:
            cur = await conn.execute("SELECT v, who FROM t WHERE v = ?", (5151,))
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 5151 and row[1] == "seed"
    finally:
        await stop_all()


@pytest.mark.asyncio
async def test_concurrent_raw_writers_no_lock_errors_after_install(tmp_db):
    """100 concurrent producers using raw .execute() must succeed cleanly
    once routing is installed. This is the production scenario."""
    from tools.db_writer import install_aiosqlite_routing
    install_aiosqlite_routing()
    coord = await get_writer(tmp_db)
    try:
        async def raw_producer(n):
            async with aiosqlite.connect(tmp_db) as conn:
                for i in range(20):
                    await conn.execute(
                        "INSERT INTO t (v, who) VALUES (?, ?)",
                        (i, f"raw-p{n}-{i}"),
                    )
                    await conn.commit()
        await asyncio.gather(*[raw_producer(p) for p in range(100)])
        async with aiosqlite.connect(tmp_db) as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM t WHERE who LIKE 'raw-%'")
            n = (await cur.fetchone())[0]
        assert n == 2000, f"Expected 2000 rows, got {n}"
        assert coord.stats()["writes_failed"] == 0, (
            f"{coord.stats()['writes_failed']} failed writes — routing is broken"
        )
    finally:
        await stop_all()

@pytest.mark.asyncio
async def test_forced_shutdown_settles_queued_callers(tmp_db):
    """Regression: if stop() times out while one _apply() is blocked, queued
    callers must not hang forever. After stop() returns, every caller task
    submitted before/during shutdown has reached a terminal state.
    """
    coord = WriteCoordinator(tmp_db)
    await coord.start()

    gate = asyncio.Event()
    first_started = asyncio.Event()
    release = {"on": False}

    # Patch _apply so the FIRST write blocks until we allow it through,
    # simulating a stuck writer (e.g. cross-process lock). Later writes
    # queue behind it and are never applied once we force shutdown.
    original_apply = coord._apply

    async def slow_apply(op_type, sql, payload):
        if not release["on"]:
            first_started.set()
            await gate.wait()
        return await original_apply(op_type, sql, payload)

    coord._apply = slow_apply

    results = {}

    async def caller(name, coro):
        try:
            results[name] = ("ok", await coro)
        except asyncio.CancelledError:
            results[name] = ("cancelled", None)
        except Exception as e:
            results[name] = ("error", e)

    t1 = asyncio.create_task(caller("first", coord.execute("INSERT INTO t (v, who) VALUES (?, ?)", (1, "blocked"))))
    await asyncio.wait_for(first_started.wait(), timeout=5)

    # This one lands in the queue behind the blocked write.
    t2 = asyncio.create_task(caller("second", coord.execute("INSERT INTO t (v, who) VALUES (?, ?)", (2, "queued"))))
    t3 = asyncio.create_task(caller("third", coord.transaction([
        ("INSERT INTO t (v, who) VALUES (?, ?)", (3, "tx")),
    ])))
    await asyncio.sleep(0.05)  # let them enqueue deterministically
    assert coord._queue.qsize() >= 2, "queued ops should be pending behind blocked write"

    # Force a short shutdown timeout while the first write is still blocked.
    await coord.stop(drain_timeout_s=0.1)

    # Every caller must reach a terminal state promptly.
    done, pending = await asyncio.wait({t1, t2, t3}, timeout=2.0)
    assert not pending, f"callers hung after stop(): {pending}"

    outcomes = list(results.values())
    kinds = {k for k, _ in outcomes}
    assert kinds <= {"ok", "cancelled", "error"}, f"unexpected outcome: {results}"
    assert any(k == "error" for k in kinds), (
        "queued-but-unapplied ops should fail with an explicit shutdown error, "
        f"got {results}"
    )
    for k, v in outcomes:
        if k == "error":
            assert isinstance(v, RuntimeError) and "shut down" in str(v)

    # No unresolved futures left anywhere in lifecycle state.
    assert coord._task is None, "drain task reference should be cleared"
    assert coord._queue is None, "queue reference should be cleared"
    assert not coord._running

    # A clean restart works afterwards.
    await coord.start()
    try:
        rid = await coord.execute("INSERT INTO t (v, who) VALUES (?, ?)", (9, "post-restart"))
        assert rid > 0
    finally:
        coord._apply = original_apply
        await coord.stop()


@pytest.mark.asyncio
async def test_graceful_drain_still_applies_all_writes(tmp_db):
    """Normal graceful stop (no forced timeout) must still flush the queue."""
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        tasks = [
            asyncio.create_task(coord.execute(
                "INSERT INTO t (v, who) VALUES (?, ?)", (i, "graceful")
            ))
            for i in range(50)
        ]
        await coord.stop(drain_timeout_s=10.0)
        rids = await asyncio.gather(*tasks)
        assert all(r > 0 for r in rids)
        async with aiosqlite.connect(tmp_db) as db:
            cur = await db.execute("SELECT COUNT(*) FROM t WHERE who='graceful'")
            n = (await cur.fetchone())[0]
        assert n == 50
    finally:
        pass  # already stopped
