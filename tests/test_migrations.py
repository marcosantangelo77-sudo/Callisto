"""Tests for the versioned migration framework.

Covers:
- Fresh DB → apply_pending_migrations populates schema_migrations and
  leaves the schema in a consistent state.
- Existing DB (hypotheses table pre-seeded) → bootstrap marks everything
  as already-applied; second run is a no-op.
- Concurrent apply from two threads → one runs, the other waits on the
  exclusive lock, both end with identical schema_migrations state.
- DDL guard in db_writer rejects ALTER/CREATE/DROP routed through the
  WriteCoordinator.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from tools.migrations import (
    apply_pending_migrations,
    discover_migrations,
    ensure_migration_table,
    get_applied_versions,
)
from tools.migrations.runner import bootstrap_existing_db


# ─────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────

def test_discover_migrations_returns_sorted_by_version():
    migs = discover_migrations()
    versions = [m.version for m in migs]
    assert versions == sorted(versions)
    assert 1 in versions  # 001_initial
    # Every discovered migration has a callable up().
    for m in migs:
        assert callable(m.up)


def test_discover_migrations_unique_versions():
    migs = discover_migrations()
    versions = [m.version for m in migs]
    assert len(versions) == len(set(versions)), "duplicate migration version"


# ─────────────────────────────────────────────
# Fresh-DB apply
# ─────────────────────────────────────────────

def test_fresh_db_applies_all_migrations(tmp_path):
    db = str(tmp_path / "fresh.db")
    # No hypotheses table ⇒ runner does NOT bootstrap; every migration runs.
    result = apply_pending_migrations(db)
    assert result["bootstrapped"] == 0
    # All discovered versions were applied.
    discovered_versions = {m.version for m in discover_migrations()}
    assert set(result["applied"]) == discovered_versions
    assert result["skipped"] == []

    # schema_migrations is populated and every row has applied_at set.
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT version, name, applied_at, bootstrap FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert {r[0] for r in rows} == discovered_versions
    for r in rows:
        assert r[2] is not None, f"applied_at unset for version {r[0]}"
        assert r[3] == 0


def test_second_apply_is_noop(tmp_path):
    db = str(tmp_path / "idempotent.db")
    apply_pending_migrations(db)
    result2 = apply_pending_migrations(db)
    assert result2["applied"] == []
    # All known versions in skipped.
    discovered_versions = {m.version for m in discover_migrations()}
    assert set(result2["skipped"]) == discovered_versions


# ─────────────────────────────────────────────
# Bootstrap path for existing DB
# ─────────────────────────────────────────────

def test_existing_db_is_bootstrapped(tmp_path):
    db = str(tmp_path / "existing.db")
    # Simulate "Callisto ran once before migrations existed": hypotheses
    # table is present, schema_migrations is not.
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY, status TEXT)")
    conn.commit()
    conn.close()

    result = apply_pending_migrations(db)
    discovered_versions = {m.version for m in discover_migrations()}
    # B5 (schema seam): 013/014 must NOT be bootstrap-marked — they carry
    # the domain-general rebuild and run for real against existing DBs.
    # Same rule for any later seam-carrying migration: everything from 13 on
    # is post-framework and must actually run (generalised so adding
    # migration 015+ doesn't silently break this count).
    assert result["bootstrapped"] == len([v for v in discovered_versions if v < 13])
    pre_seam = {v for v in discovered_versions if v < 13}
    assert set(result["applied"]) == discovered_versions - pre_seam
    assert 13 in result["applied"] and 14 in result["applied"]

    # All schema_migrations rows have bootstrap=1, applied_at=NULL.
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT version, applied_at, bootstrap FROM schema_migrations"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == len(discovered_versions)
    for _v, applied_at, bootstrap in rows:
        # 013+ ran for real (not bootstrapped) so they carry timestamps;
        # every earlier migration is bootstrap-marked with NULL applied_at.
        if _v >= 13:
            assert applied_at is not None
            assert bootstrap == 0
        else:
            assert applied_at is None
            assert bootstrap == 1


def test_bootstrap_only_runs_when_schema_migrations_empty(tmp_path):
    """Bootstrap logic must NOT fire if schema_migrations already has rows."""
    db = str(tmp_path / "already_tracked.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY)")
    conn.close()

    # First run: bootstraps existing DB.
    r1 = apply_pending_migrations(db)
    assert r1["bootstrapped"] > 0

    # Second run: schema_migrations is already populated, so
    # bootstrap_existing_db returns 0 and there's nothing to apply either.
    r2 = apply_pending_migrations(db)
    assert r2["bootstrapped"] == 0
    assert r2["applied"] == []


# ─────────────────────────────────────────────
# Concurrency: two processes race; one wins, other waits.
# ─────────────────────────────────────────────

def test_concurrent_apply_is_serialized(tmp_path):
    db = str(tmp_path / "concurrent.db")
    results: list[dict] = []
    errors: list[Exception] = []

    def worker():
        try:
            results.append(apply_pending_migrations(db))
        except Exception as e:  # pragma: no cover — diagnosis helper
            errors.append(e)

    t1 = threading.Thread(target=worker, name="mig-t1")
    t2 = threading.Thread(target=worker, name="mig-t2")
    t1.start()
    time.sleep(0.01)
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=120)

    assert not errors, f"migration worker errored: {errors!r}"
    assert len(results) == 2

    # Both end with the same schema_migrations state.
    conn = sqlite3.connect(db)
    try:
        versions = {
            r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
    finally:
        conn.close()
    discovered = {m.version for m in discover_migrations()}
    assert versions == discovered

    # Exactly one worker did the applying; the other saw everything already
    # applied. (Either ordering is fine — we just need disjoint work.)
    applied_sets = [set(r["applied"]) for r in results]
    non_empty = [s for s in applied_sets if s]
    assert len(non_empty) <= 1, (
        f"both workers applied migrations — lock failed: {applied_sets!r}"
    )


# ─────────────────────────────────────────────
# schema_migrations upgrade path
# ─────────────────────────────────────────────

def test_ensure_migration_table_upgrades_old_shape(tmp_path):
    """A DB created by the pre-framework code has schema_migrations with
    only (version, name, applied_at). The new runner must idempotently
    add `checksum` and `bootstrap` columns."""
    db = str(tmp_path / "old_shape.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        ensure_migration_table(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
    finally:
        conn.close()
    assert {"version", "name", "applied_at", "checksum", "bootstrap"} <= cols


# ─────────────────────────────────────────────
# DDL guard: coordinator must reject ALTER/CREATE/DROP.
# ─────────────────────────────────────────────

def test_write_re_no_longer_matches_ddl():
    from tools.db_writer import _is_write_sql, _is_ddl_sql
    for sql in (
        "ALTER TABLE foo ADD COLUMN bar INTEGER",
        "CREATE TABLE foo (id INT)",
        "CREATE INDEX idx ON foo(id)",
        "DROP TABLE foo",
    ):
        assert not _is_write_sql(sql), f"DDL still matches _WRITE_RE: {sql!r}"
        assert _is_ddl_sql(sql), f"DDL not caught by _DDL_RE: {sql!r}"
    for sql in (
        "INSERT INTO foo VALUES (1)",
        "UPDATE foo SET v=1",
        "DELETE FROM foo",
        "REPLACE INTO foo VALUES (1)",
    ):
        assert _is_write_sql(sql), f"DML missed by _WRITE_RE: {sql!r}"
        assert not _is_ddl_sql(sql), f"DML flagged as DDL: {sql!r}"


@pytest.mark.asyncio
async def test_coordinator_rejects_ddl(tmp_path):
    """If something slips DDL through to the coordinator, `_apply` must
    raise loudly instead of silently bumping writes_failed."""
    from tools.db_writer import WriteCoordinator, stop_all

    db_path = str(tmp_path / "ddl_guard.db")
    # Seed a trivial table so ALTER has a target.
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    coord = WriteCoordinator(db_path)
    await coord.start()
    try:
        with pytest.raises(RuntimeError, match="DDL"):
            await coord.execute("ALTER TABLE t ADD COLUMN v INTEGER")
        with pytest.raises(RuntimeError, match="DDL"):
            await coord.execute("CREATE INDEX idx_t_v ON t(id)")
        with pytest.raises(RuntimeError, match="DDL"):
            await coord.execute("DROP TABLE t")
        # And inside a transaction:
        with pytest.raises(RuntimeError, match="DDL"):
            await coord.transaction([("ALTER TABLE t ADD COLUMN w INTEGER", ())])
    finally:
        await coord.stop()
        await stop_all()


# ─────────────────────────────────────────────
# Specific migrations: end-to-end effects.
# ─────────────────────────────────────────────

def test_migration_002_adds_archived_column(tmp_path):
    db = str(tmp_path / "arch.db")
    # Pre-create the four rotation targets so migration 002 has work to do.
    conn = sqlite3.connect(db)
    for table in ("bets", "ev_opportunities", "line_movements", "odds_snapshots"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    apply_pending_migrations(db)

    conn = sqlite3.connect(db)
    try:
        for table in ("bets", "ev_opportunities", "line_movements", "odds_snapshots"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "archived" in cols, f"migration 002 skipped {table}"
    finally:
        conn.close()


def test_migration_003_creates_event_id_index(tmp_path):
    db = str(tmp_path / "idx.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE backtest_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "  event_id TEXT NOT NULL, "
        "  hypothesis_id TEXT NOT NULL, "
        "  game_date DATE NOT NULL"
        ")"
    )
    conn.commit()
    conn.close()

    apply_pending_migrations(db)

    conn = sqlite3.connect(db)
    try:
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_bt_events_event_id'"
        ).fetchone()
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(DISTINCT event_id) FROM backtest_events"
        ).fetchall()
    finally:
        conn.close()
    assert idx is not None, "idx_bt_events_event_id not created"
    plan_text = " | ".join(str(r) for r in plan)
    assert "idx_bt_events_event_id" in plan_text, f"plan did not use index: {plan_text}"


def test_migration_004_deletes_fk_orphans(tmp_path):
    db = str(tmp_path / "orphans.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE hypothesis_stats ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "  hypothesis_id TEXT NOT NULL, "
        "  stage TEXT)"
    )
    # One valid, one orphan.
    conn.execute("INSERT INTO hypotheses VALUES ('hyp_good')")
    conn.execute("INSERT INTO hypothesis_stats (hypothesis_id, stage) VALUES ('hyp_good', 'backtest')")
    conn.execute("INSERT INTO hypothesis_stats (hypothesis_id, stage) VALUES ('hyp_ghost', 'backtest')")
    conn.commit()
    conn.close()

    # Because hypotheses exists the runner will bootstrap all migrations as
    # already-applied — skip the bootstrap for this test by seeding the
    # migrations table manually to force v004 to run.
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT, "
        "  checksum TEXT, bootstrap INTEGER NOT NULL DEFAULT 0)"
    )
    # Mark 1,2,3 as bootstrapped so only 004+ run.
    for v, n in ((1, "initial"), (2, "add_archived_columns"), (3, "backtest_events_event_id_index")):
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, bootstrap) VALUES (?, ?, 1)",
            (v, n),
        )
    conn.close()

    apply_pending_migrations(db)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT hypothesis_id FROM hypothesis_stats"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["hyp_good"], "migration 004 did not remove orphan"
