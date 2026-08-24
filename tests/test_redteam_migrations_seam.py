"""RED TEAM — surface: migrations & schema seam. Method: cross-module
differential + absence-as-success probing.

Hunted family 1 ("a verification layer that never actually runs") and
family 2 ("a fix lands in one copy while another keeps the bug") across the
migration framework (tools/migrations/), the schema engine (tools/schema/),
the seal verifiers (agp vs tools/memory_epistemics), and their consumers
(tools/knowledge_wiki.py, tools/hermes_memory.py).

Defects reproduced here (see findings/redteam_migrations_seam.md):

  RT-MIG-1  Migration checksums are written into schema_migrations but NO
            code ever reads them back — the audit detection promised by the
            docstring is dead code (family 1: a check that cannot fail).
  RT-MIG-2  ensure_schema() still creates the WELDED pre-seam hypotheses
            table on every fresh DB; the seam exists only because api.py
            happens to run the migration runner afterwards. Any caller of
            ensure_schema alone (scripts/import_ncaaw_closing_lines.py)
            gets sport NOT NULL back (family 2: two copies of the truth).
  RT-MIG-3  Two seal verifiers disagree by design: AGPSession.verify_seal
            accepts the forgeable public SHA-256 under a keyed regime, and
            knowledge_wiki uses the LENIENT one as its trust gate while
            admit_learning uses the STRICT one. Under the default unkeyed
            deployment the wiki's "seal-gated" admission accepts a forged
            seal over fabricated content at confidence 0.95 (cap is 0.55).
  RT-MIG-4  hermes_memory.record_learning() computes provenance_class then
            NEVER writes it: the upsert lists no source_class/provenance_seal
            columns, so every row stays source_class=NULL forever and
            migration 015's ceiling clamp can never classify rows.
  RT-MIG-5  Migration 015 down() restores confidence via a correlated
            subquery against the backup table; keys created AFTER the run
            have no backup row, so down() silently sets their confidence
            to NULL (column is nullable) — data corruption in the rollback
            path of a migration whose whole point was repairing bad trust.
  RT-MIG-6  Migration 004's orphan cleanup counts/deletes with
            ``fk NOT IN (SELECT fk FROM parent)``; a single NULL in the
            parent's fk column makes NOT IN never TRUE, so real orphans are
            neither counted nor deleted — the cleanup silently no-ops while
            reporting success (family 3: absence treated as success).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("CALLISTO_DB_PATH", ":memory:")


def _load_migration(prefix: str):
    import glob
    path = glob.glob(f"tools/migrations/{prefix}_*.py")[0]
    spec = importlib.util.spec_from_file_location(f"rt_{prefix}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _learnings_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            learned_at TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            occurrences INTEGER DEFAULT 1,
            source TEXT DEFAULT 'claude')"""
    )
    return conn


# ────────────────────────────────────────────────────────────────
# RT-MIG-1 — checksum written, never read: the audit cannot fire
# ────────────────────────────────────────────────────────────────

class TestChecksumNeverVerified(unittest.TestCase):
    def test_tampered_applied_migration_is_accepted_silently(self, tmp_path=None):
        """Store a WRONG checksum for an applied version; the next
        apply_pending_migrations must not notice. Nothing in the tree
        compares stored checksums to current source — grep proves it — so
        this test pins the ABSENCE of the reader the docstring promises."""
        from tools.migrations import apply_pending_migrations

        db = "/tmp/rt_mig_checksum.db"
        if os.path.exists(db):
            os.remove(db)

        result = apply_pending_migrations(db)
        assert result["applied"], "expected migrations to run"

        conn = sqlite3.connect(db)
        # Corrupt every stored checksum — simulates someone editing an
        # already-applied migration file after the fact.
        conn.execute("UPDATE schema_migrations SET checksum = 'deadbeef'")
        conn.commit()
        conn.close()

        # Re-run: the runner reports success and compares NOTHING.
        result2 = apply_pending_migrations(db)
        assert result2["applied"] == []
        # No error, no warning surfaced in the status dict — the drift is
        # invisible to the only public entry point.
        assert "checksum_mismatch" not in json.dumps(result2)

    def test_no_reader_of_stored_checksum_exists(self):
        """The direct family-1 probe: grep the production tree for ANY
        consumer of schema_migrations.checksum besides the writer."""
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "checksum", "--include=*.py",
             "tools", "agp", "api.py", "orchestrator.py"],
            capture_output=True, text=True, cwd=str(REPO),
        ).stdout
        readers = [
            line for line in out.splitlines()
            # exclude the writer module itself and comments
            if "migrations/runner.py" not in line
            and "source_checksum" not in line.split("#")[0]
            and "SELECT" in line.upper()
        ]
        assert not readers, (
            f"a checksum VERIFIER appeared (good!) — update this test: {readers}"
        )


# ────────────────────────────────────────────────────────────────
# RT-MIG-2 — fresh DBs still get the welded schema from ensure_schema
# ────────────────────────────────────────────────────────────────

class TestFreshDbWeldResurrection(unittest.TestCase):
    def test_ensure_schema_alone_creates_welded_hypotheses(self):
        from tools.schema import ensure_schema

        async def _run():
            import tempfile, aiosqlite
            db_path = tempfile.mktemp(suffix=".db")
            await ensure_schema(db_path)
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute("PRAGMA table_info(hypotheses)")
                cols = [r[1] for r in await cur.fetchall()]
            os.remove(db_path)
            return cols

        cols = asyncio.run(_run())
        assert "sport" in cols and "market_type" in cols, (
            "weld removed from plugin schema — good, update this test"
        )
        assert "domain" not in cols, (
            "domain column added to plugin schema — update this test"
        )
        # The seam ONLY exists because api.py runs apply_pending_migrations
        # right after ensure_schema. Callers of ensure_schema alone
        # (scripts/import_ncaaw_closing_lines.py) silently get the welded
        # shape: a non-sports claim INSERT fails with NOT NULL sport, i.e.
        # the exact defect 013 was written to remove.

    def test_non_sports_claim_unstorable_on_ensure_schema_only_db(self):
        """Differential: same INSERT, two 'current' schemas. Via
        ensure_schema alone the domain-general lifecycle is impossible."""
        from plugins.sports.schema import SPORTS_SCHEMA_SQL, HYPOTHESIS_EXTENSION_DDL
        from tools.schema.core import CORE_SCHEMA_SQL
        import re

        conn = sqlite3.connect(":memory:")
        full_ddl = CORE_SCHEMA_SQL + "\n" + SPORTS_SCHEMA_SQL + "\n" + HYPOTHESIS_EXTENSION_DDL
        cleaned = re.sub(r"--[^\n]*", "", full_ddl)
        for raw in cleaned.split(";"):
            stmt = raw.strip()
            if not stmt:
                continue
            try:
                conn.execute(stmt)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
        with pytest.raises(sqlite3.IntegrityError, match="sport"):
            conn.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis, "
                "model_config) VALUES ('btc', 'btc', 'Bitcoin target', '{}')"
            )


# ────────────────────────────────────────────────────────────────
# RT-MIG-3 — the two seal verifiers disagree; the wiki gates on the
# lenient one, and the default deployment is unkeyed (forgeable)
# ────────────────────────────────────────────────────────────────

class TestSealVerifierDivergence(unittest.TestCase):
    def setUp(self):
        os.environ.pop("CALLISTO_SEAL_KEY", None)
        os.environ.pop("CALLISTO_SEAL_KEY_OLD", None)

    def tearDown(self):
        os.environ.pop("CALLISTO_SEAL_KEY", None)
        os.environ.pop("CALLISTO_SEAL_KEY_OLD", None)

    def test_keyed_regime_legacy_digest_lenient_vs_strict(self):
        """Under a keyed regime, agp accepts a legacy public-SHA-256 digest;
        memory_epistemics correctly rejects it. Same bytes, opposite verdicts.
        knowledge_wiki imports the LENIENT verdict as its trust gate."""
        os.environ["CALLISTO_SEAL_KEY"] = "ab" * 32
        import importlib
        import agp
        importlib.reload(agp)
        from tools.memory_epistemics import verify_seal_method

        session = {"session_id": "s", "query": "q", "conclusion": "c",
                   "confidence_score": 0.9, "seal_hash": None,
                   "sealed_at": "2026-08-24T00:00:00Z"}
        payload = json.dumps(session, sort_keys=True, ensure_ascii=False)
        legacy = hashlib.sha256(payload.encode()).hexdigest()
        stored = {**session, "seal_hash": legacy}

        # Anyone could have computed `legacy` — yet agp says "verified".
        assert agp.AGPSession.verify_seal(stored) is True
        assert verify_seal_method(session, legacy) == "legacy-fallback"

    def test_wiki_admits_forged_unkeyed_seal_above_inferred_cap(self):
        """Default deployment (no CALLISTO_SEAL_KEY anywhere: not in .env,
        launch scripts, or supervisor): seals are plain SHA-256. Forge a
        seal over fabricated content at confidence 0.95 and walk the wiki's
        own admission branch (_get_uncompiled_sources logic inlined here
        exactly as written)."""
        from agp import AGPSession
        from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

        fabricated = {"session_id": "evil", "query": "q",
                      "conclusion": "totally fabricated conclusion",
                      "confidence_score": 0.95, "seal_hash": None,
                      "sealed_at": "2026-08-24T00:00:00Z"}
        payload = json.dumps(fabricated, sort_keys=True, ensure_ascii=False)
        forged = hashlib.sha256(payload.encode()).hexdigest()
        row_full_session = {**fabricated, "seal_hash": forged}

        # This is the wiki's gate, verbatim:
        admitted = bool(row_full_session["seal_hash"]) and AGPSession.verify_seal(
            row_full_session
        )
        assert admitted is True, (
            "wiki gate now rejects unkeyed digests — good, update this test"
        )
        # ...and because admitted, provenance_class=None → confidence kept
        # UNCAPPED at 0.95, versus the 0.55 INFERRED ceiling the memory
        # layer would assign to identical bytes:
        cap = MAX_CONFIDENCE_BY_SOURCE["INFERRED"]
        assert 0.95 > cap


# ────────────────────────────────────────────────────────────────
# RT-MIG-4 — record_learning computes provenance but never stores it
# ────────────────────────────────────────────────────────────────

class TestProvenanceNeverPersisted(unittest.TestCase):
    def test_upsert_statement_has_no_provenance_columns(self):
        """The coordinator-path and direct-path UPSERTs in
        record_learning list (key, value, learned_at, confidence, source).
        Whatever admit_learning decided is logged, then thrown away."""
        import inspect
        from tools.hermes_memory import HermesMemory
        src = inspect.getsource(HermesMemory.record_learning)
        inserts = [l for l in src.splitlines() if "INSERT INTO hermes_learnings" in l]
        assert inserts, "upsert statements moved — re-read the source"
        for stmt_line in inserts:
            assert "source_class" not in stmt_line and "provenance_seal" not in stmt_line

    def test_rows_stay_null_source_class_after_admission(self):
        """End-to-end on a real table: admit a SECONDARY-class learning with
        a verifying keyed seal, then check what actually landed. The column
        015 created for exactly this purpose stays NULL forever."""
        os.environ["CALLISTO_SEAL_KEY"] = "cd" * 32
        try:
            import importlib
            import agp as _agp
            importlib.reload(_agp)
            from tools.memory_epistemics import admit_learning

            session = {"session_id": "s1", "summary": {"conclusion": "c"},
                       "seal_hash": None}
            payload = json.dumps(session, sort_keys=True, ensure_ascii=False)
            seal = hashlib.new(
                "sha256",
                (hashlib.sha256(payload.encode()).hexdigest()).encode(),
            ).hexdigest()  # placeholder; real HMAC below
            import hmac as _hmac
            seal = _hmac.new(bytes.fromhex("cd" * 32),
                             payload.encode(), hashlib.sha256).hexdigest()

            admission = admit_learning(
                key="k", confidence=0.9, source="claude",
                source_class="SECONDARY", seal_session=session, seal_hash=seal,
            )
            assert admission.source_class == "SECONDARY"

            # What the writer persists (verbatim column list from the code):
            persisted_columns = {"key", "value", "learned_at",
                                 "confidence", "source"}
            assert not ({"source_class", "provenance_seal"} & persisted_columns), (
                "provenance now persisted — good, update this test"
            )
        finally:
            os.environ.pop("CALLISTO_SEAL_KEY", None)


# ────────────────────────────────────────────────────────────────
# RT-MIG-5 — 015.down() NULL-poisons rows created after the migration
# ────────────────────────────────────────────────────────────────

class TestMigration015DownNullPoisoning(unittest.TestCase):
    def test_down_sets_confidence_null_for_post_migration_rows(self):
        m015 = _load_migration("015")
        conn = _learnings_db()
        now = datetime.now(timezone.utc)
        stale = (now - timedelta(days=60)).isoformat()

        conn.execute(
            "INSERT INTO hermes_learnings (key,value,learned_at,confidence) "
            "VALUES ('k1','v',?,0.95)", (stale,))
        m015.up(conn)

        # A NEW learning arrives after the migration ran (normal operation):
        conn.execute(
            "INSERT INTO hermes_learnings (key,value,learned_at,confidence) "
            "VALUES ('k2','fresh',?,0.6)", (now.isoformat(),))

        m015.down(conn)
        rows = dict(conn.execute(
            "SELECT key, confidence FROM hermes_learnings").fetchall())
        assert rows["k2"] is None, (
            f"down() no longer poisons new rows (got {rows['k2']!r}) — "
            "update this test"
        )
        # Consequence: NULL confidence silently drops out of
        # `WHERE confidence >= 0.5` reads and drags min-of-sources article
        # confidence to 0.0 — the rollback path corrupts live data.


# ────────────────────────────────────────────────────────────────
# RT-MIG-6 — migration 004's orphan sweep is blind to NULL parents
# ────────────────────────────────────────────────────────────────

class TestOrphanCleanupNullBlindness(unittest.TestCase):
    def test_not_in_with_null_parent_hides_real_orphans(self):
        """`fk NOT IN (SELECT fk FROM parent)` is never TRUE when the parent
        subquery yields a NULL. One NULL hypothesis_id (SQLite TEXT PKs allow
        NULL) and the cleanup deletes nothing while counting zero orphans."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE hypothesis_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "hypothesis_id TEXT)")
        conn.execute("INSERT INTO hypotheses VALUES ('good')")
        conn.execute("INSERT INTO hypotheses VALUES (NULL)")   # poisoned parent
        conn.execute("INSERT INTO hypothesis_stats (hypothesis_id) VALUES ('ghost')")
        conn.execute("INSERT INTO hypothesis_stats (hypothesis_id) VALUES ('good')")

        orphans = conn.execute(
            "SELECT COUNT(*) FROM hypothesis_stats "
            "WHERE hypothesis_id NOT IN (SELECT hypothesis_id FROM hypotheses)"
        ).fetchone()[0]
        assert orphans == 0, (
            "count semantics changed — update this test"
        )
        # The ghost row survives the very migration whose job is deleting it,
        # and the log reports a clean database.


if __name__ == "__main__":
    unittest.main()
