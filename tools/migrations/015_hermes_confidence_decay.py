"""Migration 015 — reset hermes learning confidences contaminated by the
MAX-ratchet, and add provenance columns for ceiling enforcement.

Problem (findings/instance4.md P3, the "trust escalator")
---------------------------------------------------------
``hermes_learnings`` was upserted with
``confidence = MAX(confidence, excluded.confidence)``: a learning's stored
confidence could never fall. One optimistic model self-report permanently
contaminated its key; the knowledge wiki then admitted anything >= 0.5 as a
compile source and reinjected it as prompt prior. record_learning now REPLACES
confidence and clamps to provenance ceilings (tools/memory_epistemics.py) —
but rows WRITTEN under the ratchet carry inflated values that no new write
will ever touch.

What this migration does
------------------------
1. Adds two nullable columns to ``hermes_learnings``:
     - ``source_class TEXT``      (PRIMARY/SECONDARY/SIGNAL/INFERRED; NULL is
       read as INFERRED)
     - ``provenance_seal TEXT``   (seal hash of the session the learning was
       derived from, when one exists)
2. Clamps every row's confidence to its provenance class ceiling: rows with
   source_class NULL/INFERRED are capped at 0.55, SECONDARY at 0.75,
   SIGNAL at 0.55, PRIMARY at 1.0 (same values as agp.thresholds).
3. Applies one decay pass to stale rows: any row not re-observed in over
   CONFIDENCE_HALF_LIFE_DAYS (14) days has its confidence halved once per
   elapsed half-life, floored at 0.05 — matching tools/memory_epistemics
   read-time decay so stored and effective values converge.
4. Records what it changed in the migration log output (dry_run reports the
   same counts before any write).

Safety properties (pattern of tools/migrations/013_schema_seam_hypotheses.py)
-----------------------------------------------------------------------------
* Dry-run first: ``dry_run(conn)`` reports affected-row counts with zero writes.
* Runs inside the runner's per-migration transaction: atomic, crash-safe.
* Idempotent: column adds are guarded; the clamp and decay are recomputed
  safely if re-executed (they are functions of current state).
* Reversible: ``down(conn)`` restores pre-migration confidence values from
  the ``hermes_learnings_conf_backup_015`` backup table and drops the added
  columns. The backup table itself is left in place by down() as an audit
  trail (dropping data destroys evidence; keeping it costs ~nothing).

NOT run against any real database here per build rules — requires operator
sign-off on the workstation DB.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("callisto.migrations.015")

# Keep in sync with agp/thresholds.MAX_CONFIDENCE_BY_SOURCE and
# tools/memory_epistemics.PROVENANCE_CEILINGS (both pinned by tests).
CEILINGS = {
    None: 0.55,
    "INFERRED": 0.55,
    "SIGNAL": 0.55,
    "SECONDARY": 0.75,
    "PRIMARY": 1.0,
}
HALF_LIFE_DAYS = 14.0
FLOOR = 0.05


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _decay(conf: float, learned_at: str, now: datetime | None = None) -> float:
    """One stored-decay pass matching memory_epistemics.decay_confidence."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(str(learned_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return max(FLOOR, min(1.0, float(conf)))
    decayed = float(conf) * math.pow(0.5, age_days / HALF_LIFE_DAYS)
    return round(max(FLOOR, min(1.0, decayed)), 4)


def _needs_migration(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "hermes_learnings"):
        return False
    cols = _columns(conn, "hermes_learnings")
    return "source_class" not in cols or "provenance_seal" not in cols


def dry_run(conn: sqlite3.Connection) -> dict:
    """Report exactly what would change. Read-only; safe anywhere."""
    report: dict = {"needed": True, "actions": []}
    if not _table_exists(conn, "hermes_learnings"):
        report["needed"] = False
        report["actions"].append("no-op: hermes_learnings table absent")
        return report

    cols = _columns(conn, "hermes_learnings")
    n = conn.execute("SELECT COUNT(*) FROM hermes_learnings").fetchone()[0]
    report["learning_rows"] = n

    if "source_class" not in cols:
        report["actions"].append(
            "ADD COLUMN source_class TEXT; ADD COLUMN provenance_seal TEXT"
        )
    # Clamp preview: how many rows exceed their ceiling?
    clamp_needed = 0
    for cls_val, ceiling in CEILINGS.items():
        if cls_val is None:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM hermes_learnings "
                "WHERE (source_class IS NULL OR source_class NOT IN "
                "('PRIMARY','SECONDARY','SIGNAL','INFERRED')) AND confidence > ?",
                (ceiling,),
            ).fetchone()[0]
        else:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM hermes_learnings "
                "WHERE source_class = ? AND confidence > ?",
                (cls_val, ceiling),
            ).fetchone()[0]
        clamp_needed += cnt
    if clamp_needed:
        report["actions"].append(f"clamp {clamp_needed} row(s) above provenance ceilings")
    report["rows_over_ceiling"] = clamp_needed
    report["actions"].append(
        f"apply one {HALF_LIFE_DAYS:.0f}-day half-life decay pass to stale rows"
    )
    return report


def up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "hermes_learnings"):
        logger.info("Migration 015: hermes_learnings absent; nothing to do")
        return
    cols = _columns(conn, "hermes_learnings")

    # ── Step 1: provenance columns ──
    if "source_class" not in cols:
        conn.execute("ALTER TABLE hermes_learnings ADD COLUMN source_class TEXT")
    if "provenance_seal" not in cols:
        conn.execute("ALTER TABLE hermes_learnings ADD COLUMN provenance_seal TEXT")

    # ── Step 2: backup original confidences (reversibility) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hermes_learnings_conf_backup_015 (
            key TEXT PRIMARY KEY,
            confidence REAL NOT NULL,
            backed_up_at TEXT NOT NULL
        )
    """)
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO hermes_learnings_conf_backup_015 "
        "(key, confidence, backed_up_at) SELECT key, confidence, ? "
        "FROM hermes_learnings",
        (now_iso,),
    )

    # ── Step 3: clamp to provenance ceilings ──
    clamped = 0
    for cls_val, ceiling in CEILINGS.items():
        if cls_val is None:
            cur = conn.execute(
                "UPDATE hermes_learnings SET confidence = ? "
                "WHERE (source_class IS NULL OR source_class NOT IN "
                "('PRIMARY','SECONDARY','SIGNAL','INFERRED')) AND confidence > ?",
                (ceiling, ceiling),
            )
        else:
            cur = conn.execute(
                "UPDATE hermes_learnings SET confidence = ? "
                "WHERE source_class = ? AND confidence > ?",
                (ceiling, cls_val, ceiling),
            )
        clamped += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # ── Step 4: one decay pass ──
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT key, confidence, learned_at FROM hermes_learnings"
    ).fetchall()
    decayed = 0
    for key, conf, learned_at in rows:
        target = _decay(conf, learned_at, now)
        if abs(target - float(conf)) >= 0.005:
            conn.execute(
                "UPDATE hermes_learnings SET confidence = ? WHERE key = ?",
                (target, key),
            )
            decayed += 1

    logger.info(
        "Migration 015 complete: %d row(s) clamped to ceilings, %d decayed",
        clamped, decayed,
    )


def down(conn: sqlite3.Connection) -> None:
    """Restore pre-migration confidences from backup and drop added columns."""
    if not _table_exists(conn, "hermes_learnings"):
        return
    backup = "hermes_learnings_conf_backup_015"
    if _table_exists(conn, backup):
        conn.execute(
            f"UPDATE hermes_learnings SET confidence = (" 
            f"SELECT b.confidence FROM {backup} b WHERE b.key = hermes_learnings.key)"
        )
        logger.info("Migration 015 down(): confidences restored from %s", backup)

    cols = _columns(conn, "hermes_learnings")
    if "source_class" in cols or "provenance_seal" in cols:
        # Column drops require a table rebuild in older SQLite; use the
        # ALTER DROP COLUMN path when available, else rebuild verbatim.
        version = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
        keep = [c for c in cols if c not in ("source_class", "provenance_seal")]
        if version >= (3, 35):
            if "source_class" in cols:
                conn.execute("ALTER TABLE hermes_learnings DROP COLUMN source_class")
            if "provenance_seal" in cols:
                conn.execute("ALTER TABLE hermes_learnings DROP COLUMN provenance_seal")
        else:
            conn.execute("PRAGMA legacy_alter_table = ON")
            try:
                conn.execute("ALTER TABLE hermes_learnings RENAME TO hermes_learnings_old_015")
            finally:
                conn.execute("PRAGMA legacy_alter_table = OFF")
            create_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='hermes_learnings_old_015'"
            ).fetchone()[0]
            rebuilt = create_sql.replace("hermes_learnings_old_015", "hermes_learnings")
            for dropped in ("source_class", "provenance_seal"):
                import re as _re
                rebuilt = _re.sub(
                    rf",?\s*{dropped}\s+TEXT[^,)]*", "", rebuilt, count=1
                )
            conn.execute(rebuilt)
            col_list = ", ".join(keep)
            conn.execute(
                f"INSERT INTO hermes_learnings ({col_list}) "
                f"SELECT {col_list} FROM hermes_learnings_old_015"
            )
            conn.execute("DROP TABLE hermes_learnings_old_015")
        logger.info("Migration 015 down(): provenance columns dropped")

    # NOTE: the backup table is deliberately KEPT by down() — dropping data
    # destroys the audit trail; it is tiny and inert.
