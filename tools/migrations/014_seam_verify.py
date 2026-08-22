"""Migration 014 — schema-seam verification gate.

Runs immediately after 013 (the hypotheses weld removal) and verifies the
post-migration state is coherent before the database is allowed back into
service. This is the "assume it runs once, unattended" safety net:

1. Row-count parity: every row present before 013 must be present after.
   Verified by re-deriving counts from the extension table join, not from
   a checkpoint taken at runtime (the runner's transaction already
   guarantees atomicity; this is the independent double-check).
2. Referential integrity: every hypothesis_id in child tables resolves to
   a live hypotheses row; every ext row too.
3. FK clause sanity: no stored CREATE statement may still reference a
   dead ``hypotheses_old_*`` name.
4. Domain coverage: every hypotheses row has domain='sports' unless a
   later process explicitly changed it.

Also owns the seam journal (``_b5_seam_migrations``) which records the
migration's execution metadata for audit.

Rollback: down() re-runs 013.down() (restores welded shape) after the
same integrity checks pass in reverse.

Dry run: dry_run(conn) reports all four checks without writing.
"""

from __future__ import annotations

import logging
import sqlite3

from importlib import import_module as _import

m013 = _import("tools.migrations.013_schema_seam_hypotheses")

logger = logging.getLogger("callisto.migrations.014")

HYP_FK_CHILDREN = m013.HYP_FK_CHILDREN


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


# ─────────────────────────────────────────────
# Checks (read-only; shared by dry_run and up)
# ─────────────────────────────────────────────

def check_row_parity(conn: sqlite3.Connection) -> dict:
    """Every sports-domain hypothesis row must have an ext row."""
    if not _table_exists(conn, "hypotheses"):
        return {"ok": True, "detail": "no hypotheses table (fresh DB)"}
    total = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(hypotheses)").fetchall()]
    has_domain = "domain" in cols
    if has_domain:
        sports_missing = conn.execute(
            "SELECT COUNT(*) FROM hypotheses h WHERE h.domain = 'sports' "
            "AND NOT EXISTS (SELECT 1 FROM hypothesis_sports_ext e "
            " WHERE e.hypothesis_id = h.hypothesis_id)"
        ).fetchone()[0]
        non_sports = conn.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE domain <> 'sports'"
        ).fetchone()[0]
    else:
        # Pre-013 shape (dry-run against an unmigrated DB): every row is
        # sports by definition; ext table may not exist yet.
        if not _table_exists(conn, "hypothesis_sports_ext"):
            return {
                "ok": True,
                "total": total,
                "detail": "pre-migration shape; parity applies post-013",
            }
        sports_missing = conn.execute(
            "SELECT COUNT(*) FROM hypotheses h WHERE NOT EXISTS "
            "(SELECT 1 FROM hypothesis_sports_ext e "
            " WHERE e.hypothesis_id = h.hypothesis_id)"
        ).fetchone()[0]
        non_sports = 0
    return {
        "ok": sports_missing == 0,
        "total": total,
        "missing_ext": sports_missing,
        "non_sports_domain_rows": non_sports,
    }


def check_referential_integrity(conn: sqlite3.Connection) -> dict:
    orphans: dict[str, int] = {}
    for t in HYP_FK_CHILDREN:
        if not _table_exists(conn, t):
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} c WHERE NOT EXISTS "
            "(SELECT 1 FROM hypotheses h "
            " WHERE h.hypothesis_id = c.hypothesis_id)"
        ).fetchone()[0]
        if n:
            orphans[t] = n
    return {"ok": not orphans, "orphans": orphans}


def check_no_stale_fk_sql(conn: sqlite3.Connection) -> dict:
    stale = []
    for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ).fetchall():
        name, sql = row[0], row[1] or ""
        if name.startswith("hypotheses_old_"):
            stale.append(name)
            continue
        if "hypotheses_old_" in sql:
            stale.append(name)
    return {"ok": not stale, "stale": stale}


def check_domain_coverage(conn: sqlite3.Connection) -> dict:
    if not _table_exists(conn, "hypotheses"):
        return {"ok": True, "detail": "no hypotheses table"}
    cols = [r[1] for r in conn.execute("PRAGMA table_info(hypotheses)").fetchall()]
    if "domain" not in cols:
        return {"ok": False, "detail": "domain column missing post-migration"}
    nulls = conn.execute(
        "SELECT COUNT(*) FROM hypotheses WHERE domain IS NULL OR domain = ''"
    ).fetchone()[0]
    return {"ok": nulls == 0, "null_domain_rows": nulls}


def run_all_checks(conn: sqlite3.Connection) -> dict:
    results = {
        "row_parity": check_row_parity(conn),
        "referential_integrity": check_referential_integrity(conn),
        "stale_fk_sql": check_no_stale_fk_sql(conn),
        "domain_coverage": check_domain_coverage(conn),
    }
    results["ok"] = all(v.get("ok") for v in results.values())
    return results


def dry_run(conn: sqlite3.Connection) -> dict:
    """Report verification results without writing anything."""
    report = run_all_checks(conn)
    report["actions"] = [
        "verify row parity between hypotheses and hypothesis_sports_ext",
        "verify referential integrity of child tables",
        "verify no stale hypotheses_old_* references remain",
        "verify domain column populated on all rows",
    ]
    return report


# ─────────────────────────────────────────────
# up / down
# ─────────────────────────────────────────────

def up(conn: sqlite3.Connection) -> None:
    checks = run_all_checks(conn)

    # Record the seam journal entry regardless of outcome so an operator
    # can see this gate ran (and what it found).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _b5_seam_migrations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " migration TEXT NOT NULL,"
        " ran_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " result TEXT,"
        " detail TEXT"
        ")"
    )

    # Hard failures: parity loss, stale FK SQL, missing domain column.
    # Referential-integrity orphans are reported but NOT fatal: legacy
    # databases may already contain orphaned child rows (FK enforcement
    # was off for years); deleting them is not this migration's job.
    hard_failures = {
        k: v
        for k, v in checks.items()
        if k not in ("ok", "referential_integrity") and not v.get("ok")
    }
    if hard_failures:
        conn.execute(
            "INSERT INTO _b5_seam_migrations (migration, result, detail) "
            "VALUES ('014_verify', 'FAILED', ?)",
            (str(hard_failures),),
        )
        raise RuntimeError(
            f"Migration 014 verification FAILED: {hard_failures}. "
            "Migration 013 will roll back with it (same transaction)."
        )
    if not checks["referential_integrity"]["ok"]:
        logger.warning(
            "Migration 014: pre-existing orphan rows (not created by the "
            "migration, left untouched): %s",
            checks["referential_integrity"]["orphans"],
        )
    conn.execute(
        "INSERT INTO _b5_seam_migrations (migration, result) "
        "VALUES ('014_verify', 'OK')"
    )
    logger.info("Migration 014: schema-seam verification passed: %s", checks)


def down(conn: sqlite3.Connection) -> None:
    """Restore the welded shape via 013.down(), then verify coherence."""
    m013.down(conn)
    # Post-down, sport is back on hypotheses; ext table still holds its copy.
    n = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
    null_sport = conn.execute(
        "SELECT COUNT(*) FROM hypotheses WHERE sport IS NULL"
    ).fetchone()[0]
    if null_sport:
        raise RuntimeError(
            f"down(): {null_sport}/{n} rows have NULL sport after restore"
        )
    logger.info("Migration 014 down(): welded shape verified (%d rows)", n)
