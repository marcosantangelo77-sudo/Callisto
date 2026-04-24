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
    get_migration_status,
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
    assert result["bootstrapped"] == len(discovered_versions)
    # Bootstrap marks every migration as already-applied, so applied list is empty.
    assert result["applied"] == []

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


# ─────────────────────────────────────────────
# Numbering & duplicate-filename guard
# ─────────────────────────────────────────────

def test_migration_numbering_has_no_gaps():
    """Every version from 1..max must exist — no missing NNN file."""
    migs = discover_migrations()
    versions = sorted({m.version for m in migs})
    assert versions[0] == 1
    max_v = versions[-1]
    expected = list(range(1, max_v + 1))
    assert versions == expected, (
        f"gap in migration numbering: have {versions}, expected {expected}"
    )


def test_every_migration_has_callable_up():
    migs = discover_migrations()
    assert migs, "no migrations discovered"
    for m in migs:
        assert callable(m.up), f"{m.module_name}.up is not callable"


def test_source_checksums_are_populated():
    migs = discover_migrations()
    for m in migs:
        assert m.source_checksum, (
            f"{m.module_name} has empty source_checksum"
        )
        assert len(m.source_checksum) == 64  # sha256 hex digest


# ─────────────────────────────────────────────
# Mid-migration failure = rollback + halt.
# ─────────────────────────────────────────────

def test_mid_migration_failure_rolls_back_and_halts(tmp_path, monkeypatch):
    """A failing migration must:
      - roll back its own DDL (no partial schema for that version),
      - NOT record itself in schema_migrations,
      - prevent later migrations from running,
      - leave earlier migrations' rows intact.
    """
    from tools.migrations import runner as runner_mod

    db = str(tmp_path / "crash.db")
    real_discover = runner_mod.discover_migrations

    # Boom migration — creates a sentinel table then explodes. If rollback
    # is correct, the sentinel must NOT exist post-failure.
    def _boom_up(conn):
        conn.execute("CREATE TABLE crash_sentinel (id INTEGER)")
        raise RuntimeError("simulated mid-migration failure")

    def _later_up(conn):
        conn.execute("CREATE TABLE should_never_exist (id INTEGER)")

    def _patched_discover():
        migs = list(real_discover())
        max_v = max(m.version for m in migs)
        boom = runner_mod.Migration(
            version=max_v + 1, name="simulated_boom",
            module_name="test.boom", up=_boom_up, down=None,
            source_checksum="0" * 64,
        )
        later = runner_mod.Migration(
            version=max_v + 2, name="should_not_run",
            module_name="test.later", up=_later_up, down=None,
            source_checksum="1" * 64,
        )
        migs.extend([boom, later])
        return migs

    monkeypatch.setattr(runner_mod, "discover_migrations", _patched_discover)

    with pytest.raises(RuntimeError, match="simulated"):
        apply_pending_migrations(db)

    conn = sqlite3.connect(db)
    try:
        # Crash migration's DDL must have been rolled back.
        got = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='crash_sentinel'"
        ).fetchone()
        assert got is None, "crash migration DDL was not rolled back"

        # Later migration must not have been attempted.
        got = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='should_never_exist'"
        ).fetchone()
        assert got is None, "post-failure migration ran — halt semantics broken"

        # schema_migrations has every migration BEFORE the crashing one,
        # and NOT the crashing one.
        applied = {
            int(r[0]) for r in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        real_versions = {m.version for m in real_discover()}
        assert real_versions <= applied, (
            f"earlier migrations were rolled back too: missing "
            f"{sorted(real_versions - applied)}"
        )
        crash_version = max(real_versions) + 1
        assert crash_version not in applied
        assert (crash_version + 1) not in applied
    finally:
        conn.close()


def test_reapply_after_partial_crash_is_clean(tmp_path, monkeypatch):
    """After a crash, fixing the buggy migration and re-running must succeed
    — the crash migration's version is still pending, so a corrected version
    gets applied cleanly without stepping on earlier state.
    """
    from tools.migrations import runner as runner_mod
    real_discover = runner_mod.discover_migrations
    db = str(tmp_path / "recovery.db")

    state = {"should_fail": True}

    def _flaky_up(conn):
        conn.execute("CREATE TABLE flaky_table (id INTEGER)")
        if state["should_fail"]:
            raise RuntimeError("first attempt fails")

    def _patched_discover():
        migs = list(real_discover())
        max_v = max(m.version for m in migs)
        migs.append(runner_mod.Migration(
            version=max_v + 1, name="flaky",
            module_name="test.flaky", up=_flaky_up, down=None,
            source_checksum="2" * 64,
        ))
        return migs

    monkeypatch.setattr(runner_mod, "discover_migrations", _patched_discover)

    with pytest.raises(RuntimeError):
        apply_pending_migrations(db)

    # Fix the bug → rerun → succeeds.
    state["should_fail"] = False
    result = apply_pending_migrations(db)

    real_max = max(m.version for m in real_discover())
    assert (real_max + 1) in result["applied"], (
        "flaky migration did not re-apply after bug fix"
    )

    # Table now exists and schema_migrations reflects it.
    conn = sqlite3.connect(db)
    try:
        got = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='flaky_table'"
        ).fetchone()
        assert got is not None
        applied_v = {
            int(r[0]) for r in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        assert (real_max + 1) in applied_v
    finally:
        conn.close()


# ─────────────────────────────────────────────
# get_migration_status (read-only report for /admin/db/migrations)
# ─────────────────────────────────────────────

def test_status_on_fresh_db_reports_all_pending(tmp_path):
    db = str(tmp_path / "status_fresh.db")
    status = get_migration_status(db)
    assert status["schema_version"] == 0
    assert status["applied"] == []
    pending_versions = {p["version"] for p in status["pending"]}
    known = set(status["known_versions"])
    assert pending_versions == known
    assert status["drift"] == []


def test_status_after_apply_reports_all_applied(tmp_path):
    db = str(tmp_path / "status_applied.db")
    apply_pending_migrations(db)
    status = get_migration_status(db)
    assert status["pending"] == []
    assert status["drift"] == []
    assert status["schema_version"] == max(
        m.version for m in discover_migrations()
    )
    applied_versions = {a["version"] for a in status["applied"]}
    assert applied_versions == set(status["known_versions"])


def test_status_detects_checksum_drift(tmp_path):
    """Simulate someone editing an already-applied migration: the runner
    stored the OLD sha256 in schema_migrations; discover_migrations() now
    sees a DIFFERENT sha256. Status must flag this as drift."""
    db = str(tmp_path / "drift.db")
    apply_pending_migrations(db)

    # Corrupt one stored checksum to simulate "migration file was edited
    # after it was applied" (drift direction is symmetric — either side
    # changing triggers the same alarm).
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("deadbeef" * 8,),
        )
    finally:
        conn.close()

    status = get_migration_status(db)
    drift_versions = {d["version"] for d in status["drift"]}
    assert 1 in drift_versions
    entry = next(d for d in status["drift"] if d["version"] == 1)
    assert entry["stored_checksum"] != entry["current_checksum"]


# ─────────────────────────────────────────────
# CREATE TABLE IF NOT EXISTS audit — every migration CREATE must be
# idempotent. (Exception: 005 rebuild which uses a _new table as a
# scratchpad.)
# ─────────────────────────────────────────────

def test_all_migration_create_tables_use_if_not_exists():
    import glob
    import os
    import re

    offenders: list[tuple[str, str]] = []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pattern = os.path.join(here, "tools", "migrations", "[0-9][0-9][0-9]_*.py")
    for path in sorted(glob.glob(pattern)):
        src = open(path, "r", encoding="utf-8").read()
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)([a-zA-Z_][a-zA-Z0-9_]*)",
            src,
            re.IGNORECASE,
        ):
            tbl = m.group(1)
            # 005 creates task_queue_new as an intentional scratchpad inside
            # a table-rebuild that drops + renames. Not an idempotency bug.
            if tbl.endswith("_new"):
                continue
            offenders.append((os.path.basename(path), tbl))
    assert not offenders, (
        f"migrations with non-idempotent CREATE TABLE (should use IF NOT "
        f"EXISTS): {offenders}"
    )


# ─────────────────────────────────────────────
# scripts/migrate.py CLI smoke tests
# ─────────────────────────────────────────────

def test_migrate_script_status(tmp_path, capsys):
    import scripts.migrate as migrate_cli
    db = str(tmp_path / "cli_status.db")
    rc = migrate_cli.main(["--db", db, "--status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"schema_version"' in out
    assert '"pending"' in out


def test_migrate_script_dry_run_does_not_apply(tmp_path, capsys):
    import scripts.migrate as migrate_cli
    db = str(tmp_path / "cli_dry.db")
    rc = migrate_cli.main(["--db", db, "--dry-run"])
    assert rc == 0
    # DB file must NOT have the schema_migrations table because we
    # did not run --apply.
    if os.path.exists(db):
        conn = sqlite3.connect(db)
        try:
            got = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='schema_migrations'"
            ).fetchone()
        finally:
            conn.close()
        assert got is None, "--dry-run wrote to the DB"


def test_migrate_script_apply_then_status(tmp_path):
    import scripts.migrate as migrate_cli
    db = str(tmp_path / "cli_apply.db")
    assert migrate_cli.main(["--db", db, "--apply"]) == 0
    # Second apply is idempotent and exits 0.
    assert migrate_cli.main(["--db", db, "--apply"]) == 0

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    finally:
        conn.close()
    applied = {int(r[0]) for r in rows}
    discovered = {m.version for m in discover_migrations()}
    assert applied == discovered
