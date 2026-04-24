"""Tests for tools.db_utils retry wrappers, connection pragma application,
WAL growth detection, and scripts.db_doctor.

These are the guardrails against the silent-failure mode that kept biting
Callisto in March-April: dangling transactions, missing pragmas on raw
aiosqlite connections, and unattended WAL bloat.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import db_utils
from tools.db_utils import (
    busy_timeout_stats,
    commit_with_retry,
    execute_with_retry,
    record_busy_timeout,
    reset_busy_timeout_counter,
    safe_execute_commit,
)
from tools.schema import open_db


@pytest_asyncio.fixture
async def tmp_db(tmp_path):
    db_path = str(tmp_path / "test_health.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    yield db_path


@pytest.mark.asyncio
async def test_open_db_applies_canonical_pragmas(tmp_db):
    """open_db must apply WAL, busy_timeout=120000, synchronous=NORMAL, foreign_keys=ON."""
    db = await open_db(tmp_db)
    try:
        jm = await (await db.execute("PRAGMA journal_mode")).fetchone()
        bt = await (await db.execute("PRAGMA busy_timeout")).fetchone()
        sy = await (await db.execute("PRAGMA synchronous")).fetchone()
        fk = await (await db.execute("PRAGMA foreign_keys")).fetchone()
        assert (jm[0] or "").lower() == "wal"
        assert int(bt[0]) == 120000
        assert int(sy[0]) == 1
        assert int(fk[0]) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_open_db_tags_connection_for_coordinator_routing(tmp_db):
    db = await open_db(tmp_db)
    try:
        assert getattr(db, "_callisto_db_path", None) == os.path.abspath(tmp_db)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_open_db_honours_disable_fk(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_DISABLE_FK", "1")
    db = await open_db(tmp_db)
    try:
        fk = await (await db.execute("PRAGMA foreign_keys")).fetchone()
        assert int(fk[0]) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_execute_with_retry_records_busy_on_simulated_lock(tmp_db):
    """execute_with_retry must bump the busy-timeout counter each retry."""
    reset_busy_timeout_counter()
    db = await aiosqlite.connect(tmp_db)

    calls = {"n": 0}
    orig_execute = db.execute

    async def flaky_execute(sql, params=()):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return await orig_execute(sql, params)

    db.execute = flaky_execute  # type: ignore[assignment]
    try:
        cursor = await execute_with_retry(
            db, "INSERT INTO t (v) VALUES (?)", (7,),
            max_retries=5, operation="test_flaky",
        )
        assert cursor is not None
    finally:
        db.execute = orig_execute  # type: ignore[assignment]
        await db.close()

    stats = busy_timeout_stats(3600.0)
    assert stats["hits_in_window"] >= 2
    assert stats["hits_last_5m"] >= 2


@pytest.mark.asyncio
async def test_record_busy_timeout_trims_to_window():
    """Events outside the window must not be counted."""
    reset_busy_timeout_counter()
    old = time.time() - 10_000
    db_utils._busy_timeout_events.append(old)
    record_busy_timeout("fresh")
    stats = busy_timeout_stats(3600.0)
    assert stats["hits_in_window"] == 1


@pytest.mark.asyncio
async def test_safe_execute_commit_rolls_back_on_commit_failure(tmp_db):
    """If commit raises, pending writes must be rolled back."""
    db = await aiosqlite.connect(tmp_db)
    try:
        orig_commit = db.commit

        async def bad_commit(*a, **k):
            raise sqlite3.OperationalError("synthetic commit failure")

        db.commit = bad_commit  # type: ignore[assignment]
        rolled_back = {"n": 0}
        orig_rb = db.rollback

        async def counting_rollback(*a, **k):
            rolled_back["n"] += 1
            return await orig_rb(*a, **k)

        db.rollback = counting_rollback  # type: ignore[assignment]
        with pytest.raises(Exception):
            await safe_execute_commit(
                db, "INSERT INTO t (v) VALUES (?)", (42,),
                max_retries=1, operation="test_safe_execute_commit",
            )
        assert rolled_back["n"] >= 1
        db.commit = orig_commit  # type: ignore[assignment]
        db.rollback = orig_rb  # type: ignore[assignment]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_safe_execute_commit_happy_path_persists(tmp_db):
    db = await aiosqlite.connect(tmp_db)
    try:
        await safe_execute_commit(
            db, "INSERT INTO t (v) VALUES (?)", (99,),
            operation="test_happy",
        )
    finally:
        await db.close()

    conn = sqlite3.connect(tmp_db)
    try:
        (n,) = conn.execute("SELECT COUNT(*) FROM t WHERE v = 99").fetchone()
        assert n == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_commit_with_retry_records_busy_timeout_hits(tmp_db):
    reset_busy_timeout_counter()
    db = await aiosqlite.connect(tmp_db)
    calls = {"n": 0}
    orig_commit = db.commit

    async def flaky_commit(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise sqlite3.OperationalError("database is locked")
        return await orig_commit(*a, **k)

    db.commit = flaky_commit  # type: ignore[assignment]
    try:
        await commit_with_retry(db, max_retries=5, operation="test_commit_flaky")
    finally:
        db.commit = orig_commit  # type: ignore[assignment]
        await db.close()

    stats = busy_timeout_stats(3600.0)
    assert stats["hits_in_window"] >= 1


def test_wal_growth_detectable_via_pragma(tmp_db):
    """Confirm page_count + wal_checkpoint give us metrics the health loop uses."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.commit()

    reader = sqlite3.connect(tmp_db)
    reader.execute("BEGIN").fetchone()
    reader.execute("SELECT COUNT(*) FROM t").fetchone()

    try:
        for i in range(500):
            conn.execute("INSERT INTO t (v) VALUES (?)", (i,))
        conn.commit()

        row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        busy, log_pages, checkpointed = row
        assert log_pages > 0

        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        assert page_size > 0
        assert page_count > 0

        reader.commit()

        row2 = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        t_busy, t_log, t_ckpt = row2
        assert t_busy == 0
    finally:
        reader.close()
        conn.close()


@pytest.mark.asyncio
async def test_db_doctor_integrity_and_fk_and_stats(tmp_db):
    """scripts.db_doctor should report clean integrity + fk + stats on a fresh DB."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("db_doctor", REPO_ROOT / "scripts" / "db_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    assert mod.integrity_check(tmp_db)["ok"] is True
    assert mod.fk_check(tmp_db)["ok"] is True
    trunc = mod.wal_truncate(tmp_db)
    assert trunc["ok"] is True
    s = mod.stats(tmp_db)
    assert s["page_count"] > 0
    assert s["page_size"] in (4096, 8192)
    assert s["journal_mode"].lower() == "wal"


@pytest.mark.asyncio
async def test_wal_health_state_shape():
    """The api._wal_health_state dict must expose the keys /admin/db/health reads."""
    import api
    required = {
        "last_checkpoint_ts",
        "last_checkpoint_duration_s",
        "last_wal_pages_before",
        "last_wal_pages_after",
        "last_wal_mb_before",
        "last_wal_mb_after",
        "checkpoints_total",
        "truncates_total",
        "checkpoint_errors_total",
    }
    assert required.issubset(api._wal_health_state.keys())
    assert callable(api.wal_maintenance_loop)
    assert api.wal_checkpoint_loop is api.wal_maintenance_loop


def test_state_paths_db_path_defaults_and_override(monkeypatch, tmp_path):
    from tools import state_paths
    monkeypatch.delenv("CALLISTO_DB_PATH", raising=False)
    assert state_paths.db_path() == os.path.join("memory", "callisto.db")
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "x.db"))
    assert state_paths.db_path() == str(tmp_path / "x.db")
