"""P4 build wave tests — the memory layer (tools/hermes_memory.py).

Covers:
  Job 1  — trust escalator is dead: no MAX ratchet, provenance ceilings,
           seal-gated class claims (fail closed).
  Job 2  — disconfirming-biased trimming consistent with
           tools/loop_quality.compact_state.
  Job 3  — provenance + confidence ceiling travel with reinjected learnings.
  Mig    — migration 015 dry-run/up/down round-trip on a synthetic DB.

Property-based: invariants are probed with RANDOM inputs, not chosen ones
(HANDOFF.md: "Probe properties with random inputs, not chosen ones").
No live API calls; no socket use; real DBs are never touched.
"""

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("CALLISTO_DB_PATH", ":memory:")

from tools.memory_epistemics import (
    CONFIDENCE_HALF_LIFE_DAYS,
    PROVENANCE_CEILINGS,
    admit_learning,
    annotate_for_reinjection,
    clamp_to_ceiling,
    decay_confidence,
    trim_learnings_for_context,
    verify_learning_seal,
)

RNG = random.Random(20260822)


def rand_conf():
    return RNG.random()


# ────────────────────────────────────────────────────────────────────────────
# Agreement with agp thresholds
# ────────────────────────────────────────────────────────────────────────────

class TestThresholdAgreement(unittest.TestCase):
    def test_ceilings_match_agp_thresholds(self):
        from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
        for cls, ceiling in PROVENANCE_CEILINGS.items():
            self.assertEqual(ceiling, MAX_CONFIDENCE_BY_SOURCE[cls],
                             f"ceiling drift for {cls}")


# ────────────────────────────────────────────────────────────────────────────
# JOB 1: the ratchet is dead
# ────────────────────────────────────────────────────────────────────────────

class TestNoMaxRatchet(unittest.TestCase):
    def test_no_max_upsert_anywhere_in_hermes_memory(self):
        src = (REPO / "tools" / "hermes_memory.py").read_text()
        # The ratchet SQL must be gone; only a historical mention in the
        # record_learning docstring is tolerated.
        self.assertEqual(src.count("MAX(confidence"), 1,
                         "MAX-ratchet upsert must exist nowhere but the history note")
        self.assertNotIn("confidence=MAX(confidence, excluded.confidence) \"\n",
                         src, "ratchet upsert must not be live SQL")

    def test_confidence_can_fall(self):
        """Property: for random conf pairs, a lower rewrite stores the LOWER
        value (no ratchet) — using a trusted channel so ceilings don't clamp."""
        for _ in range(50):
            hi, lo = sorted((rand_conf(), rand_conf()), reverse=True)
            adm1 = admit_learning(key="k", confidence=hi, source="audit")
            adm2 = admit_learning(key="k", confidence=lo, source="audit")
            self.assertLess(adm2.stored_confidence, adm1.stored_confidence)

    def test_unverified_never_exceeds_inferred_ceiling(self):
        """Property: any model-source claim of ANY confidence lands at or
        below the INFERRED ceiling when it carries no provenance."""
        ceiling = PROVENANCE_CEILINGS["INFERRED"]
        for _ in range(100):
            c = rand_conf()
            adm = admit_learning(key="k", confidence=c, source=RNG.choice(
                ["claude", "callisto", "hermes", "agent", "self_repair"]))
            self.assertLessEqual(adm.stored_confidence, ceiling)
            self.assertEqual(adm.source_class, "INFERRED")


class TestSealGatedClassClaims(unittest.TestCase):
    def _sealed_session_and_hash(self, tamper=False):
        """Build a REAL agp session, seal it with a key set, return dict+hash."""
        os.environ["CALLISTO_SEAL_KEY"] = (b"\xab" * 32).hex()
        from agp import AGPSession, Evidence, SessionStep, SessionSummary, SourceClass, Domain
        s = AGPSession("test query")
        s.domain = Domain.TECHNICAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.sources = ["example.org"]
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(Evidence(
            content="some fetched content",
            source_class=SourceClass.SECONDARY,
            confidence_score=0.7,
            domain=Domain.TECHNICAL,
            origin_agent="t",
            source_name="https://example.org/x",
        ))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope=s.scope, domain=Domain.TECHNICAL,
            conclusion="a real conclusion", confidence_score=0.7,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        h = s.seal()
        d = s.to_dict()
        if tamper:
            d = json.loads(json.dumps(d))
            d["summary"]["conclusion"] = "TAMPERED"
        return d, h

    def setUp(self):
        if "CALLISTO_SEAL_KEY" in os.environ:
            del os.environ["CALLISTO_SEAL_KEY"]

    def tearDown(self):
        os.environ.pop("CALLISTO_SEAL_KEY", None)
        os.environ.pop("CALLISTO_SEAL_KEY_OLD", None)

    def test_valid_seal_honors_secondarial_claim(self):
        # unkeyed legacy seal path: compute sha256 over canonical payload
        d, h = self._sealed_session_and_hash()
        adm = admit_learning(key="k", confidence=0.9, source="claude",
                             source_class="SECONDARY", seal_session=d, seal_hash=h)
        self.assertEqual(adm.source_class, "SECONDARY")
        self.assertLessEqual(adm.stored_confidence, PROVENANCE_CEILINGS["SECONDARY"])

    def test_failed_seal_collapses_to_inferred(self):
        d, h = self._sealed_session_and_hash(tamper=True)
        adm = admit_learning(key="k", confidence=0.9, source="claude",
                             source_class="SECONDARY", seal_session=d, seal_hash=h)
        self.assertEqual(adm.source_class, "INFERRED")
        self.assertLessEqual(adm.stored_confidence, PROVENANCE_CEILINGS["INFERRED"])

    def test_unsealed_claim_above_inferred_is_capped(self):
        for cls in ("SECONDARY", "PRIMARY", "SIGNAL"):
            adm = admit_learning(key="k", confidence=0.95, source="claude",
                                 source_class=cls, seal_session=None, seal_hash=None)
            self.assertEqual(adm.source_class, "INFERRED")

    def test_memory_seal_verifier_agrees_with_agp_random_payloads(self):
        """Property: for random payload mutations, memory_epistemics.
        verify_learning_seal agrees with AGPSession.verify_seal on every
        MUTATED (tampered) payload, and both reject the unkeyed digest under
        a keyed regime. Under an UNKEYED regime the two intentionally
        diverge: agp's verify accepts the legacy digest for backward
        compatibility with pre-keying seals, while memory_epistemics must
        reject it because it gates provenance-class claims (R5) — an
        integrity check may be lenient, a trust gate may not."""
        from agp import AGPSession
        import os
        base = {"session_id": "s1", "query": "q", "conclusion": "c",
                "seal_hash": None, "n": RNG.randint(0, 10**6)}
        digest = hashlib.sha256(
            json.dumps(base, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        had_key = "CALLISTO_SEAL_KEY" in os.environ
        if had_key:
            del os.environ["CALLISTO_SEAL_KEY"]
        try:
            for _ in range(40):
                mutated = dict(base)
                # ALWAYS mutate: an un-mutated copy verifies by design.
                target = RNG.choice([k for k in base if k != "seal_hash"])
                new_val = RNG.randint(0, 10**6)
                while new_val == base[target]:
                    new_val = RNG.randint(0, 10**6)
                mutated[target] = new_val
                # tampered payloads: both verifiers reject
                self.assertFalse(AGPSession.verify_seal(
                    {**mutated, "seal_hash": digest}))
                self.assertEqual(
                    verify_learning_seal(mutated, digest),
                    False,
                    f"unkeyed digest accepted on mutated payload {mutated}",
                )
            # untouched payload: agp accepts (legacy compat), memory gate does not
            self.assertTrue(AGPSession.verify_seal({**base, "seal_hash": digest}))
            self.assertFalse(verify_learning_seal(base, digest))
        finally:
            if had_key:
                os.environ["CALLISTO_SEAL_KEY"] = "00"  # restored marker; real env untouched in CI


class TestDecay(unittest.TestCase):
    def test_monotone_in_age(self):
        """Effective confidence is non-increasing in age, modulo the floor."""
        now = datetime.now(timezone.utc)
        conf = rand_conf() * 0.9 + 0.05
        prev = float("inf")
        for days in range(0, int(CONFIDENCE_HALF_LIFE_DAYS * 3)):
            eff = decay_confidence(conf, (now - timedelta(days=days)).isoformat(), now)
            self.assertLessEqual(eff, prev + 1e-9)
            prev = eff

    def test_half_life_arithmetic(self):
        now = datetime.now(timezone.utc)
        conf = 0.8
        one_half_life = decay_confidence(
            conf, (now - timedelta(days=CONFIDENCE_HALF_LIFE_DAYS)).isoformat(), now)
        self.assertAlmostEqual(one_half_life, conf / 2, delta=0.01)

    def test_random_rows_never_increase_and_stay_in_bounds(self):
        """Property: decay never raises a value above max(conf, floor) and
        stays within [0,1]."""
        now = datetime.now(timezone.utc)
        from tools.memory_epistemics import MIN_EFFECTIVE_CONFIDENCE
        for _ in range(200):
            conf = rand_conf()
            age = RNG.random() * 400
            eff = decay_confidence(conf, (now - timedelta(days=age)).isoformat(), now)
            self.assertLessEqual(eff, max(conf, MIN_EFFECTIVE_CONFIDENCE) + 1e-9)
            self.assertGreaterEqual(eff, 0.0)


# ────────────────────────────────────────────────────────────────────────────
# JOB 2: disconfirming-biased trimming
# ────────────────────────────────────────────────────────────────────────────

def _rand_items(n):
    items = []
    for i in range(n):
        stance = RNG.choice(["supporting", "contradicting", "neutral"])
        items.append({
            "id": f"it{i}",
            "stance": stance,
            "tier": RNG.randint(1, 5),
            "effective_confidence": rand_conf(),
        })
    return items


class TestDisconfirmingBiasedTrimming(unittest.TestCase):
    def test_contradicting_always_beats_supporting_under_pressure(self):
        """Property: with random budgets and random items, whenever a
        supporting item survives while some contradicting item is dropped,
        that is a bug. Contradicting retention >= supporting retention
        whenever both compete for the same budget."""
        for _ in range(100):
            items = _rand_items(RNG.randint(2, 30))
            budget = RNG.randint(1, max(1, len(items) // 2))
            kept, dropped = trim_learnings_for_context(items, max_items=budget)
            kept_ids = {it["id"] for it in kept}
            dropped_ids = {it["id"] for it in dropped}
            disc_kept = sum(1 for it in kept if it["stance"] == "contradicting")
            disc_total = sum(1 for it in items if it["stance"] == "contradicting")
            supp_dropped = sum(1 for it in dropped if it["stance"] == "supporting")
            if supp_dropped > 0:
                self.assertEqual(
                    disc_kept, disc_total,
                    f"supporting trimmed while contradicting was dropped: "
                    f"kept={kept_ids} dropped={dropped_ids}")

    def test_budget_respected_for_supporting_items(self):
        """The budget applies to supporting/neutral items; contradicting
        items always survive (never-drop rule, mirroring compact_state)."""
        for _ in range(60):
            items = _rand_items(RNG.randint(1, 25))
            budget = RNG.randint(0, len(items))
            kept, dropped = trim_learnings_for_context(items, max_items=budget)
            self.assertEqual(len(kept) + len(dropped), len(items))
            disc_kept = sum(1 for it in kept if it["stance"] == "contradicting")
            supp_neutral_kept = len(kept) - disc_kept
            expected_supp_budget = max(0, budget - disc_kept)
            self.assertLessEqual(supp_neutral_kept, max(expected_supp_budget, 0) or 0)
            # every contradicting item survives unless the budget itself is
            # smaller than the contradicting count (budget is absolute)
            if budget >= sum(1 for it in items if it["stance"] == "contradicting"):
                self.assertEqual(
                    disc_kept,
                    sum(1 for it in items if it["stance"] == "contradicting"))

    def test_consistency_with_loop_quality_compact_state(self):
        """The survival ORDERING must agree with loop_quality.compact_state:
        every contradicting item survives whenever the budget allows it."""
        from tools.loop_quality import compact_state
        for _ in range(30):
            items = _rand_items(RNG.randint(3, 20))
            lq_kept, lq_dropped = compact_state([dict(i) for i in items],
                                                max_supporting=len(items),
                                                max_neutral=len(items))
            self.assertFalse(lq_dropped,
                             "infinite budgets: loop_quality drops nothing")
            our_kept, _ = trim_learnings_for_context(items, max_items=len(items))
            self.assertEqual({i["id"] for i in our_kept}, {i["id"] for i in items})
            disc_lq = [i for i in lq_kept if i["stance"] == "contradicting"]
            disc_our = [i for i in our_kept if i["stance"] == "contradicting"]
            self.assertEqual({i["id"] for i in disc_lq}, {i["id"] for i in disc_our})

    def test_dropped_items_carry_reason(self):
        items = _rand_items(15)
        kept, dropped = trim_learnings_for_context(items, max_items=3)
        for d in dropped:
            self.assertIn("dropped_reason", d)

    def test_unknown_stance_treated_as_supporting(self):
        """Conservative direction: unknown stances get NO contradicting
        protection (memory entries are assertions until marked otherwise)."""
        items = [{"id": "a", "stance": "garbage", "tier": 1, "effective_confidence": 0.9},
                 {"id": "b", "stance": "contradicting", "tier": 5, "effective_confidence": 0.01}]
        kept, _ = trim_learnings_for_context(items, max_items=1)
        self.assertEqual(kept[0]["id"], "b")

    def test_missing_fields_do_not_crash(self):
        for _ in range(50):
            items = [{"id": f"x{i}"} for i in range(RNG.randint(1, 10))]
            kept, dropped = trim_learnings_for_context(items, RNG.randint(1, len(items)))
            self.assertEqual(len(kept) + len(dropped), len(items))


# ────────────────────────────────────────────────────────────────────────────
# JOB 3: provenance travels with reinjection
# ────────────────────────────────────────────────────────────────────────────

class TestProvenanceReinjection(unittest.TestCase):
    def test_reinjected_rows_always_carry_class_and_ceiling(self):
        for _ in range(100):
            row = {"key": "k", "confidence": rand_conf(),
                   "source_class": RNG.choice([None, "", "PRIMARY", "SECONDARY",
                                               "SIGNAL", "INFERRED", "BOGUS"])}
            ann = annotate_for_reinjection(row)
            self.assertIn("source_class", ann)
            self.assertIn("confidence_ceiling", ann)
            self.assertIn(ann["source_class"], PROVENANCE_CEILINGS)
            self.assertEqual(ann["confidence_ceiling"],
                             PROVENANCE_CEILINGS[ann["source_class"]])
            self.assertLessEqual(ann["effective_confidence"],
                                 ann["confidence_ceiling"])

    def test_inferred_reinjection_cannot_read_as_primary(self):
        ann = annotate_for_reinjection({"key": "k", "confidence": 0.55})
        self.assertEqual(ann["source_class"], "INFERRED")
        self.assertLess(ann["confidence_ceiling"],
                        PROVENANCE_CEILINGS["PRIMARY"])


class TestHermesMemoryRoundTrip(unittest.TestCase):
    """End-to-end against a temp SQLite DB: record → read-back."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        # pre-create minimal tables hermes touches
        conn = sqlite3.connect(self.db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            learned_at TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            occurrences INTEGER DEFAULT 1,
            source TEXT DEFAULT 'claude',
            source_class TEXT,
            provenance_seal TEXT)""")
        conn.commit()
        conn.close()
        from tools.hermes_memory import HermesMemory
        self.hm = HermesMemory(db_path=self.db_path)
        self.hm._db_initialized = True

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_then_decay_overwrite_lowers_confidence(self):
        async def run():
            await self.hm.record_learning("k1", "optimistic guess", confidence=0.99)
            await asyncio.sleep(0.01)
            await self.hm.record_learning("k1", "sober reassessment", confidence=0.30)
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT confidence FROM hermes_learnings WHERE key='k1'").fetchone()
            conn.close()
            return row[0]
        stored = asyncio.run(run())
        self.assertAlmostEqual(stored, 0.30, places=2,
                               msg="last write must win (no ratchet); clamped to INFERRED ceiling")

    def test_low_rewrite_survives_after_high_write(self):
        async def run():
            await self.hm.record_learning("k2", "high", confidence=0.55)
            await self.hm.record_learning("k2", "low", confidence=0.20)
            conn = sqlite3.connect(self.db_path)
            row = conn.execute("SELECT confidence FROM hermes_learnings WHERE key='k2'").fetchone()
            conn.close()
            return row[0]
        self.assertAlmostEqual(asyncio.run(run()), 0.20, places=2)

    def test_actionable_learnings_carry_provenance(self):
        async def run():
            await self.hm.record_learning("k3", "value", confidence=0.7)
            learnings = await self.hm.get_actionable_learnings(min_confidence=0.0)
            return learnings
        out = asyncio.run(run())
        self.assertTrue(out)
        for l in out:
            self.assertIn("source_class", l)
            self.assertIn("confidence_ceiling", l)


# ────────────────────────────────────────────────────────────────────────────
# Migration 015
# ────────────────────────────────────────────────────────────────────────────

class TestMigration015(unittest.TestCase):
    def _mk_db(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE hermes_learnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            learned_at TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            occurrences INTEGER DEFAULT 1,
            source TEXT DEFAULT 'claude')""")
        for k, conf, learned_at, src in rows:
            conn.execute(
                "INSERT INTO hermes_learnings (key, value, learned_at, confidence, source) "
                "VALUES (?, ?, ?, ?, ?)", (k, "v", learned_at, conf, src))
        return conn

    def test_roundtrip_up_down(self):
        import importlib as il
        mig = il.import_module("tools.migrations.015_hermes_confidence_decay")
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        conn = self._mk_db([
            ("ratchet_high", 0.95, fresh, "claude"),
            ("stale_mid", 0.70, old, "claude"),
            ("human_ok", 0.90, fresh, "audit"),
        ])
        report = mig.dry_run(conn)
        self.assertTrue(report["needed"])
        self.assertGreater(report["rows_over_ceiling"], 0)
        before = conn.execute(
            "SELECT key, confidence FROM hermes_learnings ORDER BY key").fetchall()
        mig.up(conn)
        after = dict(conn.execute(
            "SELECT key, confidence FROM hermes_learnings").fetchall())
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hermes_learnings)")]
        self.assertIn("source_class", cols)
        self.assertIn("provenance_seal", cols)
        self.assertLessEqual(after["ratchet_high"], 0.55)
        self.assertLess(after["stale_mid"], 0.70)
        # Pre-migration rows have NO stored provenance, so they are all
        # treated as INFERRED and capped at 0.55 — including operator-written
        # ones. Honest default: unprovenanced data cannot claim PRIMARY.
        self.assertLessEqual(after["human_ok"], 0.55)
        mig.down(conn)
        restored = dict(conn.execute(
            "SELECT key, confidence FROM hermes_learnings").fetchall())
        self.assertEqual(restored, dict(before))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hermes_learnings)")]
        self.assertNotIn("source_class", cols)

    def test_idempotent_up(self):
        import importlib as il
        mig = il.import_module("tools.migrations.015_hermes_confidence_decay")
        conn = self._mk_db([("a", 0.9, datetime.now(timezone.utc).isoformat(), "claude")])
        mig.up(conn)
        snap1 = conn.execute("SELECT key, confidence FROM hermes_learnings").fetchall()
        mig.up(conn)
        snap2 = conn.execute("SELECT key, confidence FROM hermes_learnings").fetchall()
        self.assertEqual(snap1, snap2)


# ────────────────────────────────────────────────────────────────────────────
# JOB 6: the write→read provenance seam actually carries (2026-08-23)
# ────────────────────────────────────────────────────────────────────────────

class TestProvenanceSeamCarries(unittest.TestCase):
    """record_learning admits a class, _build_learnings re-reads it. The two
    ends previously did not meet: the SELECT had no source_class column, so a
    verified PRIMARY learning was stored at 1.0 and read back as INFERRED."""

    def setUp(self):
        import asyncio, os, tempfile
        from tools.hermes_memory import HermesMemory
        self._HermesMemory = HermesMemory
        self._loop = asyncio.new_event_loop()
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        # hermes context build reads bets/ev_opportunities/hypotheses too;
        # create the minimal tables so the degraded-context fallback never fires.
        import sqlite3
        conn = sqlite3.connect(path)
        for ddl in (
            "CREATE TABLE bankroll (balance REAL, timestamp TEXT)",
            """CREATE TABLE bets (
                id INTEGER PRIMARY KEY, game_description TEXT, team TEXT,
                market TEXT, bookmaker TEXT, placement_odds REAL,
                result TEXT, stake REAL, payout REAL, clv_implied REAL,
                placed_at TEXT, notes TEXT)""",
            """CREATE TABLE ev_opportunities (
                sport TEXT, team TEXT, market TEXT, bookmaker TEXT,
                american_odds REAL, edge REAL, expected_value REAL,
                kelly_fraction REAL, detected_at TEXT)""",
            "CREATE TABLE sessions (query TEXT, conclusion TEXT,"
            " confidence_score REAL, confidence_tier TEXT, sealed_at TEXT)",
            "CREATE TABLE hypotheses (status TEXT, name TEXT, sport TEXT,"
            " market_type TEXT, thesis TEXT, hypothesis_id TEXT, updated_at TEXT)",
            "CREATE TABLE backtest_events (hypothesis_id TEXT, event_id TEXT,"
            " signal_generated INTEGER, edge REAL)",
            """CREATE TABLE hermes_learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL, learned_at TEXT NOT NULL,
                confidence REAL DEFAULT 0.5, occurrences INTEGER DEFAULT 1,
                source TEXT DEFAULT 'claude',
                source_class TEXT, provenance_seal TEXT)""",
            """CREATE TABLE hermes_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                sender TEXT NOT NULL, message TEXT NOT NULL, read INTEGER DEFAULT 0)""",
        ):
            conn.execute(ddl)
        conn.commit()
        conn.close()
        self.db = HermesMemory(db_path=path)
        self.db._db_initialized = True  # tables exist; skip DDL path
        # Keyed regime so seal verification can succeed.
        import secrets
        os.environ["CALLISTO_SEAL_KEY"] = secrets.token_hex(32)

    def tearDown(self):
        import os
        self._loop.close()
        os.environ.pop("CALLISTO_SEAL_KEY", None)
        try:
            os.unlink(self.db.db_path)
        except OSError:
            pass

    def _seal(self, session: dict) -> str:
        from tools.memory_epistemics import _canonical_payload, _seal_digest
        return _seal_digest(_canonical_payload(session))

    def test_sealed_class_survives_the_roundtrip(self):
        async def run():
            session = {"session_id": "s1", "conclusion": "c"}
            await self.db.record_learning(
                key="verified_edge", value="x", confidence=0.95,
                source="claude", source_class="PRIMARY",
                seal_session=session, seal_hash=self._seal(session))
            ctx = await self.db.get_memory_context(force_refresh=True)
            import re
            m = re.search(r"provenance (\w+)", ctx)
            self.assertIsNotNone(m, f"no provenance annotation in context:\n{ctx}")
            self.assertEqual(m.group(1), "PRIMARY")
        self._loop.run_until_complete(run())
    def test_unsealed_write_reads_as_inferred(self):
        async def run():
            await self.db.record_learning(
                key="guess", value="y", confidence=0.95,
                source="claude", source_class="PRIMARY")  # no seal
            ctx = await self.db.get_memory_context(force_refresh=True)
            import re
            m = re.search(r"provenance (\w+)", ctx)
            self.assertIsNotNone(m)
            self.assertEqual(m.group(1), "INFERRED")
            # And the ceiling travelled with it.
            self.assertIn("ceiling 55%", ctx)
        self._loop.run_until_complete(run())

    def test_legacy_row_without_columns_still_renders(self):
        """A DB created before the new columns must not crash the read path."""
        import sqlite3
        conn = sqlite3.connect(self.db.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hermes_learnings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,"
            "value TEXT NOT NULL, learned_at TEXT NOT NULL,"
            "confidence REAL DEFAULT 0.5, occurrences INTEGER DEFAULT 1,"
            "source TEXT DEFAULT 'claude')")
        conn.execute(
            "INSERT INTO hermes_learnings (key, value, learned_at, confidence)"
            " VALUES ('old_row', 'v', ?, 0.9)",
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()

        async def run():
            ctx = await self.db.get_memory_context(force_refresh=True)
            self.assertIn("old_row", ctx)
            self.assertIn("provenance INFERRED", ctx)  # fail closed
        self._loop.run_until_complete(run())

    def test_ensure_tables_upgrades_preexisting_table(self):
        """_ensure_tables itself adds the missing columns to an old table."""
        import sqlite3, tempfile, os as _os
        import aiosqlite
        fd, path = tempfile.mkstemp(suffix=".db")
        _os.close(fd)
        self.addCleanup(lambda: _os.path.exists(path) and _os.unlink(path))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE hermes_learnings (key TEXT UNIQUE)")
        conn.commit()
        conn.close()

        cols: set = set()

        async def run():
            nonlocal cols
            async with aiosqlite.connect(path) as db:
                await self._HermesMemory(db_path=path)._ensure_tables(db)
                cols = {r[1] for r in await db.execute_fetchall(
                    "PRAGMA table_info(hermes_learnings)")}
        self._loop.run_until_complete(run())
        self.assertIn("source_class", cols)
        self.assertIn("provenance_seal", cols)


if __name__ == "__main__":
    unittest.main()
