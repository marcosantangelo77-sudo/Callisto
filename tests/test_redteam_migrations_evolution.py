"""RED TEAM — schema evolution & migrations (surface: migrations/schema).

Rotation entry 2026-08-24 · branch redteam/rotating-0824-223125.
Surface picked because it is the only named unattacked ground with zero
red-team coverage: every migration (013/014/015 especially) has never run
against a real database, api.py runs ensure_schema() BEFORE
apply_pending_migrations() unconditionally, and tools/migrations/__init__
makes guarantees about bootstrap behaviour that no test checks.

Method: DIFFERENTIAL between the two schema-evolution histories this codebase
can produce (fresh-at-head vs evolved-through-migrations) plus a property
sweep over duplicated rule tables (family 2), and family-1 hunts for
verification layers that store evidence nobody reads.

Findings reproduced below (each test names its defect):
  RT-MIG-1  CRITICAL — the full startup sequence migrates EVERY database
            (fresh included) to the post-seam hypotheses shape, then the
            production writer inserts sport/market_type columns that no
            longer exist. tools/schema/compat.py, cited by
            plugins/sports/schema.py as the thing keeping writers alive,
            does not exist. Hypothesis creation is dead on any DB after
            first boot with this code.
  RT-MIG-2  HIGH — migration 013's FK repair rebuilds child tables from
            their CREATE TABLE text and silently drops every index and
            trigger on them.
  RT-MIG-3  MEDIUM — migration 015.down() restores confidence via a
            correlated subquery that yields NULL for rows created after
            the migration ran; hermes_learnings.confidence is nullable,
            so post-rollback learnings get confidence = NULL silently.
  RT-MIG-4  INFO — migration 010 carries migration 007's docstring; its
            inline team→timezone tables are an "intentional duplication"
            of tools/game_dates that nothing pins in sync (family 2).
  RT-MIG-5  MEDIUM — schema_migrations.checksum is stored so "a later
            audit can detect" edited migrations, but no code path ever
            compares it: a verification layer whose input nobody reads
            (PATTERNS family 1).

Tests written as expectations: the failing ones ARE the findings.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

import pytest

from tools.migrations import apply_pending_migrations
from tools.schema import ensure_schema


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _run_startup_sequence(db_path) -> None:
    """Exactly what api.py:lifespan does, in order (api.py:843 → :864)."""
    asyncio.run(ensure_schema(str(db_path)))
    apply_pending_migrations(str(db_path))


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# Verbatim column list of the production writer, tools/hypothesis.py:580-584.
PRODUCTION_INSERT_COLUMNS = (
    "hypothesis_id, name, thesis, sport, market_type, model_config, "
    "edge_threshold, status, min_sample_size, significance_level, "
    "created_at, updated_at, notes"
)


# ──────────────────────────────────────────────────────────────
# RT-MIG-1 — the startup sequence breaks the production writer
# ──────────────────────────────────────────────────────────────

def test_rt_mig1_production_writer_survives_full_startup_sequence(tmp_path):
    """CRITICAL.

    Fresh DB + ensure_schema() + apply_pending_migrations() — the exact
    sequence every boot performs — must leave hypotheses writable by the
    application's own INSERT (tools/hypothesis.py:580). Today migration 013
    rebuilds hypotheses without sport/market_type on EVERY database
    (ensure_schema's plugin DDL still creates the welded shape), and no
    compat layer exists, so this INSERT raises OperationalError.
    """
    db_path = tmp_path / "rt_mig1.db"
    _run_startup_sequence(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        cols = _columns(conn, "hypotheses")
        missing = [
            c for c in ("sport", "market_type") if c not in cols
        ]
        assert not missing, (
            "Startup sequence dropped columns the production writer still "
            f"inserts: {missing}. tools/schema/compat.py (the layer "
            "plugins/sports/schema.py:1241 says keeps writers alive during "
            "the transition) does not exist in the tree."
        )
        conn.execute(
            f"INSERT INTO hypotheses ({PRODUCTION_INSERT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
            (
                "rt1", "rt thesis", "basketball_nba", "player_points",
                "{}", 0.01, 50, 0.05,
                "2026-08-24T00:00:00Z", "2026-08-24T00:00:00Z", "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_rt_mig1_real_hypothesis_manager_create_after_startup(tmp_path):
    """RT-MIG-1 through the real public API, not raw SQL.

    HypothesisManager.create_hypothesis() is the ONLY creation path the
    app uses (api.py wires it at startup). Run the full startup sequence,
    then create one hypothesis through the manager.
    """
    from tools.hypothesis import HypothesisManager

    db_path = str(tmp_path / "rt_mig1b.db")
    _run_startup_sequence(db_path)

    async def _create():
        mgr = HypothesisManager(db_path)
        await mgr.initialize()
        try:
            return await mgr.create_hypothesis(
                name="rt-red-team",
                thesis="the migrated schema still accepts writes",
                sport="basketball_nba",
                market_type="player_points",
                model_config={},
            )
        finally:
            await mgr.close()

    hid = asyncio.run(_create())
    assert hid, "production writer returned no hypothesis_id"


# ──────────────────────────────────────────────────────────────
# RT-MIG-2 — FK repair silently drops child-table indexes
# ──────────────────────────────────────────────────────────────

_LEGACY_WELDED_HYP = """
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    thesis TEXT NOT NULL,
    sport TEXT NOT NULL,
    market_type TEXT NOT NULL,
    model_config TEXT NOT NULL,
    edge_threshold REAL NOT NULL DEFAULT 0.01,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN
        ('draft','backtesting','paper_trading','live','paused',
         'drawdown_paused','retired','rejected')),
    min_sample_size INTEGER NOT NULL DEFAULT 50,
    significance_level REAL NOT NULL DEFAULT 0.05,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at DATETIME,
    promoted_by TEXT,
    notes TEXT
)
"""


@pytest.fixture()
def damaged_legacy_db():
    """Pre-seam DB whose backtest_events carries the REFERENCES
    hypotheses_old_* damage that engine.ensure_schema's own rebuilds
    leave behind, plus a real index a deployment would have."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(_LEGACY_WELDED_HYP)
    conn.execute(
        'CREATE TABLE backtest_events ('
        ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
        ' run_id TEXT NOT NULL,'
        ' hypothesis_id TEXT NOT NULL'
        '   REFERENCES "hypotheses_old_20260421"(hypothesis_id),'
        ' edge REAL)'
    )
    conn.execute("CREATE INDEX idx_child_hyp ON backtest_events(hypothesis_id)")
    conn.executemany(
        "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport,"
        " market_type, model_config) VALUES (?,?,?,?,?,?)",
        [(f"h{i}", f"n{i}", f"t{i}", "basketball_nba", "ml", "{}")
         for i in range(5)],
    )
    conn.executemany(
        "INSERT INTO backtest_events (run_id, hypothesis_id, edge)"
        " VALUES (?,?,?)",
        [(f"r{i}", f"h{i % 5}", 0.02) for i in range(20)],
    )
    conn.commit()
    yield conn
    conn.close()


def test_rt_mig2_fk_repair_preserves_child_indexes(damaged_legacy_db):
    """HIGH.

    Migration 013._repair_child_fks rebuilds each damaged child from its
    CREATE TABLE text. That text contains no indexes or triggers, so every
    index on the rebuilt table vanishes silently — including
    idx_bt_events_event_id (migration 003), which exists specifically
    because /system/full-status full-scanned 112k rows without it. The
    repair restores correctness of the FK clause at the cost of the
    performance fix migration 003 shipped, and nothing re-creates it.
    """
    m013 = importlib.import_module("tools.migrations.013_schema_seam_hypotheses")
    m013.up(damaged_legacy_db)

    # Data survived (control — proves the failure below is not data loss).
    n = damaged_legacy_db.execute(
        "SELECT COUNT(*) FROM backtest_events"
    ).fetchone()[0]
    assert n == 20, f"repair lost rows: {n}"

    idx = [
        r[1] for r in
        damaged_legacy_db.execute("PRAGMA index_list(backtest_events)").fetchall()
    ]
    assert "idx_child_hyp" in idx, (
        "013's FK repair dropped indexes on the rebuilt child table "
        f"(remaining: {idx}). The rebuild must recreate indexes/triggers."
    )


# ──────────────────────────────────────────────────────────────
# RT-MIG-3 — 015.down() writes NULL into live rows
# ──────────────────────────────────────────────────────────────

_HERMES_LEARNINGS_DDL = """
CREATE TABLE hermes_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    learned_at TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    occurrences INTEGER DEFAULT 1,
    source TEXT DEFAULT 'claude'
)
"""


@pytest.fixture()
def learnings_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(_HERMES_LEARNINGS_DDL)
    conn.execute(
        "INSERT INTO hermes_learnings (key, value, learned_at, confidence)"
        " VALUES ('old-key', 'v', ?, 0.9)",
        ("2026-08-20T00:00:00+00:00",),
    )
    conn.commit()
    yield conn
    conn.close()


def test_rt_mig3_down_restores_preexisting_row_confidence(learnings_db):
    """CONTROL for RT-MIG-4: rows that existed before 015 ran must be
    restored to their backed-up confidence by down(). If THIS fails the
    fixture is wrong, not the migration."""
    m015 = importlib.import_module("tools.migrations.015_hermes_confidence_decay")
    m015.up(learnings_db)
    m015.down(learnings_db)

    conf = learnings_db.execute(
        "SELECT confidence FROM hermes_learnings WHERE key = 'old-key'"
    ).fetchone()[0]
    assert conf == pytest.approx(0.9), (
        f"down() did not restore pre-migration confidence: got {conf!r}"
    )


def test_rt_mig3_down_never_nulls_rows_created_after_migration(learnings_db):
    """MEDIUM.

    015.down() runs
        UPDATE hermes_learnings SET confidence =
          (SELECT b.confidence FROM backup b WHERE b.key = key)
    For any row created AFTER the backup snapshot the subquery returns
    NULL, and hermes_learnings.confidence is nullable — so the rollback
    silently writes confidence=NULL into a live learning. Downstream
    readers do float(conf) and crash, or treat it as missing evidence.
    A rollback must be total OR refuse loudly; it must not corrupt the
    rows it was not asked about.
    """
    m015 = importlib.import_module("tools.migrations.015_hermes_confidence_decay")
    m015.up(learnings_db)

    # The system keeps learning while the migration is the recorded history.
    learnings_db.execute(
        "INSERT INTO hermes_learnings (key, value, learned_at, confidence)"
        " VALUES ('post-mig-key', 'v', ?, 0.5)",
        ("2026-08-24T00:00:00+00:00",),
    )
    learnings_db.commit()

    m015.down(learnings_db)

    row = learnings_db.execute(
        "SELECT confidence FROM hermes_learnings WHERE key = 'post-mig-key'"
    ).fetchone()
    assert row is not None and row[0] is not None, (
        "015.down() set confidence=NULL on a row created after the "
        "backup snapshot (correlated-subquery restore). Rollback must "
        "preserve unknown rows untouched."
    )
    assert row[0] == pytest.approx(0.5)


# ──────────────────────────────────────────────────────────────
# RT-MIG-4 — duplicated team→TZ tables must agree (family 2)
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "canonical_name, migration_name",
    [
        ("MLB_TEAM_TZ", "_MLB_TEAM_TZ"),
        ("NBA_TEAM_TZ", "_NBA_TEAM_TZ"),
        ("NHL_TEAM_TZ", "_NHL_TEAM_TZ"),
        ("NFL_TEAM_TZ", "_NFL_TEAM_TZ"),
    ],
)
def test_rt_mig4_migration_tz_tables_match_game_dates(canonical_name, migration_name):
    """Migration 010 inlines copies of tools/game_dates' team→timezone
    tables and backfills local_game_date from them. Rows are only ever
    written where local_game_date IS NULL, so whichever copy runs FIRST
    wins forever: if the canonical table gains a correction (relocation,
    renamed franchise) the migration copy will keep writing stale zones
    for any newly inserted row the backfill later touches. This sweep
    pins the four tables in sync, both directions."""
    gd = importlib.import_module("tools.game_dates")
    m010 = importlib.import_module("tools.migrations.010_local_game_dates")

    canonical = getattr(gd, canonical_name)
    inline = getattr(m010, migration_name)

    only_canonical = sorted(set(canonical) - set(inline))
    only_inline = sorted(set(inline) - set(canonical))
    disagree = sorted(
        t for t in set(canonical) & set(inline)
        if canonical[t] != inline[t]
    )
    assert not (only_canonical or only_inline or disagree), (
        f"{canonical_name} vs {migration_name} drifted: "
        f"missing-from-migration={only_canonical[:5]} "
        f"stale-in-migration={only_inline[:5]} "
        f"value-disagreements={disagree[:5]}"
    )


# ──────────────────────────────────────────────────────────────
# RT-MIG-5 — checksums are stored but never verified (family 1)
# ──────────────────────────────────────────────────────────────

def test_rt_mig5_tampered_applied_checksum_is_detectable_via_public_api(tmp_path):
    """MEDIUM.

    runner.py stores sha256(source) per applied migration "so a later
    audit can detect someone edited 002 after it was applied". Nothing
    ever reads the column back: grep finds no consumer. PATTERNS family 1
    — a verification layer whose input nobody reads. The tamper MUST be
    observable through the public API (raise, or appear in the returned
    status dict); requiring manual SQL against schema_migrations is not
    detection.
    """
    db_path = tmp_path / "rt_mig5.db"
    first = apply_pending_migrations(str(db_path))
    assert first["applied"], "fixture: expected migrations to apply"

    conn = sqlite3.connect(str(db_path))
    victim = sorted(first["applied"])[0]
    conn.execute(
        "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = ?",
        (victim,),
    )
    conn.commit()
    conn.close()

    second = apply_pending_migrations(str(db_path))

    flagged = second.get("checksum_mismatches") or []
    assert victim in flagged, (
        f"A tampered checksum for already-applied migration {victim} went "
        f"unreported on re-run (status: {second}). The stored checksum "
        "column is dead evidence — verify it or delete it."
    )


# ──────────────────────────────────────────────────────────────
# Characterization (passes today) — documents the divergence the
# __init__ docstring denies, so the next reader cannot trust it.
# ──────────────────────────────────────────────────────────────

def test_char_ensure_schema_touches_bootstrap_decision(tmp_path):
    """tools/migrations/__init__.py claims: 'for existing DBs the bootstrap
    step marks every migration as already-applied so nothing re-runs.'

    False for every DB this codebase actually creates: ensure_schema()
    runs FIRST (api.py:843) and inserts versions 20260421/20260422 into
    schema_migrations, so the table is never empty when the runner looks
    at it, bootstrap_existing_db returns 0, and migrations 001-012 all
    execute against existing databases. On the workstation DB that means
    004 (deletes orphan rows) runs at next boot despite documentation
    saying nothing re-runs. Pin the reality here so the docstring gets
    fixed instead of trusted.
    """
    db_path = tmp_path / "char.db"

    # An "existing" DB in the exact state ensure_schema leaves behind:
    # lifecycle tables present, schema_migrations populated by the legacy
    # writer, and none of the NNN framework versions recorded.
    asyncio.run(ensure_schema(str(db_path)))
    conn = sqlite3.connect(str(db_path))
    legacy_rows = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    conn.close()
    assert legacy_rows > 0, (
        "fixture assumption broken: ensure_schema no longer seeds "
        "schema_migrations — revisit this characterization"
    )

    result = apply_pending_migrations(str(db_path))
    assert result["bootstrapped"] == 0, (
        "__init__ docstring says bootstrap covers existing DBs; it fired "
        f"anyway ({result['bootstrapped']}) — update the claim either way"
    )
    assert len(result["skipped"]) < len(result["total_versions"]), (
        "Docstring says 'nothing re-runs' on existing DBs, yet some "
        "framework migrations were skipped via bootstrap. Reality moved; "
        "reconcile docs and behaviour."
    )
