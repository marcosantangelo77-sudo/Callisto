"""Improve run: memory & wiki layer — the admission decision must survive to disk.

Two defects fixed (findings/improve_memory_wiki.md):

1. clamp_to_ceiling rounded UP across tier boundaries (round(0.5497, 3) ==
   0.55) — the exact bug class agp.thresholds.floor_conf exists to kill,
   present in the one clamp every learning write passes through.

2. record_learning ran admit_learning (seal verification, ceiling clamps)
   but its INSERT wrote only key/value/learned_at/confidence/source —
   source_class and provenance_seal were decided and then discarded, so
   the wiki's compile path could not distinguish a seal-verified SECONDARY
   learning from an anonymous guess. The read side now gates on what the
   write side persists.

Offline only; synthetic DBs; no live API.
"""

import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("CALLISTO_DB_PATH", ":memory:")

from tools.memory_epistemics import (
    PROVENANCE_CEILINGS,
    admit_learning,
    clamp_to_ceiling,
)


class TestClampNeverRaises(unittest.TestCase):
    """A clamp may only move a score DOWN. Random sweep across all classes."""

    def test_floor_at_boundaries(self):
        self.assertEqual(clamp_to_ceiling(0.5497, "INFERRED"), 0.5497)
        self.assertEqual(clamp_to_ceiling(0.7499, "SECONDARY"), 0.7499)
        self.assertEqual(clamp_to_ceiling(0.99999, "INFERRED"), PROVENANCE_CEILINGS["INFERRED"])
        # never above ceiling
        for cls, ceiling in PROVENANCE_CEILINGS.items():
            self.assertLessEqual(clamp_to_ceiling(1.0, cls), ceiling)

    def test_property_never_increases(self):
        import random
        rng = random.Random(20260823)
        classes = list(PROVENANCE_CEILINGS) + [None, "BOGUS"]
        for _ in range(2000):
            raw = rng.random()
            cls = rng.choice(classes)
            out = clamp_to_ceiling(raw, cls)
            self.assertLessEqual(out, raw, f"clamp RAISED {raw} -> {out} ({cls})")
            self.assertGreaterEqual(out, 0.0)

    def test_admit_learning_uses_downward_clamp(self):
        a = admit_learning(key="k", confidence=0.5497, source="claude",
                           source_class="INFERRED")
        self.assertEqual(a.stored_confidence, 0.5497)


def _make_db(tmp_path, with_provenance_cols: bool):
    import sqlite3
    db_path = str(Path(tmp_path) / ("wiki_" + str(with_provenance_cols) + ".db"))
    conn = sqlite3.connect(db_path)
    extra = ", source_class TEXT, provenance_seal TEXT" if with_provenance_cols else ""
    conn.execute(
        "CREATE TABLE hermes_learnings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "key TEXT NOT NULL UNIQUE, value TEXT NOT NULL, learned_at TEXT NOT NULL, "
        "confidence REAL DEFAULT 0.5, occurrences INTEGER DEFAULT 1, "
        "source TEXT DEFAULT 'claude'" + extra + ")"
    )
    # minimal compile-log + sessions tables so _get_uncompiled_sources can run
    conn.execute(
        "CREATE TABLE wiki_compile_log (cycle INTEGER, articles_created INTEGER, "
        "articles_updated INTEGER, duration_seconds REAL, compiled_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, query TEXT, domain TEXT, "
        "conclusion TEXT, confidence_score REAL, sealed_at TEXT, full_session TEXT, "
        "seal_hash TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalogue (entry_id TEXT PRIMARY KEY, content TEXT, domain TEXT, "
        "confidence_score REAL, source_name TEXT, created_at TEXT)"
    )
    conn.commit()
    return db_path


class TestWritePersistsProvenance(unittest.TestCase):
    def setUp(self):
        import asyncio, tempfile
        self.dir = tempfile.mkdtemp()
        from tools.hermes_memory import HermesMemory
        self.hm = HermesMemory(db_path=_make_db(self.dir, True))

    def test_class_and_seal_persisted(self):
        import asyncio
        asyncio.run(self.hm.record_learning(
            "some_key", "a claim", confidence=0.6,
            source="claude", source_class="SECONDARY",  # no seal -> INFERRED
        ))
        import sqlite3
        row = sqlite3.connect(self.hm.db_path).execute(
            "SELECT confidence, source_class, provenance_seal FROM hermes_learnings"
        ).fetchone()
        conf, cls, seal = row
        self.assertEqual(conf, PROVENANCE_CEILINGS["INFERRED"])  # capped
        self.assertEqual(cls, "INFERRED")                        # collapsed, PERSISTED
        self.assertIsNone(seal)

    def test_upsert_updates_provenance_columns(self):
        import asyncio, sqlite3
        asyncio.run(self.hm.record_learning("k1", "v1", confidence=0.4))
        asyncio.run(self.hm.record_learning("k1", "v2", confidence=0.3))
        occ, val = sqlite3.connect(self.hm.db_path).execute(
            "SELECT occurrences, value FROM hermes_learnings WHERE key='k1'"
        ).fetchone()
        self.assertEqual(occ, 2)
        self.assertEqual(val, "v2")


class TestWikiReadGatesProvenance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()

    async def test_unsealed_secondary_claim_is_rejected_from_compile(self):
        import aiosqlite
        from tools.knowledge_wiki import KnowledgeWiki
        dbp = _make_db(self.dir, True)
        async with aiosqlite.connect(dbp) as db:
            await db.execute(
                "INSERT INTO hermes_learnings (key, value, learned_at, confidence, "
                "source, source_class, provenance_seal) VALUES "
                "('forged', 'laundered claim', '2099-01-01T00:00:00', 0.7, "
                "'claude', 'SECONDARY', NULL)"
            )
            await db.commit()
            wiki = KnowledgeWiki(db_path=dbp)
            sources = await wiki._get_uncompiled_sources(db)
        self.assertFalse([s for s in sources if s["id"] == "forged"])

    async def test_sealed_secondary_claim_survives_compile(self):
        import aiosqlite
        from tools.knowledge_wiki import KnowledgeWiki
        dbp = _make_db(self.dir, True)
        async with aiosqlite.connect(dbp) as db:
            await db.execute(
                "INSERT INTO hermes_learnings (key, value, learned_at, confidence, "
                "source, source_class, provenance_seal) VALUES "
                "('earned', 'verified claim', '2099-01-01T00:00:00', 0.7, "
                "'claude', 'SECONDARY', 'deadbeef')"
            )
            await db.commit()
            wiki = KnowledgeWiki(db_path=dbp)
            sources = await wiki._get_uncompiled_sources(db)
        match = [s for s in sources if s["id"] == "earned"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["provenance_class"], "SECONDARY")

    async def test_legacy_row_treated_as_inferred_and_capped(self):
        import aiosqlite
        from tools.knowledge_wiki import KnowledgeWiki
        dbp = _make_db(self.dir, False)  # pre-migration schema
        async with aiosqlite.connect(dbp) as db:
            await db.execute(
                "INSERT INTO hermes_learnings (key, value, learned_at, confidence, source) "
                "VALUES ('old', 'legacy', '2099-01-01T00:00:00', 0.9, 'claude')"
            )
            await db.commit()
            wiki = KnowledgeWiki(db_path=dbp)
            sources = await wiki._get_uncompiled_sources(db)
        match = [s for s in sources if s["id"] == "old"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["provenance_class"], "INFERRED")
        self.assertLessEqual(match[0]["confidence"], PROVENANCE_CEILINGS["INFERRED"])


if __name__ == "__main__":
    unittest.main()
