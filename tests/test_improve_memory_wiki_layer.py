"""Improve pass: memory & wiki layer (build/cli-front-door, 2026-08-23).

Three defects, each with a regression test:

1. Wiki compile/lint hardcoded Ollama models ("gemma4", "qwen3.5:4b"),
   bypassing the ProviderRouter — violating the "never hardcode a provider"
   mandate. Now routed via task classes knowledge_compile / knowledge_lint,
   with the legacy direct-Ollama call kept only as fallback.

2. The wiki's deferred-embedding queue (_pending_embeds) was write-only:
   articles whose embedding was queued while Ollama was down were NEVER
   retried — permanently invisible to semantic search. flush_pending_embeds
   drains it; search() calls it opportunistically.

3. hermes_memory._build_identity injected a sports-book identity and a
   hardcoded model name into EVERY prompt — domain-welding and
   provider-welding at the most-injected seam in the system.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
import aiosqlite

from tools import knowledge_wiki as wiki_mod
from tools.knowledge_wiki import (
    KnowledgeWiki, TASK_CLASS_COMPILE, TASK_CLASS_LINT,
    _COMPILE_JSON_SCHEMA, _LINT_JSON_SCHEMA,
)
from tools.schema import ensure_schema


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "wiki_router_test.db")


@pytest_asyncio.fixture
async def db(db_path):
    await ensure_schema(db_path)
    async with aiosqlite.connect(db_path) as conn:
        yield conn, db_path


class FakeRouter:
    """Records task classes; returns canned parsed JSON."""
    calls = []

    def __init__(self, content='{"title": "T", "summary": "S", "content": "C"}'):
        self.content = content

    async def complete(self, task_class, messages, schema=None, **kw):
        FakeRouter.calls.append((task_class, schema))
        return {"content": self.content, "parsed_json": None, "model": "fake",
                "tier": "test", "task_class": task_class}


# ── 1. Compile routes through the ProviderRouter ──────────────────

@pytest.mark.asyncio
async def test_compile_routes_via_provider_router(db, monkeypatch):
    conn, db_path = db
    wiki = KnowledgeWiki(db_path)
    router = FakeRouter('{"title": "T", "summary": "S", "content": "body", '
                        '"related_topics": []}')
    def fake_get_router():
        return router
    import inference
    monkeypatch.setattr(inference, "get_router", fake_get_router)

    sources = [{"type": "session", "id": "s1", "query": "q", "domain": "GENERAL",
                "content": "c", "confidence": 0.7,
                "timestamp": "2026-08-23T00:00:00"}]
    out = await wiki._llm_compile("test_topic", sources, existing_content=None)
    assert out is not None and out["title"] == "T"
    assert FakeRouter.calls, "compile must go through the router"
    task_class, schema = FakeRouter.calls[0]
    assert task_class == TASK_CLASS_COMPILE == "knowledge_compile"
    assert schema == _COMPILE_JSON_SCHEMA


@pytest.mark.asyncio
async def test_compile_falls_back_when_router_absent(db, monkeypatch):
    """No providers.yaml → legacy path is used; template fallback still works."""
    conn, db_path = db
    wiki = KnowledgeWiki(db_path)
    import inference
    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(inference, "get_router", boom)
    # And no Ollama either — template fallback must produce an article.
    async def dead_achat(*a, **k):
        raise RuntimeError("no ollama")
    class DeadOllama:
        def __init__(self, *a): pass
        async def achat(self, *a, **k): raise RuntimeError("no ollama")
    fake_inf = type("M", (), {})
    import sys
    monkeypatch.setitem(sys.modules, "inference", _FakeInferenceModule())
    sources = [{"type": "session", "id": "s1", "query": "mlb spread",
                "domain": "GENERAL", "content": "c", "confidence": 0.6,
                "timestamp": "2026-08-23T00:00:00"}]
    out = await wiki._llm_compile("mlb_spread_general", sources,
                                  existing_content=None)
    assert out is not None          # template fallback produced something
    assert "Research Sessions" in out["content"]


class _FakeInferenceModule:
    """inference module stub where get_router raises and OllamaInference fails."""
    @staticmethod
    def get_router():
        raise RuntimeError("no providers.yaml")
    class AgentConfig:
        def __init__(self, **kw): pass
    class OllamaInference:
        def __init__(self, *a): pass
        async def achat(self, *a, **k): raise RuntimeError("no ollama")
    @staticmethod
    def _parse_json_response(text):
        return None


@pytest.mark.asyncio
async def test_lint_routes_via_provider_router(db, monkeypatch):
    conn, db_path = db
    wiki = KnowledgeWiki(db_path)
    payload = ('{"contradictions": [{"pair": 1, "article_a": "a", '
               '"article_b": "b", "claim_a": "x", "claim_b": "y", '
               '"severity": "high"}]}')
    router = FakeRouter(payload)
    def fake_get_router():
        return router
    import inference
    monkeypatch.setattr(inference, "get_router", fake_get_router)

    await wiki.initialize(conn)
    for t in ("a", "b"):
        r = await wiki.write_lesson_article(
            conn, topic=t, title=t, content=f"content {t}", domain="GENERAL")
    found = await wiki._detect_contradictions(conn)
    assert FakeRouter.calls
    assert FakeRouter.calls[-1][0] == TASK_CLASS_LINT == "knowledge_lint"
    assert FakeRouter.calls[-1][1] == _LINT_JSON_SCHEMA


# ── 2. Deferred-embedding queue actually drains ───────────────────

@pytest.mark.asyncio
async def test_flush_pending_embeds_drains_queue(db, monkeypatch):
    conn, db_path = db
    from tools import embeddings as emb_mod
    stored = []
    class FakeStore:
        def __init__(self, path): pass
        async def initialize(self): pass
        async def close(self): pass
        async def store_or_merge(self, coll, text, emb, meta, **kw):
            stored.append(meta.get("topic"))
            return {"action": "inserted", "id": "1"}
    async def fake_embed(text):
        return [0.1] * 16
    monkeypatch.setattr(emb_mod, "VectorStore", FakeStore)
    monkeypatch.setattr(emb_mod, "embed_text", fake_embed)
    monkeypatch.setattr(emb_mod, "EMBED_MODEL", "fake")

    old = list(wiki_mod._pending_embeds)
    wiki_mod._pending_embeds.clear()
    try:
        wiki_mod._pending_embeds.append({
            "topic": "queued_topic", "text": "Topic: queued topic\nContent: x",
            "metadata": {"topic": "queued_topic"},
            "queued_at": "2026-08-23T00:00:00"})
        result = await wiki_mod.flush_pending_embeds()
        assert result["drained"] == 1
        assert result["remaining"] == 0
        assert stored == ["queued_topic"]
    finally:
        wiki_mod._pending_embeds.clear()
        wiki_mod._pending_embeds.extend(old)


@pytest.mark.asyncio
async def test_flush_keeps_queue_when_embedder_still_down(db, monkeypatch):
    from tools import embeddings as emb_mod
    async def dead(text):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(emb_mod, "embed_text", dead)

    old = list(wiki_mod._pending_embeds)
    wiki_mod._pending_embeds.clear()
    try:
        wiki_mod._pending_embeds.append({
            "topic": "stuck", "text": "x", "metadata": {},
            "queued_at": "2026-08-23T00:00:00"})
        result = await wiki_mod.flush_pending_embeds()
        assert result["drained"] == 0
        assert len(wiki_mod._pending_embeds) == 1  # kept for later retry
    finally:
        wiki_mod._pending_embeds.clear()
        wiki_mod._pending_embeds.extend(old)


@pytest.mark.asyncio
async def test_deferred_article_becomes_searchable_after_flush(
        db, monkeypatch):
    """End to end: embed fails at write time → queue; embed recovers;
    search flushes and finds the article semantically."""
    conn, db_path = db
    from tools import embeddings as emb_mod
    state = {"up": False}
    async def flaky_embed(text):
        if not state["up"]:
            raise RuntimeError("down")
        return [0.5] + [0.0] * 15
    stored_topics = set()
    class FakeStore:
        def __init__(self, p): pass
        async def initialize(self): pass
        async def close(self): pass
        async def store_or_merge(self, coll, text, emb, meta, **kw):
            stored_topics.add(meta.get("topic"))
            return {"action": "inserted", "id": "1"}
        async def search(self, *a, **k): return []
    monkeypatch.setattr(emb_mod, "embed_text", flaky_embed)
    monkeypatch.setattr(emb_mod, "VectorStore", FakeStore)
    monkeypatch.setattr(emb_mod, "EMBED_MODEL", "fake")

    old = list(wiki_mod._pending_embeds)
    wiki_mod._pending_embeds.clear()
    try:
        wiki = KnowledgeWiki(db_path)
        # Write with the embed server down → deferral.
        async def fail_embed(*a, **k): raise RuntimeError("down")
        monkeypatch.setattr(emb_mod, "embed_text", fail_embed)
        r = await wiki.write_lesson_article(
            conn, topic="deferred_topic", title="D", content="deferred lesson")
        assert r["action"] == "created"
        assert len(wiki_mod._pending_embeds) == 1

        # Embed server recovers.
        async def ok_embed(text): return [0.5] + [0.0] * 15
        monkeypatch.setattr(emb_mod, "embed_text", ok_embed)

        # search() should opportunistically drain the queue.
        hits = await wiki.search(conn, "anything")
        assert stored_topics == {"deferred_topic"}, \
            "deferred article must be embedded on next search"
        assert len(wiki_mod._pending_embeds) == 0
    finally:
        wiki_mod._pending_embeds.clear()
        wiki_mod._pending_embeds.extend(old)


# ── 3. Identity prompt is domain- and provider-general ────────────

def test_identity_prompt_has_no_hardcoded_provider_or_books():
    from tools.hermes_memory import HermesMemory
    ident = HermesMemory()._build_identity()
    assert "Opus" not in ident, "identity must not hardcode a model name"
    assert "DraftKings" not in ident.replace(
        "sports books", ""), "identity must not name books as THE domain"
    assert "general-purpose research agent" in ident
