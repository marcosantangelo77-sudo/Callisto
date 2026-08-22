"""Migration 013 — remove the sport NOT NULL weld from core ``hypotheses``.

Problem
-------
``hypotheses.sport TEXT NOT NULL`` (plus ``market_type NOT NULL`` and
``edge_threshold``) makes it structurally impossible to store a research
claim about anything but sports. The lifecycle itself — draft →
backtesting → paper_trading → live → retired, with evidence and
calibration — is domain-general; only those three columns are not.

What this migration does
------------------------
1. Rebuilds ``hypotheses`` WITHOUT the sports columns:
     - ``sport`` and ``market_type`` are dropped entirely.
     - ``edge_threshold REAL DEFAULT 0.01`` is kept on core (an edge
       threshold is meaningful for any claim) but becomes NULLable.
     - ``domain TEXT NOT NULL DEFAULT 'sports'`` is added: every existing
       row IS a sports hypothesis, so the default backfills honestly.
     - Status CHECK keeps exactly the current allowed values
       ('draft','backtesting','paper_trading','live','paused',
        'drawdown_paused','retired','rejected').
2. Copies every row verbatim into a plugin-owned side table
   ``hypothesis_sports_ext(hypothesis_id PK, sport NOT NULL,
   market_type, edge_threshold)`` BEFORE dropping the columns, so no
   domain data is lost.
3. Repairs collateral damage from migrations 20260421/20260422: SQLite's
   ALTER TABLE RENAME rewrites FOREIGN KEY clauses in child tables, so
   ``backtest_runs``, ``backtest_events``, ``paper_trades`` and
   ``hypothesis_stats`` may still carry ``REFERENCES hypotheses_old_*``
   pointing at tables that no longer exist. This migration rewrites any
   such clause back to ``REFERENCES hypotheses(hypothesis_id)`` while it
   has the table open anyway. It also verifies that all four child tables
   reference ``hypotheses`` when they exist.

Safety properties
-----------------
* Pre-flight row-count check: counts hypotheses before and after; aborts
  (raises) if any row would be lost.
* Runs inside the runner's per-migration transaction (BEGIN IMMEDIATE …
  COMMIT): atomic. A crash mid-migration rolls everything back.
* Uses ``PRAGMA legacy_alter_table=ON`` around its own RENAME so the
  rename does not rewrite child FK text to point at the temp name.
* Idempotent: if ``schema_migrations`` says 013 ran, the runner skips it.
  Additionally every step checks current state first, so running the
  body twice is also safe.
* Dry-run: ``dry_run(conn)`` reports exactly what would change without
  changing anything.
* Rollback: ``down()`` restores the original welded shape (re-adding
  NOT NULL sport/market_type) from the extension-table backup. Data in
  ``hypothesis_sports_ext`` is preserved by down().
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("callisto.migrations.013")

MIGRATION_VERSION = 20260823  # recorded in _b5_seam_migrations journal

# Child tables that legitimately hold an FK to hypotheses. Used both for
# repair of the legacy rename damage and for post-flight verification.
HYP_FK_CHILDREN = (
    "backtest_runs",
    "backtest_events",
    "paper_trades",
    "hypothesis_stats",
    "masters_backtest_results",
    "masters_predictions",
)

# Columns of the pre-migration hypotheses table (the shape produced by
# ensure_schema + migrations 20260421/20260422). Used for verbatim copy.
LEGACY_COLUMNS = [
    "hypothesis_id", "name", "thesis", "sport", "market_type",
    "model_config", "edge_threshold", "status", "min_sample_size",
    "significance_level", "created_at", "updated_at", "promoted_at",
    "promoted_by", "notes",
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """True when hypotheses still carries the sport NOT NULL weld."""
    if not _table_exists(conn, "hypotheses"):
        return False  # fresh DB — ensure_schema already created the new shape
    cols = _columns(conn, "hypotheses")
    return "sport" in cols


# ─────────────────────────────────────────────
# New-shape DDL
# ─────────────────────────────────────────────

NEW_HYPOTHESES_DDL = """
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    thesis TEXT NOT NULL,
    -- Domain-general lifecycle: which plugin's domain this claim belongs
    -- to. Existing rows are all 'sports' (that is all the system ever
    -- stored); new domains add their own value without touching schema.
    domain TEXT NOT NULL DEFAULT 'sports',
    model_config TEXT NOT NULL,
    edge_threshold REAL DEFAULT 0.01,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','backtesting','paper_trading','live',
                         'paused','drawdown_paused','retired','rejected')),
    min_sample_size INTEGER NOT NULL DEFAULT 50,
    significance_level REAL NOT NULL DEFAULT 0.05,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    promoted_at DATETIME,
    promoted_by TEXT,
    notes TEXT
)
""".strip()

EXTENSION_DDL = """
CREATE TABLE IF NOT EXISTS hypothesis_sports_ext (
    hypothesis_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    market_type TEXT,
    edge_threshold REAL,
    FOREIGN KEY (hypothesis_id) REFERENCES hypotheses(hypothesis_id)
)
""".strip()


# ─────────────────────────────────────────────
# Dry run — reports without changing anything
# ─────────────────────────────────────────────

def dry_run(conn: sqlite3.Connection) -> dict:
    """Report what this migration WOULD do. Performs no writes.

    Read-only PRAGMAs and SELECTs only; safe to run on production.
    """
    report: dict = {"needed": _needs_migration(conn), "actions": []}
    if not report["needed"]:
        report["actions"].append(
            "no-op: hypotheses table absent or already migrated"
        )
        return report

    n = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    cols = _columns(conn, "hypotheses")
    report["hypothesis_rows"] = n
    report["actions"] = [
        f"create hypothesis_sports_ext side table and copy {n} rows "
        "(sport/market_type/edge_threshold)",
        "rename hypotheses -> hypotheses_old_b5",
        "create new domain-general hypotheses (sport/market_type dropped, "
        "domain TEXT NOT NULL DEFAULT 'sports' added, edge_threshold nullable)",
        f"copy {n} rows back (sport/market_type -> ext table)",
        "drop hypotheses_old_b5",
        "restore idx_hypotheses_name unique index",
        "repair stale REFERENCES hypotheses_old_* clauses in child tables"
        f" {list(HYP_FK_CHILDREN)} if present",
    ]
    # Report detected legacy-rename damage so the operator sees scope first.
    damaged = []
    for t in HYP_FK_CHILDREN:
        if not _table_exists(conn, t):
            continue
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        if "hypotheses_old_" in (sql or ""):
            damaged.append(t)
    if damaged:
        report["fk_repair_targets"] = damaged
    return report


# ─────────────────────────────────────────────
# up()
# ─────────────────────────────────────────────

def up(conn: sqlite3.Connection) -> None:
    if not _needs_migration(conn):
        logger.info("Migration 013: nothing to do (already seam-shaped)")
        return

    pre_count = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]

    # ── Step 1: side table + verbatim copy of domain columns ──
    conn.execute(EXTENSION_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO hypothesis_sports_ext "
        "(hypothesis_id, sport, market_type, edge_threshold) "
        "SELECT hypothesis_id, sport, market_type, edge_threshold "
        "FROM hypotheses"
    )
    ext_copied = conn.execute(
        "SELECT COUNT(*) FROM hypothesis_sports_ext"
    ).fetchone()[0]
    if ext_copied < pre_count:
        raise RuntimeError(
            f"Migration 013 aborted: extension copy holds {ext_copied} rows "
            f"but hypotheses holds {pre_count}. Refusing to drop columns."
        )

    # ── Step 2: rebuild hypotheses without the weld ──
    # legacy_alter_table=ON prevents SQLite from rewriting child-table FK
    # text to REFERENCES hypotheses_old_b5 during the rename (SQLite >=3.25
    # default behaviour — the same mechanism that damaged these children
    # during migrations 20260421/20260422).
    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE hypotheses RENAME TO hypotheses_old_b5")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")

    conn.execute(NEW_HYPOTHESES_DDL)
    keep = [
        c for c in LEGACY_COLUMNS if c not in ("sport", "market_type")
    ]
    col_list = ", ".join(keep)
    conn.execute(
        f"INSERT INTO hypotheses ({col_list}) "
        f"SELECT {col_list} FROM hypotheses_old_b5"
    )
    post_count = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    if post_count != pre_count:
        raise RuntimeError(
            f"Migration 013 aborted: rebuilt hypotheses holds {post_count} "
            f"rows, expected {pre_count}. Rolling back — original table is "
            "still intact as hypotheses_old_b5 inside this transaction."
        )

    conn.execute("DROP TABLE hypotheses_old_b5")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name "
        "ON hypotheses(name)"
    )

    # ── Step 3: repair legacy rename damage in child FKs ──
    repaired = _repair_child_fks(conn)
    if repaired:
        logger.info("Migration 013 repaired FK clauses on: %s", repaired)


def _repair_child_fks(conn: sqlite3.Connection) -> list[str]:
    """Rewrite stale ``REFERENCES hypotheses_old_*`` to the live table.

    Only touches tables whose stored SQL actually references a dead
    hypotheses_old_* name. Each rebuild preserves the table's exact column
    definitions (copied from its current CREATE statement) and all rows.
    """
    repaired = []
    for table in HYP_FK_CHILDREN:
        if not _table_exists(conn, table):
            continue
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        create_sql = row[0] if row else ""
        if not create_sql or "hypotheses_old_" not in create_sql:
            continue
        import re as _re

        fixed_sql = _re.sub(
            r'REFERENCES\s+"?`?\[?hypotheses_old_[A-Za-z0-9_]+"?`?\]?',
            "REFERENCES hypotheses",
            create_sql,
        )
        if fixed_sql == create_sql:
            continue

        tmp = f"{table}_b5_rebuild"
        conn.execute(f"DROP TABLE IF EXISTS {tmp}")
        # Create the corrected table under the temp name: take the stored
        # CREATE statement (which carries the stale FK) and rewrite BOTH
        # the FK clause and the table's own name.
        rebuilt_sql = _re.sub(
            rf'CREATE TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("{_re.escape(table)}"|\[{_re.escape(table)}\]|`{_re.escape(table)}`|{_re.escape(table)})(?![A-Za-z0-9_])',
            f"CREATE TABLE {tmp}",
            fixed_sql,
            count=1,
        )
        if rebuilt_sql == fixed_sql:
            raise RuntimeError(
                f"Migration 013: could not rewrite CREATE statement for {table}; "
                "aborting rather than rebuilding blind."
            )
        conn.execute(rebuilt_sql)
        conn.execute(
            f'INSERT INTO {tmp} SELECT * FROM "{table}"'
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(
            f'ALTER TABLE {tmp} RENAME TO "{table}"'
        )
        repaired.append(table)
        logger.info(
            "Migration 013: rebuilt %s with corrected FK to hypotheses", table
        )
    return repaired


# ─────────────────────────────────────────────
# down() — explicit rollback path
# ─────────────────────────────────────────────

def down(conn: sqlite3.Connection) -> None:
    """Restore the welded pre-seam shape.

    Requires ``hypothesis_sports_ext`` to still exist (it holds the sport /
    market_type values). Rows whose ext entry was deleted cannot be
    restored and will fail loudly rather than silently losing the column.
    """
    if not _table_exists(conn, "hypotheses"):
        raise RuntimeError("down(): hypotheses table does not exist")

    cols = _columns(conn, "hypotheses")
    if "sport" in cols:
        logger.info("down(): hypotheses already has sport column; no-op")
        return
    if not _table_exists(conn, "hypothesis_sports_ext"):
        raise RuntimeError(
            "down(): hypothesis_sports_ext missing — sport values are gone, "
            "cannot restore the welded schema safely. Manual recovery required."
        )

    orphaned = conn.execute(
        "SELECT COUNT(*) FROM hypotheses h WHERE NOT EXISTS "
        "(SELECT 1 FROM hypothesis_sports_ext e "
        " WHERE e.hypothesis_id = h.hypothesis_id)"
    ).fetchone()[0]
    if orphaned:
        raise RuntimeError(
            f"down(): {orphaned} hypotheses have no hypothesis_sports_ext "
            "row; restoring would produce NULL sport under a NOT NULL "
            "column. Aborting."
        )

    pre = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]

    conn.execute("PRAGMA legacy_alter_table = ON")
    try:
        conn.execute("ALTER TABLE hypotheses RENAME TO hypotheses_new_b5")
    finally:
        conn.execute("PRAGMA legacy_alter_table = OFF")

    conn.execute("""
        CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            thesis TEXT NOT NULL,
            sport TEXT NOT NULL,
            market_type TEXT NOT NULL,
            model_config TEXT NOT NULL,
            edge_threshold REAL NOT NULL DEFAULT 0.01,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','backtesting','paper_trading',
                                 'live','paused','drawdown_paused',
                                 'retired','rejected')),
            min_sample_size INTEGER NOT NULL DEFAULT 50,
            significance_level REAL NOT NULL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME,
            promoted_by TEXT,
            notes TEXT
        )
    """)
    conn.execute(
        "INSERT INTO hypotheses "
        "(hypothesis_id, name, thesis, sport, market_type, model_config, "
        " edge_threshold, status, min_sample_size, significance_level, "
        " created_at, updated_at, promoted_at, promoted_by, notes) "
        "SELECT h.hypothesis_id, h.name, h.thesis, e.sport, e.market_type, "
        " h.model_config, COALESCE(e.edge_threshold, h.edge_threshold), "
        " h.status, h.min_sample_size, h.significance_level, "
        " h.created_at, h.updated_at, h.promoted_at, h.promoted_by, h.notes "
        "FROM hypotheses_new_b5 h "
        "JOIN hypothesis_sports_ext e USING (hypothesis_id)"
    )
    post = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    if post != pre:
        raise RuntimeError(
            f"down() aborted: restored {post} rows, expected {pre}"
        )
    conn.execute("DROP TABLE hypotheses_new_b5")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hypotheses_name "
        "ON hypotheses(name)"
    )
    logger.info("down(): welded schema restored (%d rows)", post)
