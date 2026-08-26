"""Knowledge wiki — store/query layer (no compilation).

Owns the SQLite schema, direct article writes (lesson upserts),
semantic + LIKE search, listing, contradiction reads and stats.
Compilation lives in ``tools.wiki.compiler``; this module never
invokes the LLM.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.wiki.store")

# Collection name for wiki article embeddings in the VectorStore.
WIKI_COLLECTION = "wiki_articles"

STALE_THRESHOLD_HOURS = 72      # Flag articles not updated in 3 days

# ── Schema ──────────────────────────────────────────────

WIKI_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wiki_articles (
    topic TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    related_topics TEXT NOT NULL DEFAULT '[]',
    source_sessions TEXT NOT NULL DEFAULT '[]',
    source_entries TEXT NOT NULL DEFAULT '[]',
    domain TEXT NOT NULL DEFAULT 'GENERAL',
    confidence REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    compile_count INTEGER NOT NULL DEFAULT 1,
    content_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS wiki_contradictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_a TEXT NOT NULL,
    article_b TEXT NOT NULL,
    claim_a TEXT NOT NULL,
    claim_b TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'low',
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(article_a, article_b, claim_a)
);

CREATE TABLE IF NOT EXISTS wiki_compile_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    articles_created INTEGER NOT NULL DEFAULT 0,
    articles_updated INTEGER NOT NULL DEFAULT 0,
    contradictions_found INTEGER NOT NULL DEFAULT 0,
    stale_claims_flagged INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    compiled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_domain ON wiki_articles(domain);
CREATE INDEX IF NOT EXISTS idx_wiki_updated ON wiki_articles(updated_at);
CREATE INDEX IF NOT EXISTS idx_wiki_contradictions_unresolved
    ON wiki_contradictions(resolved) WHERE resolved = 0;
"""


class WikiStore:
    """SQLite-backed store for wiki articles: writes, search, stats.

    No LLM involvement — pure persistence and retrieval. The compile
    pipeline (tools.wiki.compiler.WikiCompiler) composes on top of this.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._initialized = False

    async def initialize(self, db: aiosqlite.Connection) -> None:
        """Create wiki tables if they don't exist."""
        if self._initialized:
            return
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (s.strip() for s in WIKI_SCHEMA_SQL.split(";") if s.strip()):
            await db.execute(stmt)
        await db.commit()
        self._initialized = True

    # ── Article fetch ───────────────────────────────────

    async def get_article(self, db: aiosqlite.Connection, topic: str) -> Optional[dict]:
        """Fetch an existing wiki article by topic."""
        cursor = await db.execute(
            "SELECT topic, title, content, summary, related_topics, "
            "source_sessions, source_entries, domain, confidence, "
            "created_at, updated_at, compile_count, content_hash "
            "FROM wiki_articles WHERE topic = ?",
            (topic,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "topic": row[0], "title": row[1], "content": row[2],
            "summary": row[3], "related_topics": json.loads(row[4]),
            "source_sessions": json.loads(row[5]),
            "source_entries": json.loads(row[6]),
            "domain": row[7], "confidence": row[8],
            "created_at": row[9], "updated_at": row[10],
            "compile_count": row[11], "content_hash": row[12],
        }

    # ── Embedding emission (shared write-side helper) ───

    def article_embed_text(self, topic: str, compiled: dict) -> str:
        """Build a single string that captures the article's semantic payload
        for embedding. Combines title + summary + truncated content so the
        vector represents the gist of the article rather than any one field.
        """
        parts = [f"Topic: {topic.replace('_', ' ')}"]
        if compiled.get("title"):
            parts.append(f"Title: {compiled['title']}")
        if compiled.get("summary"):
            parts.append(f"Summary: {compiled['summary']}")
        if compiled.get("content"):
            parts.append(f"Content: {compiled['content'][:2000]}")
        return "\n".join(parts)

    async def emit_article_embedding(
        self, topic: str, compiled: dict, domain: str, confidence: float,
        source_task_id: Optional[str] = None,
    ) -> None:
        """Write an article's (title + summary + content) into the
        ``wiki_articles`` vector collection. Keyed by topic slug so re-writes
        update the same vector (via near-dup merge above the 0.97 threshold).

        Catches Ollama failures so a dead embed server never blocks a wiki
        write. When that happens we queue the payload for later retry.

        The pending queue lives here (the store), not in the compiler, so
        both the lesson writer and the compiler share one retry backlog.
        """
        from tools.knowledge_wiki import _pending_embeds, _EMBED_QUEUE_MAX

        try:
            from tools.embeddings import (
                VectorStore, embed_text, EMBED_MODEL, NEAR_DUP_THRESHOLD,
            )
        except Exception as e:
            logger.warning(f"Wiki embed skipped (import): {e}")
            return

        text = self.article_embed_text(topic, compiled)
        metadata = {
            "topic": topic,
            "title": compiled.get("title"),
            "domain": domain,
            "confidence": confidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_task_id": source_task_id,
        }

        try:
            embedding = await asyncio.wait_for(embed_text(text), timeout=30.0)
        except Exception as e:
            if len(_pending_embeds) < _EMBED_QUEUE_MAX:
                _pending_embeds.append({
                    "topic": topic, "text": text, "metadata": metadata,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                })
            logger.warning(
                f"Wiki embed deferred for '{topic}': Ollama unavailable ({e}). "
                f"Queue depth={len(_pending_embeds)}."
            )
            return

        store = VectorStore(self.db_path)
        await store.initialize()
        try:
            result = await store.store_or_merge(
                WIKI_COLLECTION, text, embedding, metadata,
                model_name=EMBED_MODEL,
                near_dup_threshold=NEAR_DUP_THRESHOLD,
            )
            if result["action"] == "merged":
                logger.info(
                    f"Wiki embed: merged '{topic}' into existing vector "
                    f"(sim={result['similarity']:.4f})"
                )
            elif result["action"] == "inserted":
                logger.debug(f"Wiki embed: inserted '{topic}' (id={result['id']})")
        except Exception as e:
            logger.warning(f"Wiki embed store failed for '{topic}': {e}")
        finally:
            await store.close()

    # ── WRITE: Direct schema-correct article upsert ─────

    async def write_lesson_article(
        self,
        db: aiosqlite.Connection,
        *,
        topic: str,
        title: str,
        content: str,
        domain: str = "GENERAL",
        related_topics: Optional[list[str]] = None,
        source_sessions: Optional[list[str]] = None,
        source_entries: Optional[list[str]] = None,
        confidence: float = 0.6,
        summary: Optional[str] = None,
    ) -> dict:
        """Schema-correct direct write of a wiki article.

        Unlike the LLM compile path this does NOT invoke the compile
        pipeline — it's for "we already know the lesson, file it" cases
        like LIVE-demotion post-mortems and backtest null results.

        Uses the REAL table schema (topic PK, title, content, summary,
        related_topics, source_sessions, source_entries, domain, confidence,
        created_at, updated_at, compile_count, content_hash).

        Returns ``{"action": "created"|"updated"|"failed", "topic": ..., "error": ...}``.
        On failure the error is logged loudly and the ``_wiki_writes_failed``
        module counter is incremented — never swallowed silently.
        """
        from tools.knowledge_wiki import (
            _wiki_writes_failed, _wiki_writes_succeeded,
        )
        import tools.knowledge_wiki as _kw

        await self.initialize(db)

        related_topics = related_topics or []
        source_sessions = source_sessions or []
        source_entries = source_entries or []
        if summary is None:
            # Use first 280 chars of content as summary
            summary = content[:280] + ("..." if len(content) > 280 else "")
        content_hash = hashlib.md5(content.encode()).hexdigest()[:12]
        now = datetime.now(timezone.utc).isoformat()

        try:
            existing = await self.get_article(db, topic)
            if existing:
                # Merge related_topics + source lists (dedup), bump compile_count
                merged_related = list(dict.fromkeys(
                    existing.get("related_topics", []) + related_topics
                ))
                merged_sessions = list(dict.fromkeys(
                    existing.get("source_sessions", []) + source_sessions
                ))
                merged_entries = list(dict.fromkeys(
                    existing.get("source_entries", []) + source_entries
                ))
                await db.execute(
                    "UPDATE wiki_articles SET title = ?, content = ?, "
                    "summary = ?, related_topics = ?, source_sessions = ?, "
                    "source_entries = ?, domain = ?, confidence = ?, "
                    "updated_at = ?, compile_count = compile_count + 1, "
                    "content_hash = ? WHERE topic = ?",
                    (
                        title, content, summary,
                        json.dumps(merged_related),
                        json.dumps(merged_sessions),
                        json.dumps(merged_entries),
                        domain, round(float(confidence), 3), now,
                        content_hash, topic,
                    ),
                )
                await db.commit()
                _kw._wiki_writes_succeeded += 1
                logger.info(
                    f"Wiki lesson: updated '{topic}' (domain={domain}, "
                    f"compile_count+=1)"
                )
                try:
                    await self.emit_article_embedding(
                        topic,
                        {"title": title, "summary": summary, "content": content},
                        domain, round(float(confidence), 3),
                    )
                except Exception as e:
                    logger.debug(f"Wiki lesson embed deferred for '{topic}': {e}")
                return {"action": "updated", "topic": topic}
            else:
                await db.execute(
                    "INSERT INTO wiki_articles "
                    "(topic, title, content, summary, related_topics, "
                    " source_sessions, source_entries, domain, confidence, "
                    " created_at, updated_at, compile_count, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        topic, title, content, summary,
                        json.dumps(related_topics),
                        json.dumps(source_sessions),
                        json.dumps(source_entries),
                        domain, round(float(confidence), 3),
                        now, now, 1, content_hash,
                    ),
                )
                await db.commit()
                _kw._wiki_writes_succeeded += 1
                logger.info(
                    f"Wiki lesson: created '{topic}' (domain={domain}, "
                    f"{len(content)} chars)"
                )
                # Emit the embedding so the article is retrievable via
                # semantic search — without this the wiki-in-the-loop
                # retrieval path would never find lesson articles.
                try:
                    await self.emit_article_embedding(
                        topic,
                        {"title": title, "summary": summary, "content": content},
                        domain, round(float(confidence), 3),
                    )
                except Exception as e:
                    logger.debug(f"Wiki lesson embed deferred for '{topic}': {e}")
                return {"action": "created", "topic": topic}
        except Exception as e:
            _kw._wiki_writes_failed += 1
            logger.error(
                f"Wiki lesson write FAILED for '{topic}': {type(e).__name__}: {e}. "
                f"writes_failed={_kw._wiki_writes_failed}"
            )
            return {"action": "failed", "topic": topic, "error": str(e)}

    # ── QUERY: Search wiki articles ─────────────────────

    async def search(
        self, db: aiosqlite.Connection, query: str, top_k: int = 10,
        domain: Optional[str] = None, min_similarity: float = 0.0,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """Search wiki articles by SEMANTIC similarity (primary) with a
        keyword LIKE fallback when embeddings are unavailable.

        Args:
            query: natural-language query string
            top_k: max results (aliased by ``limit`` for backcompat)
            domain: optional filter — only return articles with this domain
            min_similarity: floor on cosine similarity (0.0 = no floor)
            limit: deprecated alias for top_k

        Returns: list of ``{topic, title, summary, content, domain,
        confidence, updated_at, similarity}``. ``similarity`` is ``None`` when
        the LIKE fallback path was used.
        """
        await self.initialize(db)
        if limit is not None:
            top_k = limit

        # Primary path: semantic retrieval via VectorStore.
        try:
            from tools.embeddings import VectorStore, embed_text, EMBED_MODEL
            # Embed the query (can fail if Ollama is down).
            query_emb = await asyncio.wait_for(embed_text(query), timeout=20.0)
            store = VectorStore(self.db_path)
            await store.initialize()
            try:
                # Over-fetch so we can post-filter by domain without shrinking
                # the usable result set.
                fetch_k = top_k * 3 if domain else top_k
                hits = await store.search(
                    WIKI_COLLECTION, query_emb, top_k=fetch_k,
                    min_similarity=min_similarity, model_name=EMBED_MODEL,
                )
            finally:
                await store.close()

            if hits:
                # Join back to wiki_articles by topic (from metadata).
                topics_in_order = []
                sim_by_topic = {}
                for h in hits:
                    meta = h.get("metadata") or {}
                    t = meta.get("topic")
                    if t and t not in sim_by_topic:
                        topics_in_order.append(t)
                        sim_by_topic[t] = h["similarity"]
                if topics_in_order:
                    placeholders = ", ".join("?" for _ in topics_in_order)
                    sql = (
                        f"SELECT topic, title, summary, content, domain, confidence, "
                        f"updated_at FROM wiki_articles WHERE topic IN ({placeholders})"
                    )
                    params = list(topics_in_order)
                    if domain:
                        sql += " AND domain = ?"
                        params.append(domain)
                    cursor = await db.execute(sql, params)
                    rows = await cursor.fetchall()
                    by_topic = {r[0]: r for r in rows}
                    out = []
                    for t in topics_in_order:
                        r = by_topic.get(t)
                        if not r:
                            continue
                        out.append({
                            "topic": r[0], "title": r[1], "summary": r[2],
                            "content": r[3], "domain": r[4], "confidence": r[5],
                            "updated_at": r[6],
                            "similarity": round(sim_by_topic[t], 6),
                        })
                        if len(out) >= top_k:
                            break
                    if out:
                        return out
                # Semantic returned hits but none joined to articles — fall through.
                logger.info(
                    "Wiki search: semantic hits had no matching wiki_articles row, "
                    "falling back to LIKE."
                )
        except Exception as e:
            logger.warning(
                f"Wiki search: semantic path failed ({e}); falling back to LIKE."
            )

        # Fallback: legacy keyword LIKE search.
        sql = (
            "SELECT topic, title, summary, content, domain, confidence, updated_at "
            "FROM wiki_articles "
            "WHERE (content LIKE ? OR title LIKE ? OR topic LIKE ?)"
        )
        params = [f"%{query}%", f"%{query}%", f"%{query}%"]
        if domain:
            sql += " AND domain = ?"
            params.append(domain)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(top_k)
        cursor = await db.execute(sql, params)
        return [
            {
                "topic": r[0], "title": r[1], "summary": r[2],
                "content": r[3], "domain": r[4], "confidence": r[5],
                "updated_at": r[6], "similarity": None,
            }
            for r in await cursor.fetchall()
        ]

    async def get_specific_article(self, db: aiosqlite.Connection, topic: str) -> Optional[dict]:
        """Get a specific wiki article."""
        await self.initialize(db)
        return await self.get_article(db, topic)

    async def list_articles(
        self, db: aiosqlite.Connection, domain: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        """List wiki articles, optionally filtered by domain."""
        await self.initialize(db)
        if domain:
            cursor = await db.execute(
                "SELECT topic, title, summary, domain, confidence, updated_at, compile_count "
                "FROM wiki_articles WHERE domain = ? ORDER BY updated_at DESC LIMIT ?",
                (domain, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT topic, title, summary, domain, confidence, updated_at, compile_count "
                "FROM wiki_articles ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        return [
            {
                "topic": r[0], "title": r[1], "summary": r[2],
                "domain": r[3], "confidence": r[4], "updated_at": r[5],
                "compile_count": r[6],
            }
            for r in await cursor.fetchall()
        ]

    async def get_contradictions(
        self, db: aiosqlite.Connection, unresolved_only: bool = True
    ) -> list[dict]:
        """Get contradiction findings."""
        await self.initialize(db)
        where = "WHERE resolved = 0" if unresolved_only else ""
        cursor = await db.execute(
            f"SELECT id, article_a, article_b, claim_a, claim_b, severity, "
            f"resolved, resolution, detected_at, resolved_at "
            f"FROM wiki_contradictions {where} ORDER BY detected_at DESC LIMIT 50"
        )
        return [
            {
                "id": r[0], "article_a": r[1], "article_b": r[2],
                "claim_a": r[3], "claim_b": r[4], "severity": r[5],
                "resolved": bool(r[6]), "resolution": r[7],
                "detected_at": r[8], "resolved_at": r[9],
            }
            for r in await cursor.fetchall()
        ]

    async def get_stats(self, db: aiosqlite.Connection) -> dict:
        """Get wiki statistics."""
        await self.initialize(db)
        cursor = await db.execute("SELECT COUNT(*) FROM wiki_articles")
        total_articles = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM wiki_contradictions WHERE resolved = 0"
        )
        unresolved_contradictions = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT SUM(compile_count) FROM wiki_articles")
        row = await cursor.fetchone()
        total_compiles = row[0] if row and row[0] else 0

        cursor = await db.execute(
            "SELECT domain, COUNT(*) FROM wiki_articles GROUP BY domain"
        )
        by_domain = {r[0]: r[1] for r in await cursor.fetchall()}

        stale_threshold = (
            datetime.now(timezone.utc) - timedelta(hours=STALE_THRESHOLD_HOURS)
        ).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM wiki_articles WHERE updated_at < ?",
            (stale_threshold,),
        )
        stale_count = (await cursor.fetchone())[0]

        return {
            "total_articles": total_articles,
            "total_compiles": total_compiles,
            "unresolved_contradictions": unresolved_contradictions,
            "stale_articles": stale_count,
            "by_domain": by_domain,
        }
