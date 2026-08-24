"""Build pass: memory & wiki layer improvement (build/memory-wiki-improve).

Failing-first repros for four family-#1 instances found in one sweep of the
memory/wiki trust pipeline (write -> store -> read -> reinject/compile):

1. Decay computed then clobbered: _build_learnings computes
   decay_confidence() per row and hands it to annotate_for_reinjection,
   which overwrote it with the UNDECAYED stored value. The P4 centerpiece
   ("nothing is monotonic; unobserved learnings lose standing") never
   reached a prompt or the trim ranking.
2+3. Dead write paths: ``await get_hermes_memory()`` raises TypeError
   (get_hermes_memory is a plain factory), swallowed by bare except at
   autonomous.py pipeline-validation and system-watchdog sites — those
   findings were never recorded, ever.
4. Provenance evaporates: admit_learning computes/honors the provenance
   class, migration 015 added source_class/provenance_seal columns, but
   record_learning never wrote them — every read defaulted to INFERRED,
   so even a seal-verified write reinjected as an unverified guess.
5. Wiki admission claim false: memory_epistemics docstrings say the wiki's
   >= 0.5 gate "cannot be reached by an unverified guess alone", but the
   INFERRED ceiling (0.55) sits ABOVE the gate, so every clamped guess
   passed — with no provenance marker attached to the compile source.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.memory_epistemics import (  # noqa: E402
    PROVENANCE_CEILINGS,
    annotate_for_reinjection,
    decay_confidence,
)


def _seal_for(session: dict, key_hex: str) -> str:
    os.environ["CALLISTO_SEAL_KEY"] = key_hex
    import importlib
    import tools.memory_epistemics as me
    importlib.reload(me)
    return me._seal_digest(me._canonical_payload(session))


class TestDecaySurvivesReinjection(unittest.TestCase):
    """Fix A: the caller's decayed effective_confidence must survive."""

    def test_annotate_preserves_caller_decayed_confidence(self):
        ann = annotate_for_reinjection({
            "key": "k", "confidence": 0.55, "effective_confidence": 0.05,
            "source_class": "INFERRED",
        })
        self.assertEqual(ann["effective_confidence"], 0.05,
                         "decay computed by the reader was clobbered")

    def test_annotate_clamps_provided_value_to_ceiling(self):
        ann = annotate_for_reinjection({
            "key": "k", "confidence": 0.95, "effective_confidence": 0.9,
            "source_class": "INFERRED",
        })
        self.assertLessEqual(ann["effective_confidence"],
                             PROVENANCE_CEILINGS["INFERRED"])

    def test_annotate_defaults_to_stored_when_no_decay_supplied(self):
        ann = annotate_for_reinjection({"key": "k", "confidence": 0.55})
        self.assertEqual(ann["effective_confidence"], 0.55)

    def test_build_learnings_emits_decayed_effective_confidence(self):
        """End-to-end: a stale learning must reach its prompt section with
        its DECAYED standing, not its undecayed stored value."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL, learned_at TEXT NOT NULL,
            confidence REAL DEFAULT 0.5, occurrences INTEGER DEFAULT 1,
            source TEXT DEFAULT 'claude')""")
        conn.commit()
        conn.close()
        os.environ.pop("CALLISTO_SEAL_KEY", None)

        async def run():
            import aiosqlite
            from tools.hermes_memory import HermesMemory
            hm = HermesMemory(db_path=db_path)
            await hm.record_learning("stale_pattern", "an optimistic guess",
                                     confidence=0.55)
            stale = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
            async with aiosqlite.connect(db_path) as db:
                await db.execute("UPDATE hermes_learnings SET learned_at=? "
                                 "WHERE key='stale_pattern'", (stale,))
                await db.commit()
                text = await hm._build_learnings(db)
            return text

        text = asyncio_run(run())
        expected = round(decay_confidence(
            0.55, (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
            datetime.now(timezone.utc)), 4)
        self.assertIn(f"[eff {expected:.0%} conf", text,
                      f"prompt showed undecayed standing:\n{text}")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestNoIllegalAwaitOnHermesFactory(unittest.TestCase):
    """Fix B: ``await get_hermes_memory()`` raises TypeError — the factory is
    not a coroutine — and both call sites swallow it with bare except, so the
    write silently never happens. A behavioural test would need to drive the
    whole validation/watchdog phase; the defect IS the await spelling, so pin
    the source directly."""

    def test_no_caller_awaits_the_sync_factory(self):
        src = Path("tools/autonomous.py").read_text()
        self.assertNotIn("await get_hermes_memory(", src,
                         "get_hermes_memory() is not awaitable; awaiting it "
                         "throws inside a bare-except and the learning is "
                         "silently lost")


class TestProvenancePersistsRoundTrip(unittest.TestCase):
    """Fix C: the admitted provenance class must be stored and honoured on
    read, not dropped on the floor so every row reads back as INFERRED."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "t.db")
        self.key_hex = "ab" * 32
        os.environ["CALLISTO_SEAL_KEY"] = self.key_hex
        self.addCleanup(os.environ.pop, "CALLISTO_SEAL_KEY", None)
        import importlib
        import tools.memory_epistemics as me
        importlib.reload(me)
        import tools.hermes_memory as hmod
        importlib.reload(hmod)

    def _hm(self):
        return self._hermes_cls()(db_path=self.db_path)

    def _hermes_cls(self):
        import tools.hermes_memory as hmod
        return hmod.HermesMemory

    def test_sealed_secondary_write_keeps_class_on_read_back(self):
        session = {"query": "q", "conclusion": "c"}
        import hashlib, hmac
        payload = json.dumps({**session, "seal_hash": None},
                             sort_keys=True, ensure_ascii=False)
        seal = hmac.new(bytes.fromhex(self.key_hex),
                        payload.encode("utf-8"), hashlib.sha256).hexdigest()

        async def run():
            hm = self._hm()
            await hm.record_learning("sealed_finding", "value", confidence=0.9,
                                     source="claude", source_class="SECONDARY",
                                     seal_session=session, seal_hash=seal)
            return await hm.get_actionable_learnings(min_confidence=0.0)

        rows = asyncio_run(run())
        self.assertTrue(rows)
        row = next(r for r in rows if r["key"] == "sealed_finding")
        self.assertEqual(row["source_class"], "SECONDARY")
        self.assertEqual(row["confidence_ceiling"],
                         PROVENANCE_CEILINGS["SECONDARY"])
        self.assertAlmostEqual(row["confidence"], 0.75, places=2)

    def test_unverified_claimed_primary_collapses_to_inferred_on_read(self):
        async def run():
            hm = self._hm()
            await hm.record_learning("guessy", "value", confidence=0.9,
                                     source_class="PRIMARY")
            return await hm.get_actionable_learnings(min_confidence=0.0)

        rows = asyncio_run(run())
        row = next(r for r in rows if r["key"] == "guessy")
        self.assertEqual(row["source_class"], "INFERRED")
        self.assertLessEqual(row["confidence"],
                             PROVENANCE_CEILINGS["INFERRED"])

    def test_ensure_tables_upgrades_legacy_schema_without_columns(self):
        """A pre-migration DB (no source_class column) must be upgraded lazily
        at first touch — the workstation DB has not run migration 015."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL, learned_at TEXT NOT NULL,
            confidence REAL DEFAULT 0.5, occurrences INTEGER DEFAULT 1,
            source TEXT DEFAULT 'claude')""")
        conn.commit()
        conn.close()

        async def run():
            hm = self._hm()
            await hm.record_learning("legacy_key", "value", confidence=0.6)

        asyncio_run(run())
        cols = [r[1] for r in sqlite3.connect(self.db_path)
                .execute("PRAGMA table_info(hermes_learnings)").fetchall()]
        self.assertIn("source_class", cols)
        stored = sqlite3.connect(self.db_path).execute(
            "SELECT source_class FROM hermes_learnings WHERE key='legacy_key'"
        ).fetchone()
        self.assertEqual(stored[0], "INFERRED")


class TestWikiLearningAdmission(unittest.TestCase):
    """Fix D: the wiki compiles learnings whose CURRENT standing clears the
    gate — unobserved guesses decay out, one-shot guesses need corroboration,
    and every admitted source carries its provenance class."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "t.db")
        os.environ.pop("CALLISTO_SEAL_KEY", None)

    async def _setup(self):
        import aiosqlite
        from tools.knowledge_wiki import KnowledgeWiki
        conn = await aiosqlite.connect(self.db_path)
        wiki = KnowledgeWiki(self.db_path)
        await wiki.initialize(conn)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, query TEXT NOT NULL,
                domain TEXT NOT NULL, scope TEXT NOT NULL, conclusion TEXT,
                confidence_score REAL, confidence_tier TEXT,
                evidence_count INTEGER DEFAULT 0,
                contradiction_count INTEGER DEFAULT 0,
                manager_objections TEXT DEFAULT '[]',
                full_session TEXT NOT NULL, seal_hash TEXT,
                started_at TEXT NOT NULL, sealed_at TEXT)""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS catalogue (
                entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT, domain TEXT, confidence_score REAL,
                source_name TEXT, created_at TEXT)""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hermes_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE,
                value TEXT, learned_at TEXT, confidence REAL,
                occurrences INTEGER, source TEXT, source_class TEXT)""")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS wiki_compile_log (
                cycle INTEGER PRIMARY KEY, articles_created INTEGER,
                articles_updated INTEGER, duration_seconds REAL,
                compiled_at TEXT)""")
        await conn.commit()
        return conn, wiki

    async def _insert_learning(self, conn, key, conf, occ, age_days,
                               source_class=None):
        ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        await conn.execute(
            "INSERT INTO hermes_learnings (key, value, learned_at, confidence,"
            " occurrences, source, source_class) VALUES (?,?,?,?,?,?,?)",
            (key, "content of " + key, ts, conf, occ, "claude", source_class))
        await conn.commit()

    def _learnings_in(self, sources):
        return {s["id"]: s for s in sources if s["type"] == "learning"}

    def test_single_one_shot_guess_is_not_admitted(self):
        async def run():
            conn, wiki = await self._setup()
            await self._insert_learning(conn, "one_shot_guess", 0.55, 1, 0)
            srcs = await wiki._get_uncompiled_sources(conn)
            await conn.close()
            return srcs

        self.assertEqual(self._learnings_in(asyncio_run(run())), {},
                         "an unverified one-shot guess reached wiki compile")

    def test_reobserved_guess_admitted_with_provenance_marker(self):
        async def run():
            conn, wiki = await self._setup()
            await self._insert_learning(conn, "reobserved_pattern",
                                        0.55, 2, 0)
            srcs = await wiki._get_uncompiled_sources(conn)
            await conn.close()
            return srcs

        got = self._learnings_in(asyncio_run(run()))
        self.assertIn("reobserved_pattern", got)
        self.assertEqual(got["reobserved_pattern"]["provenance_class"],
                         "INFERRED")
        self.assertLessEqual(got["reobserved_pattern"]["confidence"],
                             PROVENANCE_CEILINGS["INFERRED"])

    def test_stale_reobserved_guess_decays_out_of_admission(self):
        async def run():
            conn, wiki = await self._setup()
            await self._insert_learning(conn, "stale_pattern", 0.55, 5, 60)
            srcs = await wiki._get_uncompiled_sources(conn)
            await conn.close()
            return srcs

        self.assertEqual(self._learnings_in(asyncio_run(run())), {},
                         "a 60-day-unobserved guess still compiled as prior")

    def test_higher_class_learning_needs_no_corroboration(self):
        async def run():
            conn, wiki = await self._setup()
            await self._insert_learning(conn, "audited_finding", 0.70, 1, 0,
                                        source_class="SECONDARY")
            srcs = await wiki._get_uncompiled_sources(conn)
            await conn.close()
            return srcs

        got = self._learnings_in(asyncio_run(run()))
        self.assertIn("audited_finding", got)
        self.assertEqual(got["audited_finding"]["provenance_class"],
                         "SECONDARY")


if __name__ == "__main__":
    unittest.main()
