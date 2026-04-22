"""Tests for tools/health.py::_check_data_collector.

Validates the missing health probe that was declared in SUBSYSTEMS but never
implemented — meaning the breaker literally could not trip on data-collector
silence. These tests lock down the SLA logic so future refactors don't
regress back to the silent-failure regime.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    # Force re-import so the module constants bind to this path
    for mod in ("tools.health", "tools.ingestion_tracking"):
        if mod in sys.modules:
            del sys.modules[mod]
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _create_table(db_path: str) -> None:
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


async def _insert_run(
    db_path: str,
    source: str,
    status: str,
    minutes_ago: int,
) -> None:
    finished = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    started = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago + 1)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO ingestion_runs "
            "(source, started_at, finished_at, status, rows_ingested) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, started, finished, status, 10),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_no_table_returns_warning(tmp_db):
    """Fresh DB, no ingestion_runs table → warning (not critical)."""
    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    # Table doesn't exist → still a warning because schema pending
    assert result["status"] in ("warning", "ok")


@pytest.mark.asyncio
async def test_empty_table_returns_warning(tmp_db):
    """Table exists but no rows → warning (no data to evaluate)."""
    await _create_table(tmp_db)
    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    assert result["status"] == "warning"
    assert result.get("sources", 1) == 0


@pytest.mark.asyncio
async def test_all_fresh_returns_ok(tmp_db):
    await _create_table(tmp_db)
    # Insert a handful of fresh successful runs
    await _insert_run(tmp_db, "espn.scoreboard.baseball_mlb", "ok", minutes_ago=1)
    await _insert_run(tmp_db, "odds_api_io.v3.odds.updated", "ok", minutes_ago=2)

    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    assert result["status"] == "ok", result
    assert result["healthy"] == 2
    assert not result["stale_critical"]


@pytest.mark.asyncio
async def test_one_source_past_sla_warns(tmp_db):
    await _create_table(tmp_db)
    # SLA for espn.scoreboard.baseball_mlb is 900s (15 min). 30 min = warn.
    await _insert_run(tmp_db, "espn.scoreboard.baseball_mlb", "ok", minutes_ago=30)

    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    assert result["status"] == "warning"
    assert len(result["stale_warn"]) == 1


@pytest.mark.asyncio
async def test_one_source_past_3x_sla_critical(tmp_db):
    await _create_table(tmp_db)
    # SLA = 900s; 3x = 2700s = 45 min. 60 min → critical.
    await _insert_run(tmp_db, "espn.scoreboard.baseball_mlb", "ok", minutes_ago=60)

    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    assert result["status"] == "critical", result
    assert len(result["stale_critical"]) == 1
    assert result["error"] is not None


@pytest.mark.asyncio
async def test_rate_limited_surfaces_as_warning(tmp_db):
    await _create_table(tmp_db)
    await _insert_run(tmp_db, "odds_api_io.v3.odds.updated", "rate_limited", minutes_ago=1)

    from tools.health import SystemHealth

    sh = SystemHealth()
    result = await sh._check_data_collector()
    assert result["status"] == "warning"
    assert len(result["rate_limited"]) == 1


@pytest.mark.asyncio
async def test_breaker_trips_via_check_all(tmp_db, monkeypatch):
    """Wire through check_all + the breaker: a critical data_collector result
    must cause the data_collector circuit breaker to record a failure."""
    await _create_table(tmp_db)
    await _insert_run(tmp_db, "espn.scoreboard.baseball_mlb", "ok", minutes_ago=60)

    # Avoid needing real Ollama / ESPN for the other checks — stub them.
    from tools.health import SystemHealth

    sh = SystemHealth()
    sh._check_ollama = lambda: _async_ok()
    sh._check_sqlite = lambda: _async_ok()
    sh._check_disk = lambda: _async_ok()
    sh._check_memory = lambda: _async_ok()
    sh._check_network = lambda: _async_ok()

    results = await sh.check_all()
    assert results["data_collector"]["status"] == "critical"

    dc_breaker = sh.get_breaker("data_collector")
    assert dc_breaker is not None
    assert dc_breaker.consecutive_failures == 1


async def _async_ok() -> dict:
    return {"status": "ok"}


@pytest.mark.asyncio
async def test_venue_nameerror_fix():
    """Regression guard: tools/contextual_data.py:391 previously referenced
    `venue` (undefined — parameter is `venue_name`), which would raise a
    NameError inside the except handler and mask the real upstream error.
    """
    import inspect
    from tools import contextual_data

    src = inspect.getsource(contextual_data.get_weather)
    # The buggy form was `venue={venue}` — ensure it's been replaced.
    assert "venue={venue}" not in src, (
        "venue NameError bug regressed — use venue_name in the except branch"
    )
    assert "venue_name" in src
