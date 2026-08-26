"""Knowledge wiki package — split store (persistence/query) from compiler
(LLM compilation/lint).

- ``tools.wiki.store.WikiStore``: SQLite schema, direct writes, search, stats.
- ``tools.wiki.compiler.WikiCompiler``: source gathering + LLM compile + lint.

``tools.knowledge_wiki.KnowledgeWiki`` remains the public façade composing
both, so existing imports keep working.
"""

from tools.wiki.store import WikiStore, WIKI_SCHEMA_SQL, WIKI_COLLECTION, STALE_THRESHOLD_HOURS
from tools.wiki.compiler import (
    WikiCompiler,
    COMPILE_INTERVAL_CYCLES,
    LINT_INTERVAL_CYCLES,
    MAX_SOURCES_PER_COMPILE,
    MAX_ARTICLE_LENGTH,
    _article_confidence,
    _merged_article_confidence,
)

__all__ = [
    "WikiStore",
    "WikiCompiler",
    "WIKI_SCHEMA_SQL",
    "WIKI_COLLECTION",
    "STALE_THRESHOLD_HOURS",
    "COMPILE_INTERVAL_CYCLES",
    "LINT_INTERVAL_CYCLES",
    "MAX_SOURCES_PER_COMPILE",
    "MAX_ARTICLE_LENGTH",
    "_article_confidence",
    "_merged_article_confidence",
]
