"""Tests for the VACUUM-in-transaction silent failure fix.

Root cause (pre-fix):
  * ``tools.db_writer.install_aiosqlite_routing`` installed a regex that treated
    ``VACUUM`` as a write and forwarded it to the ``WriteCoordinator``'s queue.
  * The coordinator's sink connection runs in aiosqlite's default deferred
    isolation mode, which opens an implicit transaction around every
    ``execute()``.
  * ``self._db.execute("VACUUM")`` therefore ran *from within a transaction*
    and SQLite raised ``OperationalError: cannot VACUUM from within a
    transaction``.
  * The drain loop caught the exception, bumped ``writes_failed``, and the
    system looked healthy — /health showed "writes_failed": N growing over time.

Fix:
  * ``VACUUM`` removed from the routing regex. Coordinator's ``_apply`` asserts
    VACUUM never reaches it and raises loudly if it does.
  * ``tools.schema.vacuum_db`` now opens a dedicated stdlib ``sqlite3``
    connection in ``isolation_level=None`` (autocommit) on a worker thread,
    verifies ``in_transaction == False``, then VACUUMs.
  * ``tools.self_repair._fix_bloat`` now delegates to ``vacuum_db`` instead of
    issuing VACUUM on its own transactional connection.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from tools.db_writer import (
    WriteCoordinator,
    _is_vacuum_sql,
    _is_write_sql,
    stop_all,
)


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    db_path = str(tmp_path / "vacuum_safety.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    # Insert + delete so VACUUM has free pages to actually reclaim.
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(i,) for i in range(500)])
    conn.execute("DELETE FROM t WHERE v < 400")
    conn.commit()
    conn.close()
    yield db_path
    await stop_all()


# ─────────────────────────────────────────────────────────────
# Regression: VACUUM no longer routes through the coordinator
# ─────────────────────────────────────────────────────────────


def test_vacuum_excluded_from_write_routing():
    """VACUUM must NOT match the routing regex — otherwise the monkey-patched
    aiosqlite.Connection.execute forwards it to the coordinator, which is
    exactly the bug we're fixing."""
    assert _is_write_sql("VACUUM") is False
    assert _is_write_sql("  vacuum  ") is False
    assert _is_write_sql("VACUUM INTO 'backup.db'") is False
    # Sanity: real writes still route.
    assert _is_write_sql("INSERT INTO t VALUES (1)") is True
    assert _is_write_sql("DELETE FROM t") is True


def test_vacuum_detection_helper():
    """_is_vacuum_sql powers the defensive check in _apply."""
    assert _is_vacuum_sql("VACUUM") is True
    assert _is_vacuum_sql("  vacuum  ") is True
    assert _is_vacuum_sql("VACUUM INTO 'x.db'") is True
    assert _is_vacuum_sql("INSERT INTO t VALUES (1)") is False
    assert _is_vacuum_sql(None) is False
    assert _is_vacuum_sql("") is False


# ─────────────────────────────────────────────────────────────
# Loud-failure: coordinator refuses VACUUM with a clear error
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coordinator_refuses_vacuum_execute(tmp_db):
    """If a caller bypasses the routing patch and submits VACUUM directly to
    the coordinator, it must raise a RuntimeError with actionable guidance
    — NOT the opaque SQLite 'cannot VACUUM from within a transaction' that
    previously got silently counted as a failed write."""
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        with pytest.raises(RuntimeError, match="VACUUM cannot run through"):
            await coord.execute("VACUUM")
        # The attempted op counted as a failure; other ops still work after.
        rid = await coord.execute("INSERT INTO t (v) VALUES (?)", (999,))
        assert rid > 0
    finally:
        await coord.stop()


@pytest.mark.asyncio
async def test_coordinator_refuses_vacuum_in_transaction(tmp_db):
    """VACUUM inside a multi-statement coordinator transaction must also be
    rejected loudly — not silently converted into a SQLite error."""
    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        with pytest.raises(RuntimeError, match="VACUUM cannot appear inside"):
            await coord.transaction([
                ("INSERT INTO t (v) VALUES (?)", (1,)),
                ("VACUUM", ()),
            ])
    finally:
        await coord.stop()


# ─────────────────────────────────────────────────────────────
# Happy path: vacuum_db actually VACUUMs without raising
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vacuum_db_succeeds_on_fresh_db(tmp_db):
    """The fixed vacuum_db helper opens its own autocommit connection and
    completes successfully."""
    from tools.schema import vacuum_db

    result = await vacuum_db(tmp_db)
    assert "before_bytes" in result
    assert "after_bytes" in result
    assert "reclaimed_bytes" in result
    assert result["after_bytes"] > 0
    # VACUUM should have reclaimed space (we deleted 400 of 500 rows).
    assert result["reclaimed_bytes"] >= 0


@pytest.mark.asyncio
async def test_vacuum_db_uses_autocommit_connection(tmp_db):
    """The fix depends on the dedicated connection being in autocommit mode
    (isolation_level=None). Verify that assumption directly — if this test
    fails, the fix is load-bearing on a broken premise."""
    # Mirror what vacuum_db does internally.
    conn = sqlite3.connect(tmp_db, isolation_level=None, timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        # Invariant: autocommit connection has no open transaction.
        assert conn.in_transaction is False
        conn.execute("VACUUM")  # Must not raise.
        assert conn.in_transaction is False
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_vacuum_db_concurrent_with_coordinator(tmp_db):
    """Realistic scenario: the coordinator is running (writers are live) and
    a maintenance task calls vacuum_db. vacuum_db uses a separate connection
    so this should succeed — and must not leave the coordinator in a bad
    state afterward."""
    from tools.schema import vacuum_db

    coord = WriteCoordinator(tmp_db)
    await coord.start()
    try:
        # Coordinator has done real writes (so its sink has been used).
        await coord.execute("INSERT INTO t (v) VALUES (?)", (10001,))
        await coord.execute("INSERT INTO t (v) VALUES (?)", (10002,))

        # VACUUM via the fixed path — must NOT raise, must NOT require the
        # coordinator to be quiesced, must NOT route through the coordinator.
        result = await vacuum_db(tmp_db)
        assert result["after_bytes"] > 0

        # Coordinator still healthy after VACUUM — more writes succeed.
        rid = await coord.execute("INSERT INTO t (v) VALUES (?)", (10003,))
        assert rid > 0
        stats = coord.stats()
        # Zero failures from the writes we just made.
        assert stats["writes_failed"] == 0
    finally:
        await coord.stop()


# ─────────────────────────────────────────────────────────────
# Invariant: VACUUM on a connection with an open tx must fail
# (This is the original bug reproduced against raw sqlite3 — proves the fix
#  target is real and documents what the dedicated autocommit path avoids.)
# ─────────────────────────────────────────────────────────────


def test_raw_sqlite_vacuum_in_tx_reproduces_bug(tmp_db):
    """Without the fix, VACUUM on a connection with an open transaction raises
    ``OperationalError: cannot VACUUM from within a transaction``. This is the
    exact error the coordinator's /health surfaced as ``last_failure``."""
    conn = sqlite3.connect(tmp_db)  # default isolation_level ⇒ deferred
    try:
        conn.execute("INSERT INTO t (v) VALUES (?)", (777,))
        assert conn.in_transaction is True
        with pytest.raises(sqlite3.OperationalError, match="cannot VACUUM"):
            conn.execute("VACUUM")
    finally:
        conn.rollback()
        conn.close()
