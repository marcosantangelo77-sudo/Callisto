"""improve/memory-wiki tests — pins the memory/wiki improvement pass.

Three units (findings/improve_memory_wiki.md):
  1. Wiki LLM calls route via ProviderRouter (wiki_compile task class);
     hardcodes gemma4/qwen3.5:4b only survive as fallback.
  2. Topic extraction is domain-general: non-sports sources no longer all
     collapse to <domain>_misc; sports slugs unchanged.
  3. Hermes identity prompt is domain-general; MESSAGES_FILE dead constant
     is gone.

All offline: routing failures are simulated, no sockets, no live models,
real DBs never touched.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

os.environ.setdefault("CALLISTO_DB_PATH", ":memory:")


def _wiki_no_init():
    """KnowledgeWiki instance without DB init (topic extraction is pure)."""
    from tools.knowledge_wiki import KnowledgeWiki
    return KnowledgeWiki.__new__(KnowledgeWiki)


# ────────────────────────────────────────────────────────────────────────────
# 1. Routed LLM calls
# ────────────────────────────────────────────────────────────────────────────

class _FakeRouter:
    def __init__(self, parsed=None, raise_exc=None):
        self.parsed = parsed
        self.raise_exc = raise_exc
        self.calls = []

    async def complete(self, task_class, messages, schema=None, **kwargs):
        self.calls.append({"task_class": task_class, "schema": schema})
        if self.raise_exc:
            raise self.raise_exc
        return {"content": "unused", "parsed_json": self.parsed}


class TestRoutedCompile(unittest.IsolatedAsyncioTestCase):
    async def test_compile_prefers_router_with_wiki_compile_class(self):
        from tools.knowledge_wiki import KnowledgeWiki
        w = KnowledgeWiki.__new__(KnowledgeWiki)
        router = _FakeRouter(parsed={
            "title": "T", "summary": "S", "content": "C", "related_topics": [],
        })
        called = {}

        async def fake_ollama(*a, **k):
            called["ollama"] = True
            return None

        import unittest.mock as mock
        with mock.patch("inference.get_router", return_value=router):
            w._ollama_compile = fake_ollama
            out = await w._llm_compile("some_topic", [], None)
        self.assertEqual(out["title"], "T")
        self.assertEqual(router.calls[0]["task_class"], "wiki_compile")
        self.assertNotIn("ollama", called)

    async def test_router_failure_falls_back_to_ollama_path(self):
        from tools.knowledge_wiki import KnowledgeWiki
        w = KnowledgeWiki.__new__(KnowledgeWiki)
        router = _FakeRouter(raise_exc=RuntimeError("router down"))

        async def fake_ollama(topic, sources, existing):
            return {"title": "FB", "summary": "", "content": "fallback"}

        import unittest.mock as mock
        with mock.patch("inference.get_router", return_value=router):
            w._ollama_compile = fake_ollama
            out = await w._llm_compile("t", [], None)
        self.assertEqual(out["title"], "FB")

    async def test_routed_garbage_falls_back(self):
        from tools.knowledge_wiki import KnowledgeWiki
        w = KnowledgeWiki.__new__(KnowledgeWiki)
        # parsed dict missing required fields -> must fall through to ollama
        router = _FakeRouter(parsed={"title": "", "content": ""})

        async def fake_ollama(topic, sources, existing):
            return {"title": "FB2", "summary": "", "content": "x"}

        import unittest.mock as mock
        with mock.patch("inference.get_router", return_value=router):
            w._ollama_compile = fake_ollama
            out = await w._llm_compile("t", [], None)
        self.assertEqual(out["title"], "FB2")

    async def test_compile_prompt_is_domain_general(self):
        from tools.knowledge_wiki import KnowledgeWiki
        w = KnowledgeWiki.__new__(KnowledgeWiki)
        prompt = w._compile_prompt("t", [
            {"type": "session", "confidence": 0.7, "content": "c"}], None)
        self.assertNotIn("sports betting", prompt)

    def test_hardcoded_models_only_in_fallback_path(self):
        src = (REPO / "tools" / "knowledge_wiki.py").read_text()
        # gemma4 must appear ONLY inside the direct-Ollama fallback
        idx_route = src.find("async def _routed_json")
        idx_fb = src.find("async def _ollama_compile")
        self.assertGreater(idx_fb, idx_route > 0, "expected both markers")
        pos = 0
        while True:
            pos = src.find('"gemma4"', pos + 1)
            if pos == -1:
                break
            self.assertGreater(pos, idx_fb, "gemma4 hardcoded outside fallback")


class TestRoutedContradictions(unittest.IsolatedAsyncioTestCase):
    async def test_contradictions_prefer_classification_class(self):
        from tools.knowledge_wiki import KnowledgeWiki
        w = KnowledgeWiki.__new__(KnowledgeWiki)
        router = _FakeRouter(parsed={"contradictions": [
            {"article_a": "a", "article_b": "b",
             "claim_a": "x", "claim_b": "y", "severity": "high"}]})

        class FakeCursor:
            async def fetchall(self):
                # two same-domain articles so the pair builder has work
                return [("t1", "summary one", "content", "GENERAL"),
                        ("t2", "summary two", "content", "GENERAL")]

        class FakeDB:
            executed = []

            async def execute(self, *a, **k):
                FakeDB.executed.append(a)
                return FakeCursor()

            async def commit(self):
                pass

        db = FakeDB()
        stored = None
        orig_store = KnowledgeWiki._store_contradictions

        async def fake_store(dself, d, found):
            return found

        import unittest.mock as mock
        KnowledgeWiki._store_contradictions = fake_store
        try:
            with mock.patch("inference.get_router", return_value=router):
                stored = await w._detect_contradictions(db)
        finally:
            KnowledgeWiki._store_contradictions = orig_store
        self.assertEqual(len(stored), 1)
        self.assertEqual(router.calls[0]["task_class"], "classification")

    async def test_store_contradictions_persists_rows(self):
        import aiosqlite
        from tools.knowledge_wiki import KnowledgeWiki

        async def run():
            db = await aiosqlite.connect(":memory:")
            try:
                for stmt in (
                    "CREATE TABLE wiki_contradictions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " article_a TEXT NOT NULL, article_b TEXT NOT NULL, claim_a TEXT NOT NULL,"
                    " claim_b TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'low',"
                    " resolved INTEGER NOT NULL DEFAULT 0, resolution TEXT,"
                    " detected_at TEXT NOT NULL, resolved_at TEXT)",
                ):
                    await db.execute(stmt)
                found = [{"article_a": "a", "article_b": "b", "claim_a": "x",
                          "claim_b": "y", "severity": "high"}]
                out = await KnowledgeWiki._store_contradictions(
                    KnowledgeWiki.__new__(KnowledgeWiki), db, found)
                cur = await db.execute("SELECT COUNT(*) FROM wiki_contradictions")
                n = (await cur.fetchone())[0]
                self.assertEqual(n, 1)
                self.assertEqual(out, found)
            finally:
                await db.close()

        await asyncio.wait_for(run(), timeout=10)


# ────────────────────────────────────────────────────────────────────────────
# 2. Domain-general topic extraction
# ────────────────────────────────────────────────────────────────────────────

class TestTopicExtraction(unittest.TestCase):
    CASES_NON_SPORTS = [
        ("What does recent scholarly research say about semiconductor "
         "supply chain resilience?", "TECHNICAL"),
        ("US unemployment rate rose to 4.2% in July", "FINANCIAL"),
        ("Clinical trial NCT123 shows phase 3 efficacy", "GENERAL"),
        ("Is Bitcoin a good buy right now?", "FINANCIAL"),
        ("The Fed raised the federal funds rate 25bps", "FINANCIAL"),
    ]

    def test_non_sports_sources_get_real_topics(self):
        w = _wiki_no_init()
        for query, domain in self.CASES_NON_SPORTS:
            topic = w._extract_topic(
                {"type": "session", "query": query, "content": "",
                 "domain": domain})
            self.assertFalse(topic.endswith("_misc"),
                             f"{query!r} collapsed to {topic}")

    def test_sports_slugs_unchanged(self):
        w = _wiki_no_init()
        self.assertEqual(
            w._extract_topic({"type": "session", "query": "NBA moneyline value",
                              "content": "", "domain": "SIGNAL"}),
            "nba_moneyline")
        self.assertEqual(
            w._extract_topic({"type": "session",
                              "query": "MLB spread backtest early season",
                              "content": "", "domain": "SIGNAL"}),
            "mlb_spread")

    def test_hypothesis_name_still_wins_first(self):
        w = _wiki_no_init()
        topic = w._extract_topic({
            "type": "learning", "query": "", "content":
            "mlb_early_home_fav shows drift even though unemployment rose",
            "domain": "GENERAL"})
        self.assertEqual(topic, "mlb_early_home_fav")

    def test_true_unknown_still_falls_to_misc(self):
        w = _wiki_no_init()
        topic = w._extract_topic({
            "type": "evidence", "query": "",
            "content": "quantum error correction threshold theorem",
            "domain": "TECHNICAL"})
        self.assertEqual(topic, "technical_misc")

    def test_specificity_beats_position(self):
        w = _wiki_no_init()
        # 'market cap' appears once late; 'earnings' twice -> equities wins
        topic = w._extract_topic({
            "type": "evidence", "query": "",
            "content": "earnings guidance cut; market cap fell after earnings day",
            "domain": "FINANCIAL"})
        self.assertEqual(topic, "equities_valuation")


# ────────────────────────────────────────────────────────────────────────────
# 3. Hermes identity / dead constant
# ────────────────────────────────────────────────────────────────────────────

class TestHermesIdentity(unittest.TestCase):
    def test_identity_is_domain_general(self):
        from tools.hermes_memory import HermesMemory
        ident = HermesMemory._build_identity(HermesMemory.__new__(HermesMemory))
        self.assertNotIn("DraftKings", ident)
        self.assertNotIn("Opus", ident)
        self.assertIn("ANY domain", ident.replace("any domain", "ANY domain")
                      if "ANY domain" not in ident else ident)

    def test_messages_file_constant_removed(self):
        src = (REPO / "tools" / "hermes_memory.py").read_text()
        self.assertNotIn("MESSAGES_FILE =", src)
        self.assertNotIn('os.path.join(os.path.dirname(DB_PATH)', src)


if __name__ == "__main__":
    unittest.main()
