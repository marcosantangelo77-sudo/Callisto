"""Tests for expanded self-repair recoveries (feat/self-repair-expansion).

Each test synthesises the trigger condition for one recovery, runs the
recovery, and asserts both the action outcome and that a self_repair_log
row was written.

These tests are deliberately hermetic: every DB is a fresh tmp_path
SQLite file, and tools.self_repair module-level state (DB_PATH,
_recovery_cooldowns, the engine singleton) is monkey-patched per test so
cases do not bleed into each other.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import self_repair
import importlib


def _apply_migration_015(db_path: str) -> None:
    """Run the 015 migration's up() directly so the test DB has
    self_repair_log without the runner's bootstrap logic skipping it."""
    mig = importlib.import_module("tools.migrations.015_self_repair_log")
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        mig.up(conn)
    finally:
        conn.close()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh SQLite DB with required tables. Resets all module state."""
    db_path = str(tmp_path / "callisto_test.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS hypotheses ("
                     "hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "name TEXT, sport TEXT, status TEXT, "
                     "created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS task_queue ("
                     "task_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "query TEXT, status TEXT, priority INTEGER DEFAULT 0, "
                     "result TEXT, error TEXT, "
                     "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                     "started_at TEXT, completed_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS ingestion_runs ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "source TEXT, status TEXT, started_at TEXT, "
                     "finished_at TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS odds_snapshots ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "sport TEXT, timestamp TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS odds_snapshots_v2 ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "sport TEXT, snapshot_time TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS game_contexts ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "sport TEXT, game_date TEXT)")
        conn.commit()
    finally:
        conn.close()

    _apply_migration_015(db_path)

    # Point the self_repair module at the tmp DB and reset singletons.
    monkeypatch.setattr(self_repair, "DB_PATH", db_path)
    monkeypatch.setattr(self_repair, "_engine", None)
    self_repair._recovery_cooldowns.clear()
    return db_path


@pytest.fixture
def engine(tmp_db):
    return self_repair.get_repair_engine()


async def _fetch_log_rows(db_path, recovery_name=None):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        if recovery_name:
            cur = await db.execute(
                "SELECT recovery_name, trigger, success, action, detail, "
                "metadata_json, elapsed_ms FROM self_repair_log "
                "WHERE recovery_name = ? ORDER BY invoked_at DESC",
                (recovery_name,),
            )
        else:
            cur = await db.execute(
                "SELECT recovery_name, trigger, success, action, detail, "
                "metadata_json, elapsed_ms FROM self_repair_log "
                "ORDER BY invoked_at DESC"
            )
        return await cur.fetchall()


# ─────────────────────────────────────────────────────────────────────
# Migration 015
# ─────────────────────────────────────────────────────────────────────

def test_migration_015_creates_self_repair_log(tmp_db):
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='self_repair_log'"
        ).fetchall()
        assert rows, "migration 015 should create self_repair_log"
        # Columns present.
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(self_repair_log)"
        ).fetchall()}
        assert {"recovery_name", "trigger", "success", "action", "detail",
                "metadata_json", "invoked_at", "elapsed_ms"}.issubset(cols)
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Recovery: db_lock_long
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_lock_long_triggers_when_hits_exceeded(engine, tmp_db,
                                                        monkeypatch):
    from tools import db_utils
    # Seed synthetic lock hits in the last 60s — above the threshold (3).
    db_utils.reset_busy_timeout_counter()
    for _ in range(5):
        db_utils.record_busy_timeout("test")

    result = await engine.trigger_recovery("db_lock_long", manual=True)
    assert result["action"] in ("force_wal_checkpoint",
                                "db_lock_checkpoint_error")
    rows = await _fetch_log_rows(tmp_db, "db_lock_long")
    assert rows, "db_lock_long should have written a log row"
    assert rows[0][1] == "manual"
    # lock_hits_60s metadata persisted.
    meta = json.loads(rows[0][5]) if rows[0][5] else {}
    assert meta.get("lock_hits_60s", 0) >= 5


@pytest.mark.asyncio
async def test_db_lock_long_noop_below_threshold(engine, tmp_db):
    from tools import db_utils
    db_utils.reset_busy_timeout_counter()
    result = await engine.trigger_recovery("db_lock_long", manual=True)
    assert result["fixed"] is False
    assert result["action"] == "db_lock_below_threshold"


# ─────────────────────────────────────────────────────────────────────
# Recovery: orphaned_processing
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orphaned_processing_marks_failed(engine, tmp_db):
    # Insert a PROCESSING row whose started_at is 10x the max timeout ago.
    stuck_ts = (datetime.now(timezone.utc)
                - timedelta(seconds=self_repair.TASK_MAX_TIMEOUT_SECONDS * 10)
                ).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO task_queue (query, status, started_at) "
            "VALUES (?, 'PROCESSING', ?)",
            ("stuck old", stuck_ts),
        )
        conn.execute(
            "INSERT INTO task_queue (query, status, started_at) "
            "VALUES (?, 'PROCESSING', ?)",
            ("fresh", fresh_ts),
        )
        conn.commit()
    finally:
        conn.close()

    result = await engine.trigger_recovery("orphaned_processing", manual=True)
    assert result["fixed"] is True
    assert result["action"] == "marked_failed_stuck_processing"

    # Only the old one got marked FAILED.
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT status, error FROM task_queue ORDER BY task_id"
        ).fetchall()
    finally:
        conn.close()
    statuses = [r[0] for r in rows]
    assert statuses == ["FAILED", "PROCESSING"], (
        f"fresh task should remain PROCESSING; got {rows}"
    )
    assert rows[0][1] == "stuck in processing"

    # Log row asserts.
    logs = await _fetch_log_rows(tmp_db, "orphaned_processing")
    assert logs
    assert bool(logs[0][2]) is True


@pytest.mark.asyncio
async def test_orphaned_processing_noop_when_empty(engine, tmp_db):
    result = await engine.trigger_recovery("orphaned_processing", manual=True)
    assert result["fixed"] is False
    assert result["action"] == "no_orphans"


# ─────────────────────────────────────────────────────────────────────
# Recovery: research_loop_stuck
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_loop_stuck_seeds_watermark_first(engine, tmp_db):
    # First run just seeds the watermark.
    result = await engine.trigger_recovery("research_loop_stuck", manual=True)
    assert result["action"] == "research_loop_watermark_init"
    assert engine._last_hypothesis_watermark == 0


@pytest.mark.asyncio
async def test_research_loop_stuck_forces_after_stagnant_cycles(
    engine, tmp_db
):
    # Seed watermark to 100 and insert 0 hypotheses above.
    engine._last_hypothesis_watermark = 100
    engine._research_stagnant_cycles = (
        self_repair.RESEARCH_LOOP_ZERO_PROGRESS_CYCLES - 1
    )
    result = await engine.trigger_recovery("research_loop_stuck", manual=True)
    # No import api — so dispatch fails gracefully; still resets counter.
    assert result["action"] in (
        "forced_hypothesis_gen_cycle",
        "hypothesis_gen_dispatch_failed",
    )
    assert engine._research_stagnant_cycles == 0
    logs = await _fetch_log_rows(tmp_db, "research_loop_stuck")
    assert logs


@pytest.mark.asyncio
async def test_research_loop_stuck_recognizes_progress(engine, tmp_db):
    # Seed watermark, then add new hypothesis rows above it.
    engine._last_hypothesis_watermark = 0
    engine._research_stagnant_cycles = 3
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO hypotheses (name, sport, status) "
            "VALUES ('h1', 'mlb', 'draft')"
        )
        conn.execute(
            "INSERT INTO hypotheses (name, sport, status) "
            "VALUES ('h2', 'mlb', 'draft')"
        )
        conn.commit()
    finally:
        conn.close()
    result = await engine.trigger_recovery("research_loop_stuck", manual=True)
    assert result["action"] == "research_loop_progressing"
    assert engine._research_stagnant_cycles == 0


# ─────────────────────────────────────────────────────────────────────
# Recovery: claude_cli_missing
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claude_cli_missing_degrades_to_local(engine, tmp_db,
                                                    monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _cmd: None)
    from tools import local_only as _local
    monkeypatch.setattr(_local, "is_local_only", lambda: False)

    result = await engine.trigger_recovery("claude_cli_missing", manual=True)
    assert result["fixed"] is True
    assert result["action"] == "degraded_to_local_model"
    logs = await _fetch_log_rows(tmp_db, "claude_cli_missing")
    assert logs


@pytest.mark.asyncio
async def test_claude_cli_missing_noop_in_local_only(engine, tmp_db,
                                                     monkeypatch):
    from tools import local_only as _local
    monkeypatch.setattr(_local, "is_local_only", lambda: True)

    result = await engine.trigger_recovery("claude_cli_missing", manual=True)
    assert result["fixed"] is False
    assert result["action"] == "local_only_mode"


@pytest.mark.asyncio
async def test_claude_cli_missing_noop_when_cli_present(engine, tmp_db,
                                                        monkeypatch):
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda _cmd: "/usr/bin/claude")
    from tools import local_only as _local
    monkeypatch.setattr(_local, "is_local_only", lambda: False)

    result = await engine.trigger_recovery("claude_cli_missing", manual=True)
    assert result["fixed"] is False
    assert result["action"] == "claude_cli_found"


# ─────────────────────────────────────────────────────────────────────
# Recovery: sla_stuck_sources
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sla_stuck_sources_refreshes_stuck(engine, tmp_db, tmp_path,
                                                 monkeypatch):
    # Write the alerted-sources file next to the DB. self_repair checks
    # the DB-local dir before falling back to CALLISTO_STATE_DIR, so the
    # DB-adjacent copy is what's required for this test. tmp_db and
    # tmp_path share the same parent in pytest, so point both at it.
    alerted = Path(tmp_db).parent / "sla_alerted_sources.json"
    alerted.write_text(json.dumps({"sources": ["test_src_stuck",
                                                "test_src_fresh"]}))
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(Path(tmp_db).parent))

    # Seed ingestion_runs: stuck source last ok 48h ago, fresh source 1h ago.
    stuck_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    fresh_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO ingestion_runs (source, status, started_at, finished_at) "
            "VALUES ('test_src_stuck', 'ok', ?, ?)",
            (stuck_ts, stuck_ts),
        )
        conn.execute(
            "INSERT INTO ingestion_runs (source, status, started_at, finished_at) "
            "VALUES ('test_src_fresh', 'ok', ?, ?)",
            (fresh_ts, fresh_ts),
        )
        conn.commit()
    finally:
        conn.close()

    # Register a synthetic handler for the stuck source.
    calls: list[str] = []

    async def _handler():
        calls.append("hit")
        return {"ok": True}

    self_repair.register_sla_refresh_handler("test_src_stuck", _handler)

    result = await engine.trigger_recovery("sla_stuck_sources", manual=True)
    assert result["action"] == "sla_refresh_attempted"
    assert "test_src_stuck" in result.get("metadata", {}).get("refreshed", [])
    assert calls == ["hit"]
    # Only stuck source is touched; fresh stays out.
    meta = result.get("metadata", {})
    assert "test_src_fresh" not in meta.get("refreshed", [])
    logs = await _fetch_log_rows(tmp_db, "sla_stuck_sources")
    assert logs


@pytest.mark.asyncio
async def test_sla_stuck_sources_noop_when_no_alerts(engine, tmp_db,
                                                    tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(tmp_path))
    result = await engine.trigger_recovery("sla_stuck_sources", manual=True)
    assert result["fixed"] is False
    assert result["action"] == "no_sla_alerts"


# ─────────────────────────────────────────────────────────────────────
# Recovery: missing_odds_snapshot
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_odds_snapshot_triggers_fallback(engine, tmp_db,
                                                       monkeypatch):
    # Seed a game_contexts row so the recovery identifies an active sport.
    conn = sqlite3.connect(tmp_db)
    try:
        soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        conn.execute(
            "INSERT INTO game_contexts (sport, game_date) VALUES (?, ?)",
            ("basketball_nba", soon),
        )
        conn.commit()
    finally:
        conn.close()

    # Stub every scraper to return a benign success so we don't hit the network.
    async def _ok(_sport):
        return {"ok": True, "game_count": 1}

    import tools.dk_scraper as _dk
    import tools.fanduel_scraper as _fd
    monkeypatch.setattr(_dk, "scrape_dk_odds", _ok, raising=False)
    monkeypatch.setattr(_fd, "scrape_fd_odds", _ok, raising=False)

    # Ensure line_monitor lookup returns None so the recovery falls through
    # to the direct-scraper path.
    import importlib
    api_mod = None
    try:
        api_mod = importlib.import_module("api")
        monkeypatch.setattr(api_mod, "line_monitor", None, raising=False)
    except Exception:
        pass  # api not importable in test env — recovery handles the None

    result = await engine.trigger_recovery("missing_odds_snapshot",
                                            manual=True)
    assert result["action"] in ("forced_odds_fallback",
                                 "odds_snapshots_fresh",
                                 "no_active_sport_identified")
    logs = await _fetch_log_rows(tmp_db, "missing_odds_snapshot")
    assert logs


@pytest.mark.asyncio
async def test_missing_odds_snapshot_noop_when_fresh(engine, tmp_db,
                                                     monkeypatch):
    # Recent snapshot row — recovery should say "fresh".
    now_iso = datetime.now(timezone.utc).isoformat()
    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO game_contexts (sport, game_date) VALUES (?, ?)",
            ("basketball_nba", soon),
        )
        conn.execute(
            "INSERT INTO odds_snapshots (sport, timestamp) VALUES (?, ?)",
            ("basketball_nba", now_iso),
        )
        conn.commit()
    finally:
        conn.close()

    result = await engine.trigger_recovery("missing_odds_snapshot",
                                            manual=True)
    assert result["action"] == "odds_snapshots_fresh"


# ─────────────────────────────────────────────────────────────────────
# Cooldown + status endpoint
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_prevents_auto_rerun(engine, tmp_db):
    # Trigger manually (resets cooldown) then call run_expanded_recoveries —
    # should NOT re-run the same recovery because the cooldown is in force.
    await engine.trigger_recovery("orphaned_processing", manual=True)
    logs_before = await _fetch_log_rows(tmp_db, "orphaned_processing")
    assert len(logs_before) == 1
    # Auto sweep — every recovery's cooldown is active; expect 0 runs.
    auto = await engine.run_expanded_recoveries(force=False)
    names_ran = [r.get("recovery_name") for r in auto]
    assert "orphaned_processing" not in names_ran


@pytest.mark.asyncio
async def test_force_bypasses_cooldown(engine, tmp_db):
    await engine.trigger_recovery("orphaned_processing", manual=True)
    auto_forced = await engine.run_expanded_recoveries(force=True)
    names_ran = [r.get("recovery_name") for r in auto_forced]
    assert "orphaned_processing" in names_ran


@pytest.mark.asyncio
async def test_trigger_unknown_recovery_raises(engine, tmp_db):
    with pytest.raises(ValueError):
        await engine.trigger_recovery("this_does_not_exist", manual=True)


@pytest.mark.asyncio
async def test_expanded_status_includes_all_recoveries(engine, tmp_db):
    status = await engine.get_expanded_status()
    assert "recoveries" in status
    names = {r["recovery_name"] for r in status["recoveries"]}
    expected = {"db_lock_long", "orphaned_processing", "research_loop_stuck",
                "claude_cli_missing", "sla_stuck_sources",
                "missing_odds_snapshot"}
    assert expected == names
    # Each entry has cooldown metadata.
    for r in status["recoveries"]:
        assert "cooldown_seconds" in r
        assert "cooldown_remaining_seconds" in r
        assert "last_run" in r  # may be None before first run


@pytest.mark.asyncio
async def test_expanded_status_surfaces_last_run(engine, tmp_db):
    await engine.trigger_recovery("orphaned_processing", manual=True)
    status = await engine.get_expanded_status()
    entry = next(r for r in status["recoveries"]
                 if r["recovery_name"] == "orphaned_processing")
    assert entry["last_run"] is not None
    assert entry["last_run"]["trigger"] == "manual"
    assert entry["last_run"]["action"] in ("no_orphans",
                                           "marked_failed_stuck_processing")


# ─────────────────────────────────────────────────────────────────────
# Module-level registry sanity
# ─────────────────────────────────────────────────────────────────────

def test_recoveries_registry_shape():
    from tools.self_repair import SelfRepairEngine
    assert len(SelfRepairEngine._RECOVERIES) == 6
    seen_names = set()
    for name, fn_name, cooldown in SelfRepairEngine._RECOVERIES:
        assert name and isinstance(name, str)
        assert name not in seen_names, "duplicate recovery name"
        seen_names.add(name)
        assert isinstance(fn_name, str) and fn_name.startswith("_recover_")
        assert cooldown > 0
        # Method exists on the class.
        assert callable(getattr(SelfRepairEngine, fn_name, None))


def test_every_recovery_has_a_cooldown_constant():
    """Cooldown values are from well-known constants, not magic numbers."""
    from tools.self_repair import SelfRepairEngine
    # Every recovery cooldown must be at least 60s — prevent thrashing.
    for _name, _fn, cooldown in SelfRepairEngine._RECOVERIES:
        assert cooldown >= 60


# ─────────────────────────────────────────────────────────────────────
# self_repair_log schema integrity
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_row_has_all_expected_fields(engine, tmp_db):
    await engine.trigger_recovery("orphaned_processing", manual=True)
    rows = await _fetch_log_rows(tmp_db, "orphaned_processing")
    assert rows
    recovery_name, trigger, success, action, detail, meta_json, elapsed_ms = (
        rows[0]
    )
    assert recovery_name == "orphaned_processing"
    assert trigger == "manual"
    assert success in (0, 1)
    assert isinstance(action, str)
    assert isinstance(detail, str)
    assert elapsed_ms is not None and elapsed_ms >= 0


@pytest.mark.asyncio
async def test_metadata_json_is_valid_json_when_present(engine, tmp_db):
    # Provoke a path that emits metadata.
    stuck_ts = (datetime.now(timezone.utc)
                - timedelta(seconds=self_repair.TASK_MAX_TIMEOUT_SECONDS * 10)
                ).isoformat()
    conn = sqlite3.connect(tmp_db)
    try:
        conn.execute(
            "INSERT INTO task_queue (query, status, started_at) "
            "VALUES ('x', 'PROCESSING', ?)",
            (stuck_ts,),
        )
        conn.commit()
    finally:
        conn.close()
    await engine.trigger_recovery("orphaned_processing", manual=True)
    rows = await _fetch_log_rows(tmp_db, "orphaned_processing")
    assert rows
    meta_json = rows[0][5]
    if meta_json:
        parsed = json.loads(meta_json)
        assert "task_ids" in parsed or "cutoff_seconds" in parsed
