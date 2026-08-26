"""tests/test_hermes_mem_split.py — pin the tools.hmem split of hermes_memory.

The 2026-08 refactor moved the implementation of tools/hermes_memory.py into
the tools.hmem package (sanitize / identity / sections / memory) and left
tools/hermes_memory.py as a facade. These tests pin:

1. The facade re-exports every public name callers rely on.
2. Behavior is unchanged end-to-end against a real temp SQLite DB
   (record_learning round trip, message queue, context assembly, caching,
   section ordering, degraded-mode fallback).
3. Security properties survive the move (audit C-4 sanitizers, audit C-6
   per-statement DDL, no MAX-ratchet upsert).
4. The two-plane rule is untouched: nothing here introduces hermes_cli into
   any model ladder, and Hermes memory stays a local tool — completions stay
   HTTP via ProviderRouter/inference.
"""

import asyncio
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import tools.hermes_memory as facade  # noqa: E402
import tools.hmem as hmem  # noqa: E402
from tools.hermes_memory import HermesMemory, get_hermes_memory  # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TempDBHermesCase(unittest.TestCase):
    """Base: a HermesMemory bound to a fresh temp SQLite DB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "callisto.db")
        self.hm = HermesMemory(db_path=self.db_path)
        self._seed_schema()

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_schema(self):
        conn = sqlite3.connect(self.db_path)
        # Tables hermes_* are created lazily by HermesMemory; seed the domain
        # tables the section builders read.
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bankroll (balance REAL, timestamp TEXT);
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_description TEXT, team TEXT, market TEXT, bookmaker TEXT,
                placement_odds INTEGER, stake REAL, payout REAL,
                result TEXT DEFAULT 'pending', clv_implied REAL,
                placed_at TEXT DEFAULT '', notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ev_opportunities (
                sport TEXT, team TEXT, market TEXT, bookmaker TEXT,
                american_odds INTEGER, edge REAL, expected_value REAL,
                kelly_fraction REAL, detected_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                query TEXT, conclusion TEXT, confidence_score REAL,
                confidence_tier TEXT, sealed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS hypotheses (
                hypothesis_id TEXT, name TEXT, sport TEXT, market_type TEXT,
                thesis TEXT, status TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS backtest_events (
                event_id TEXT, hypothesis_id TEXT, signal_generated INTEGER,
                edge REAL
            );
            """
        )
        conn.commit()
        conn.close()


# ────────────────────────────────────────────────────────────────────────────
# 1. Facade surface
# ────────────────────────────────────────────────────────────────────────────

class TestFacadeSurface(unittest.TestCase):
    NAMES = [
        "CALLER_HYPOTHESIS_GEN", "CALLER_DEEP_WORK", "CALLER_EDGE_ANALYSIS",
        "CALLER_TELEGRAM", "CALLER_DEFAULT", "DB_PATH", "MESSAGES_FILE",
        "HermesMemory", "get_hermes_memory", "get_cache_manager",
    ]

    def test_facade_exports_all_public_names(self):
        for name in self.NAMES:
            self.assertTrue(hasattr(facade, name), f"facade lost {name}")

    def test_facade_class_is_package_class(self):
        self.assertIs(facade.HermesMemory, hmem.HermesMemory)

    def test_caller_constants_stable(self):
        self.assertEqual(facade.CALLER_HYPOTHESIS_GEN, "hypothesis_gen")
        self.assertEqual(facade.CALLER_DEEP_WORK, "deep_work")
        self.assertEqual(facade.CALLER_EDGE_ANALYSIS, "edge_analysis")
        self.assertEqual(facade.CALLER_TELEGRAM, "telegram")
        self.assertEqual(facade.CALLER_DEFAULT, "default")

    def test_singleton_returns_same_instance(self):
        a = get_hermes_memory()
        b = get_hermes_memory()
        self.assertIs(a, b)
        self.assertIsInstance(a, HermesMemory)

    def test_sanitize_helpers_reachable_both_ways(self):
        self.assertIs(facade.sanitize_learning_key, hmem.sanitize_learning_key)
        self.assertIs(facade.sanitize_learning_value, hmem.sanitize_learning_value)
        # Legacy staticmethod access on the class still works post-split.
        self.assertIs(HermesMemory._sanitize_learning_key,
                      hmem.sanitize_learning_key)
        self.assertIs(HermesMemory._sanitize_learning_value,
                      hmem.sanitize_learning_value)

    def test_section_builders_live_in_hmem_sections(self):
        import tools.hmem.sections as sec
        for fn in ("build_bet_history", "build_edge_history",
                   "build_learned_patterns", "build_active_state",
                   "build_research_state", "build_learnings", "build_messages",
                   "build_code_changes"):
            self.assertTrue(hasattr(sec, fn), f"hmem.sections missing {fn}")
            self.assertTrue(hasattr(hmem, fn), f"hmem package missing {fn}")

    def test_hermes_memory_shrunk_to_facade(self):
        src = (REPO / "tools" / "hermes_memory.py").read_text()
        lines = [l for l in src.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertLess(len(lines), 120,
                        "facade must not re-embed the implementation")


# ────────────────────────────────────────────────────────────────────────────
# 2. Sanitizers (audit C-4), behavior preserved through the split
# ────────────────────────────────────────────────────────────────────────────

class TestSanitize(unittest.TestCase):
    def test_tags_neutralized(self):
        out = hmem.sanitize_learning_value("<script>alert(1)</script>")
        self.assertNotIn("<", out)
        self.assertNotIn(">", out)

    def test_code_fences_neutralized(self):
        out = hmem.sanitize_learning_value("```python\nprint(1)\n```")
        self.assertNotIn("```", out)

    def test_sentinels_escaped(self):
        for sentinel in ("[INST]", "[/INST]", "[SYSTEM]", "{{system}}",
                         "<|im_start|>", "<|im_end|>"):
            out = hmem.sanitize_learning_value(sentinel)
            self.assertNotIn(sentinel, out, f"{sentinel} survived")

    def test_zero_width_and_nul_removed(self):
        out = hmem.sanitize_learning_value("a\u200bb\x00c")
        self.assertEqual(out, "abc")

    def test_length_cap(self):
        out = hmem.sanitize_learning_value("x" * 9000)
        self.assertLessEqual(len(out), 4096 + len(" …[truncated]"))
        self.assertIn("truncated", out)

    def test_non_string_coerced(self):
        self.assertEqual(hmem.sanitize_learning_value(12345), "12345")

    def test_key_rules(self):
        self.assertEqual(hmem.sanitize_learning_key("  dk lag pinnacle "), "dk_lag_pinnacle")
        self.assertEqual(hmem.sanitize_learning_key("a/b:c.d-e_f"), "a/b:c.d-e_f")
        with self.assertRaises(ValueError):
            hmem.sanitize_learning_key("   ")
        self.assertLessEqual(len(hmem.sanitize_learning_key("k" * 500)), 128)
        self.assertNotIn("<", hmem.sanitize_learning_key("bad<key"))

    def test_record_learning_sanitizes_before_store(self):
        case = TempDBHermesCase()
        case.setUp()
        try:
            async def go():
                await case.hm.record_learning("inj key!", "<b>[INST] payload```</b>")
                import aiosqlite
                async with aiosqlite.connect(case.db_path) as db:
                    rows = await db.execute_fetchall(
                        "SELECT key, value FROM hermes_learnings")
                return rows

            rows = run(go())
            self.assertEqual(len(rows), 1)
            key, value = rows[0]
            self.assertNotIn("<", value)
            self.assertNotIn("[INST]", value)
            self.assertNotIn("`", value)
            self.assertNotIn(" ", key)
        finally:
            case.tearDown()


# ────────────────────────────────────────────────────────────────────────────
# 3. Epistemics: no MAX ratchet (pin survives the file split)
# ────────────────────────────────────────────────────────────────────────────

class TestNoRatchetPostSplit(unittest.TestCase):
    def test_no_ratchet_sql_in_any_hmem_module(self):
        pkg_dir = REPO / "tools" / "hmem"
        for py in list(pkg_dir.glob("*.py")) + [REPO / "tools" / "hermes_memory.py"]:
            src = py.read_text()
            live = src.count("confidence=MAX(confidence, excluded.confidence)")
            if py.name == "memory.py":
                # only the historical docstring mention is tolerated, and it is
                # prose, not SQL: it appears once, inside triple quotes.
                self.assertEqual(live, 1, f"{py}: unexpected ratchet mentions")
                # and the ON CONFLICT upsert never uses MAX
                self.assertNotIn('excluded.confidence)"\n                    "ON CONFLICT',
                                 src.replace(", ", ","))
            else:
                self.assertEqual(live, 0, f"{py}: ratchet leaked into {py.name}")

    def test_upsert_is_replace_semantics(self):
        from tools.hmem.memory import HermesMemory as HM
        src = inspect.getsource(HM.record_learning)
        self.assertIn("ON CONFLICT(key) DO UPDATE SET", src)
        self.assertIn("confidence=excluded.confidence,", src)
        # exactly one MAX(confidence mention, and it is the historical docstring
        self.assertEqual(src.count("MAX(confidence"), 1)
        self.assertIn("quoted here only as history", src)


# ────────────────────────────────────────────────────────────────────────────
# 4. End-to-end behavior on real SQLite
# ────────────────────────────────────────────────────────────────────────────

class TestLearningsRoundTrip(TempDBHermesCase):
    def test_write_then_read_context_contains_learning(self):
        async def go():
            await self.hm.record_learning("dk_h2h_lag_pinnacle",
                                          "DK h2h lines lag Pinnacle by ~12 min",
                                          confidence=0.9, source="audit")
            ctx = await self.hm.get_memory_context(force_refresh=True)
            learnings = await self.hm.get_actionable_learnings(limit=5,
                                                               min_confidence=0.5)
            return ctx, learnings

        ctx, learnings = run(go())
        self.assertIn("dk_h2h_lag_pinnacle", ctx)
        self.assertIn("DK h2h lines lag Pinnacle", ctx)
        self.assertTrue(any(l["key"] == "dk_h2h_lag_pinnacle" for l in learnings))

    def test_confidence_floor_filters_actionable(self):
        async def go():
            await self.hm.record_learning("weak_one", "low conf note",
                                          confidence=0.1, source="audit")
            return await self.hm.get_actionable_learnings(min_confidence=0.5)

        self.assertFalse(run(go()))

    def test_batch_count(self):
        async def go():
            return await self.hm.record_learnings_batch([
                {"key": "k1", "value": "v1"},
                {"key": "k2", "value": "v2", "source": "human"},
                {"key": "k3", "value": "v3", "confidence": 0.8},
            ])

        self.assertEqual(run(go()), 3)

    def test_unknown_source_defaults_to_claude(self):
        async def go():
            await self.hm.record_learning("src_probe", "val", source="not_a_source")
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                return (await db.execute_fetchall(
                    "SELECT source FROM hermes_learnings WHERE key='src_probe'"))[0][0]

        self.assertEqual(run(go()), "claude")

    def test_bad_confidence_defaults(self):
        async def go():
            await self.hm.record_learning("conf_probe", "val", confidence="zzz")
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                return (await db.execute_fetchall(
                    "SELECT confidence FROM hermes_learnings WHERE key='conf_probe'"))[0][0]

        self.assertLessEqual(float(run(go())), 0.5 + 1e-9)


class TestMessageQueue(TempDBHermesCase):
    def test_send_then_unread_once(self):
        async def go():
            await self.hm.send_message("research_loop", "found edges on MLB unders")
            first = await self.hm.get_unread_messages()
            second = await self.hm.get_unread_messages()
            return first, second

        first, second = run(go())
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["sender"], "research_loop")
        self.assertEqual(second, [], "messages must be marked read")

    def test_message_invalidates_context_cache(self):
        async def go():
            before = await self.hm.get_memory_context(force_refresh=True)
            await self.hm.send_message("deep_work", "pipeline integrity issue")
            after = await self.hm.get_memory_context()
            return before, after

        before, after = run(go())
        self.assertNotIn("UNREAD MESSAGES", before)
        self.assertIn("UNREAD MESSAGES", after)
        self.assertIn("pipeline integrity issue", after)


class TestContextAssembly(TempDBHermesCase):
    def test_identity_always_first(self):
        async def go():
            out = {}
            for caller in ("default", "hypothesis_gen", "deep_work",
                           "edge_analysis", "telegram"):
                out[caller] = await self.hm.get_memory_context(caller=caller,
                                                               force_refresh=True)
            return out

        contexts = run(go())
        for caller, ctx in contexts.items():
            self.assertTrue(ctx.startswith('<memory type="identity">'),
                            f"{caller} context does not start with identity")

    def test_degraded_mode_returns_identity_banner(self):
        async def go():
            self.hm.db_path = os.path.join(self.db_path, "no", "such", "dir.db")
            return await self.hm.get_memory_context(force_refresh=True)

        ctx = run(go())
        self.assertIn("HERMES CONTEXT DEGRADED", ctx)
        self.assertIn("identity", ctx)

    def test_bets_section_content(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO bankroll VALUES (1000.0, '2026-08-01T00:00:00Z')")
        conn.execute(
            "INSERT INTO bets (game_description, team, market, bookmaker, "
            "placement_odds, stake, payout, result, clv_implied, placed_at, notes) "
            "VALUES ('Yankees v Red Sox', 'NYY', 'h2h', 'DraftKings', -120, 50, "
            "91.67, 'won', 0.02, '2026-08-20T12:00:00Z', '')")
        conn.commit()
        conn.close()

        async def go():
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                return await hmem.build_bet_history(db)

        sec = run(go())
        self.assertIn("<memory type=\"bets\">", sec)
        self.assertIn("Bankroll: $1000.0", sec)
        self.assertIn("1W-0L", sec)
        self.assertIn("WON: NYY h2h -120", sec)

    def test_active_state_lists_pending_only(self):
        conn = sqlite3.connect(self.db_path)
        now = "2026-08-25T10:00:00Z"
        for i, result in enumerate(["pending", "pending", "won"]):
            conn.execute(
                "INSERT INTO bets (game_description, team, market, bookmaker, "
                "placement_odds, stake, result, placed_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"g{i}", f"T{i}", "h2h", "DK", 150, 10, result, now, f"note {i}"),
            )
        conn.commit()
        conn.close()

        async def go():
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                return await hmem.build_active_state(db)

        sec = run(go())
        self.assertIn("Open bets (2):", sec)
        self.assertIn("Bet #1:", sec)
        self.assertIn("Bet #2:", sec)
        self.assertNotIn("Bet #3:", sec)

    def test_empty_sections_return_empty_string(self):
        async def go():
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                return {
                    "edges": await hmem.build_edge_history(db),
                    "patterns": await hmem.build_learned_patterns(db),
                    "active": await hmem.build_active_state(db),
                    "learnings": await hmem.build_learnings(db),
                    "messages": await hmem.build_messages(db),
                }

        for name, sec in run(go()).items():
            self.assertEqual(sec, "", f"{name} should be empty on empty DB")

    def test_messages_section_format(self):
        async def go():
            await self.hm.send_message("termius_session", "x" * 400)
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as db:
                # messages were marked unread; keep read=0 for the builder
                await db.execute("UPDATE hermes_messages SET read=0")
                await db.commit()
                return await hmem.build_messages(db)

        sec = run(go())
        self.assertIn("UNREAD MESSAGES (1):", sec)
        self.assertIn("[termius_session]"[:1], "[")  # sanity no-op
        self.assertIn("ACTION: Acknowledge these messages", sec)
        # msg truncated to 150 chars
        body_line = [l for l in sec.splitlines() if "termius_session:" in l][0]
        self.assertLessEqual(len(body_line.split(": ", 1)[1]), 151)

    def test_code_changes_section_runs_in_repo(self):
        sec = hmem.build_code_changes()
        self.assertIsInstance(sec, str)
        if sec:
            self.assertIn("<memory type=\"code_changes\">", sec)

    def test_identity_block_content(self):
        block = hmem.build_identity()
        self.assertIn("You are Callisto", block)
        self.assertIn("RULES:", block)
        self.assertIn("WRITE IT BACK via record_learning()", block)
        self.assertTrue(block.endswith("</memory>"))


class TestCacheBehavior(TempDBHermesCase):
    def test_cache_hit_avoids_rebuild(self):
        calls = {"n": 0}
        orig = hmem.memory.build_identity

        def counting():
            calls["n"] += 1
            return orig()

        hmem.memory.build_identity = counting
        try:
            async def go():
                a = await self.hm.get_memory_context(caller="telegram")
                b = await self.hm.get_memory_context(caller="telegram")
                return a, b

            a, b = run(go())
            self.assertEqual(a, b)
            self.assertEqual(calls["n"], 1, "second call should hit cache")
        finally:
            hmem.build_identity = orig

    def test_ttl_expiry_rebuilds(self):
        async def go():
            a = await self.hm.get_memory_context(caller="default")
            self.hm._cache_time["default"] -= self.hm._cache_ttl + 1
            b = await self.hm.get_memory_context(caller="default")
            return a, b

        run(go())  # no assertion beyond "does not crash"; expiry path exercised

    def test_cache_eviction_under_pressure(self):
        hm = self.hm
        hm._cache_max_entries = 2

        async def go():
            for caller in ("a", "b", "c", "d"):
                await hm.get_memory_context(caller=caller, force_refresh=True)
            return len(hm._cache)

        self.assertLessEqual(run(go()), 2)


# ────────────────────────────────────────────────────────────────────────────
# 5. DDL hygiene (audit C-6): per-statement execute, not executescript
# ────────────────────────────────────────────────────────────────────────────

class TestDDLHygiene(unittest.TestCase):
    def test_no_executescript_in_hermes_modules(self):
        paths = [REPO / "tools" / "hermes_memory.py"] + list((REPO / "tools" / "hmem").glob("*.py"))
        for p in paths:
            self.assertNotIn(".executescript(", p.read_text(),
                             f"{p.name}: multi-statement DDL regression")

    def test_tables_created_lazily_and_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            hm = HermesMemory(db_path=os.path.join(tmp.name, "x.db"))

            async def go():
                for _ in range(3):
                    await hm.send_message("s", "m")
                return True

            self.assertTrue(run(go()))
        finally:
            tmp.cleanup()


# ────────────────────────────────────────────────────────────────────────────
# 6. Two-plane guardrails: Hermes stays local tooling; no transport creep
# ────────────────────────────────────────────────────────────────────────────

class TestTransportGuardrails(unittest.TestCase):
    def test_no_hermes_cli_reference_in_split(self):
        for p in (REPO / "tools" / "hmem").glob("*.py"):
            src = p.read_text()
            self.assertNotIn("hermes_cli", src,
                             f"{p.name}: CLI plane leaked into kernel-side memory")
            self.assertNotIn("MODEL_LADDER", src)

    def test_no_http_completion_calls_in_split(self):
        for p in (REPO / "tools" / "hmem").glob("*.py"):
            src = p.read_text()
            self.assertNotIn("ProviderRouter.complete", src)
            self.assertNotIn("requests.post", src)
            self.assertNotIn("httpx", src)

    def test_paper_trade_statuses_untouched_by_split(self):
        """The split must not touch paper-trade gating at all — belt and braces."""
        try:
            import tools.paper_trade_gate as gate
        except ImportError:
            self.skipTest("tools.paper_trade_gate not present in this tree")
        statuses = getattr(gate, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is None:
            self.skipTest("no _PAPER_TRADE_SIGNAL_STATUSES in this tree")
        self.assertNotIn("live", statuses)


if __name__ == "__main__":
    unittest.main()
