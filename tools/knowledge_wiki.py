"""
Knowledge Wiki — LLM-compiled persistent knowledge base for Callisto.

Inspired by Karpathy's LLM Wiki pattern. Instead of stateless RAG (re-discover
everything each query), this module incrementally compiles raw research results
into persistent, cross-referenced wiki articles. Knowledge compounds.

Three operations:
  1. COMPILE — Read recent sessions/evidence/learnings, synthesize into articles
  2. LINT    — Scan for contradictions, stale claims, missing cross-references
  3. QUERY   — Search wiki articles by topic/keyword (replaces ad-hoc world queries)

Articles are stored in SQLite (wiki_articles table). Each article has:
  - A topic slug (e.g., "mlb_home_fav_edge", "nba_q4_collapse")
  - Compiled content with cross-references to related articles
  - Source trail (which sessions/evidence entries contributed)
  - Staleness score (how old is the newest contributing evidence?)
  - Confidence (inherited from AGP — weighted average of source confidence)

Compilation uses Gemma 4 (local, free) by default. Claude is NOT required.

SPLIT (2026-08): the store/query layer lives in ``tools.wiki.store`` and the
compilation/lint layer in ``tools.wiki.compiler``. This module keeps the
public ``KnowledgeWiki`` façade composing both, plus module-level counters
and the pending-embed queue shared by both layers, so existing imports
(``from tools.knowledge_wiki import KnowledgeWiki``, ``get_write_stats``,
monkeypatched counters in tests) keep working unchanged.
"""

import os
from typing import Optional

from tools.wiki.store import (
    WikiStore,
    WIKI_COLLECTION,
    WIKI_SCHEMA_SQL,
    STALE_THRESHOLD_HOURS,
)
from tools.wiki.compiler import (
    WikiCompiler,
    COMPILE_INTERVAL_CYCLES,
    LINT_INTERVAL_CYCLES,
    MAX_SOURCES_PER_COMPILE,
    MAX_ARTICLE_LENGTH,
    _article_confidence,
    _merged_article_confidence,
)

logger = __import__("logging").getLogger("callisto.wiki")

# Pending-embedding queue: when Ollama is down, article writes stash the
# payload here so a later retry can catch up without blocking (or failing)
# the write path. Bounded to prevent runaway. Lives on the façade so both
# store and compiler share one backlog.
_EMBED_QUEUE_MAX = 500
_pending_embeds: list[dict] = []

# Wiki write telemetry — bumped on every direct-write (bypasses LLM compile).
# Pre-2026-04-22 the demotion writer used the wrong schema and every call
# failed silently inside a bare ``except Exception: pass``; these counters
# make future silent failures loud.
_wiki_writes_succeeded: int = 0
_wiki_writes_failed: int = 0


def get_write_stats() -> dict:
    """Expose wiki direct-write counters for /health-style introspection."""
    return {
        "succeeded": _wiki_writes_succeeded,
        "failed": _wiki_writes_failed,
    }


class KnowledgeWiki:
    """LLM-compiled persistent knowledge base.

    Façade composing the split layers:
      - ``self.store``    → tools.wiki.store.WikiStore (persistence + query)
      - ``self.compile``  → tools.wiki.compiler.WikiCompiler (LLM compile/lint)
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.store = WikiStore(db_path)
        self._compiler = WikiCompiler(self.store)
        # Route the store's article fetch through this facade's
        # ``_get_article`` hook so callers/tests can override
        # KnowledgeWiki._get_article and have all layers observe it.
        object.__setattr__(self.store, "get_article",
                           lambda db_, topic: self._get_article(db_, topic))
        # Compile dispatch goes through KnowledgeWiki._llm_compile so class
        # level monkeypatches in tests affect the compiler.
        async def _llm_compile_dispatch(topic, sources, existing_content):
            return await KnowledgeWiki._llm_compile(
                self, topic, sources, existing_content,
            )
        object.__setattr__(self._compiler, "_external_llm_compile",
                           _llm_compile_dispatch)

    # ── Store passthrough ───────────────────────────

    @property
    def _initialized(self) -> bool:
        return self.store._initialized

    @_initialized.setter
    def _initialized(self, value: bool) -> None:
        self.store._initialized = value

    async def initialize(self, db) -> None:
        await self.store.initialize(db)

    async def write_lesson_article(self, db, **kwargs) -> dict:
        return await self.store.write_lesson_article(db, **kwargs)

    async def search(self, db, *args, **kwargs) -> list[dict]:
        return await self.store.search(db, *args, **kwargs)

    async def get_article(self, db, topic: str):
        return await self.store.get_specific_article(db, topic)

    async def list_articles(self, db, *args, **kwargs) -> list[dict]:
        return await self.store.list_articles(db, *args, **kwargs)

    async def get_contradictions(self, db, *args, **kwargs) -> list[dict]:
        return await self.store.get_contradictions(db, *args, **kwargs)

    async def get_stats(self, db) -> dict:
        return await self.store.get_stats(db)

    # Back-compat alias used by tests/callers that reached into internals.
    async def _get_article(self, db, topic: str):
        return await WikiStore.get_article(self.store, db, topic)

    async def _llm_compile(self, *args, **kwargs):
        """Back-compat alias for the compiler's LLM compile step (tests
        monkeypatch this on KnowledgeWiki)."""
        return await self._compiler._llm_compile(*args, **kwargs)

    async def _get_uncompiled_sources(self, db):
        return await self._compiler._get_uncompiled_sources(db)

    async def _create_article(self, db, topic, sources, source_task_id=None):
        return await self._compiler._create_article(
            db, topic, sources, source_task_id=source_task_id,
        )

    async def _update_article(self, db, topic, existing, new_sources,
                              source_task_id=None):
        return await self._compiler._update_article(
            db, topic, existing, new_sources, source_task_id=source_task_id,
        )

    async def _get_uncompiled_sources(self, db):
        return await self._compiler._get_uncompiled_sources(db)

    async def _emit_article_embedding(self, *args, **kwargs) -> None:
        """Back-compat alias for store.emit_article_embedding."""
        return await WikiStore.emit_article_embedding(self.store, *args, **kwargs)

    async def emit_article_embedding(self, *args, **kwargs) -> None:
        return await self.store.emit_article_embedding(*args, **kwargs)

    # ── Compiler passthrough ────────────────────────

    async def compile(self, db, cycle: int) -> dict:
        return await self._compiler.compile(db, cycle)

    async def lint(self, db, cycle: int) -> dict:
        return await self._compiler.lint(db, cycle)

    async def file_task_result(
        self, db, query: str, conclusion: str,
        confidence: float, domain: str, task_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Auto-file a task/query result into the wiki.

        Called when a /task completes. The conclusion gets compiled into
        the relevant wiki article, so exploration compounds.

        ``task_id`` is the REAL id from ``task_queue`` — required for lineage
        joins. When None (legacy callers), we log a warning but still write
        using a synthetic id so we don't drop the knowledge on the floor.

        ``session_id`` is the AGP ``sessions.session_id`` when available.

        Returns the topic slug it was filed under, or None.
        """
        import time as _time
        import logging as _logging
        _log = _logging.getLogger("callisto.wiki")

        await self.initialize(db)

        if not task_id:
            _log.warning(
                "Wiki.file_task_result: called without task_id — lineage will be "
                "incomplete. Caller should pass the real task_queue id."
            )
            source_id = f"task_anon_{int(_time.time())}"
        else:
            source_id = session_id or f"task_{task_id}"

        source = {
            "type": "session",
            "id": source_id,
            "query": query,
            "domain": domain,
            "content": conclusion,
            "confidence": confidence,
            "timestamp": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
        }

        topic = self._compiler._extract_topic(source)

        try:
            existing = await self.store.get_article(db, topic)
            if existing:
                await self._compiler._update_article(
                    db, topic, existing, [source], source_task_id=task_id,
                )
            else:
                await self._compiler._create_article(
                    db, topic, [source], source_task_id=task_id,
                )

            _log.info(f"Wiki: filed task result under '{topic}' (task={task_id})")
            return topic
        except Exception as e:
            _log.warning(f"Wiki: failed to file task result: {e}")
            return None


# ── Module-level singleton ──────────────────────────────

_wiki: Optional[KnowledgeWiki] = None


def get_wiki(db_path: Optional[str] = None) -> KnowledgeWiki:
    """Get or create the wiki singleton."""
    global _wiki
    if _wiki is None:
        path = db_path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        _wiki = KnowledgeWiki(path)
    return _wiki
