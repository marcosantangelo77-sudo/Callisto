"""Tests for the @tracked_ingestion decorator.

Covers the three terminal paths a wrapped function can take:
  1. Returns normally with rows → status='ok'
  2. Raises → status='failed', error captured
  3. Returns a rate-limit sentinel → status='rate_limited'

The decorator MUST never raise on its own — a tracker failure must not break
the wrapped function. Each test creates a throwaway SQLite DB via
tempfile and points CALLISTO_DB_PATH at it before importing the module.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest


# Force the tracking module to pick up a clean DB path per test run. We set
# it BEFORE import so the module-level DB_PATH constant binds correctly.
@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    # Reload the module so DB_PATH module constant re-reads env
    if "tools.ingestion_tracking" in sys.modules:
        del sys.modules["tools.ingestion_tracking"]
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _init_schema(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                status TEXT NOT NULL,
                rows_ingested INTEGER DEFAULT 0,
                error_class TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                extra_json TEXT
            )
            """
        )
        await db.commit()


async def _read_rows(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM ingestion_runs ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


@pytest.mark.asyncio
async def test_ok_path_writes_row(tmp_db):
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.ok")
    async def fetch() -> dict:
        return {"games": 7, "sport": "nba"}

    result = await fetch()
    assert result == {"games": 7, "sport": "nba"}

    rows = await _read_rows(tmp_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "unit.test.ok"
    assert row["status"] == "ok"
    assert row["rows_ingested"] == 7
    assert row["error_class"] is None
    assert row["error_message"] is None
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_raises_path_writes_failed(tmp_db):
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.raises")
    async def fetch() -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await fetch()

    rows = await _read_rows(tmp_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["error_class"] == "RuntimeError"
    assert "boom" in (row["error_message"] or "")
    assert row["rows_ingested"] == 0


@pytest.mark.asyncio
async def test_rate_limit_sentinel(tmp_db):
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.ratelimit")
    async def fetch() -> dict:
        return {"games": 0, "error": "rate limit hit (100/hr)"}

    result = await fetch()
    assert "error" in result

    rows = await _read_rows(tmp_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "rate_limited"
    assert row["rows_ingested"] == 0
    assert "rate limit" in (row["error_message"] or "").lower()


@pytest.mark.asyncio
async def test_error_payload_marked_failed(tmp_db):
    """When a function returns {"error": "..."} (not rate-limit), it's failed."""
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.error_payload")
    async def fetch() -> dict:
        return {"error": "HTTP 500: upstream exploded"}

    result = await fetch()
    assert result["error"].startswith("HTTP 500")
    rows = await _read_rows(tmp_db)
    assert rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_dynamic_source_callable(tmp_db):
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source=lambda sport, **_: f"unit.test.{sport}")
    async def fetch(sport: str) -> dict:
        return {"games": 1}

    await fetch("mlb")
    await fetch("nba")

    rows = await _read_rows(tmp_db)
    assert sorted(r["source"] for r in rows) == ["unit.test.mlb", "unit.test.nba"]


@pytest.mark.asyncio
async def test_decorator_resilient_when_db_missing(tmp_db, monkeypatch):
    """Wrapped function must STILL run + return normally even if tracker DB
    is unreachable. This is the core reliability guarantee."""
    # Point CALLISTO_DB_PATH at a read-only-impossible location
    monkeypatch.setenv("CALLISTO_DB_PATH", "/nonexistent/path/definitely/does/not/exist.db")
    if "tools.ingestion_tracking" in sys.modules:
        del sys.modules["tools.ingestion_tracking"]
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.db_missing")
    async def fetch() -> dict:
        return {"games": 42}

    # Must NOT raise — tracking failure swallowed
    result = await fetch()
    assert result == {"games": 42}


@pytest.mark.asyncio
async def test_rows_extraction_from_list(tmp_db):
    await _init_schema(tmp_db)
    from tools.ingestion_tracking import tracked_ingestion

    @tracked_ingestion(source="unit.test.list_return")
    async def fetch() -> list:
        return [{"x": 1}, {"x": 2}, {"x": 3}]

    await fetch()
    rows = await _read_rows(tmp_db)
    assert rows[0]["rows_ingested"] == 3
    assert rows[0]["status"] == "ok"
