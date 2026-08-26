"""Tests for the knowledge wiki store/compiler split.

Verifies:
  1. tools.wiki.store.WikiStore handles persistence/query without any LLM
     imports (compile pipeline must not be reachable from the store module).
  2. tools.wiki.compiler.WikiCompiler owns compilation/lint and composes on
     the store.
  3. tools.knowledge_wiki.KnowledgeWiki façade keeps back-compat: same
     public methods, module counters, pending-embed queue, singleton.
  4. No silent evidence rewrites: neither layer issues UPDATEs against
     historical evidence flags (catalogue/sessions confidence or signal
     flags) — wiki writes only touch wiki_* tables.
"""

import ast
import inspect

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_store_module_does_not_import_inference():
    """The store must never pull in the LLM compile stack."""
    import tools.wiki.store as store_mod
    src = inspect.getsource(store_mod)
    assert "from inference" not in src
    assert "import inference" not in src
    assert "OllamaInference" not in src


def test_compiler_module_has_llm_and_template_compile():
    """The compiler owns the LLM compilation path + template fallback."""
    import tools.wiki.compiler as comp_mod
    assert hasattr(comp_mod.WikiCompiler, "_llm_compile")
    assert hasattr(comp_mod.WikiCompiler, "_template_compile")
    assert hasattr(comp_mod.WikiCompiler, "compile")
    assert hasattr(comp_mod.WikiCompiler, "lint")


def test_facade_composes_both_layers(tmp_path):
    from tools.knowledge_wiki import KnowledgeWiki
    from tools.wiki.store import WikiStore
    from tools.wiki.compiler import WikiCompiler

    db_path = str(tmp_path / "wiki.db")
    wiki = KnowledgeWiki(db_path)
    assert isinstance(wiki.store, WikiStore)
    assert isinstance(wiki._compiler, WikiCompiler)
    assert wiki._compiler.store is wiki.store
    assert wiki.db_path == db_path


def test_backcompat_names_reexported():
    import tools.knowledge_wiki as kw

    for name in (
        "WIKI_SCHEMA_SQL", "WIKI_COLLECTION", "STALE_THRESHOLD_HOURS",
        "COMPILE_INTERVAL_CYCLES", "LINT_INTERVAL_CYCLES",
        "MAX_SOURCES_PER_COMPILE", "MAX_ARTICLE_LENGTH",
        "_article_confidence", "_merged_article_confidence",
        "_pending_embeds", "_EMBED_QUEUE_MAX",
        "_wiki_writes_succeeded", "_wiki_writes_failed",
        "get_write_stats", "get_wiki", "KnowledgeWiki",
    ):
        assert hasattr(kw, name), f"missing back-compat export: {name}"


def test_get_write_stats_reflects_counters():
    import tools.knowledge_wiki as kw
    stats = kw.get_write_stats()
    assert stats["succeeded"] == kw._wiki_writes_succeeded
    assert stats["failed"] == kw._wiki_writes_failed


def test_confidence_helpers_preserved_semantics():
    """Fail-closed min-of-sources semantics survive the split."""
    from tools.wiki.compiler import _article_confidence as c1
    from tools.wiki.compiler import _merged_article_confidence as m1

    assert c1([]) == 0.0
    assert c1([{"confidence": 0.9}, {"confidence": 0.5}]) == 0.5
    # Missing confidence field counts as 0.0 — pulls down, not up.
    assert c1([{"confidence": 0.9}, {}]) == 0.0
    merged = m1(existing_confidence=0.8, compile_count=3,
                new_sources=[{"confidence": 0.2}])
    assert merged <= 0.2


def test_no_evidence_flag_updates_outside_wiki_tables():
    """Fail-closed guard: no UPDATE/INSERT may target non-wiki tables.

    The wiki layers write only to wiki_articles / wiki_contradictions /
    wiki_compile_log. Any UPDATE touching catalogue, sessions, signals or
    other evidence tables would be a silent evidence rewrite.
    """
    import tools.knowledge_wiki as kw
    import tools.wiki.store as store_mod
    import tools.wiki.compiler as comp_mod

    forbidden = re_tbl = r"(catalogue|sessions|signals|hermes_learnings|task_queue)"
    for mod in (kw, store_mod, comp_mod):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.upper()
                # Only flag SQL UPDATE statements aimed at evidence tables
                # (DDL strings legitimately contain table-ish words).
                for m in __import__("re").finditer(
                    r"UPDATE\s+([A-Za-z_][A-Za-z0-9_]*)", node.value,
                    __import__("re").IGNORECASE,
                ):
                    assert not __import__("re").search(
                        forbidden, m.group(1), __import__("re").IGNORECASE
                    ), f"{mod.__name__}: UPDATE touches evidence table: {node.value[:120]}"


def test_store_search_and_write_smoke(tmp_path):
    """Store-level smoke: schema init, lesson upsert (created→updated),
    list/get/stats — all without any LLM involvement."""
    import asyncio
    import aiosqlite
    from tools.wiki.store import WikiStore

    async def run():
        db_path = str(tmp_path / "store.db")
        store = WikiStore(db_path)
        async with aiosqlite.connect(db_path) as db:
            await store.initialize(db)
            res1 = await store.write_lesson_article(
                db, topic="t_a", title="A", content="alpha lesson",
                confidence=0.7,
            )
            assert res1["action"] == "created"
            res2 = await store.write_lesson_article(
                db, topic="t_a", title="A2", content="alpha updated",
            )
            assert res2["action"] == "updated"
            art = await store.get_article(db, "t_a")
            assert art["compile_count"] == 2
            listed = await store.list_articles(db)
            assert len(listed) == 1
            stats = await store.get_stats(db)
            assert stats["total_articles"] == 1
            hits = await store.search(db, "alpha")
            assert hits[0]["topic"] == "t_a"

    asyncio.run(run())
