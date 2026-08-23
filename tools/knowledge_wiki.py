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
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.wiki")

# Collection name for wiki article embeddings in the VectorStore.
WIKI_COLLECTION = "wiki_articles"

# Pending-embedding queue: when Ollama is down, _create_article/_update_article
# stash the (topic, text, metadata) tuple here so a later retry can catch up
# without blocking (or failing) the write path. Bounded to prevent runaway.
_EMBED_QUEUE_MAX = 500
_pending_embeds: list[dict] = []
_embed_queue_stats = {"queued": 0, "drained": 0, "dropped": 0}


def get_embed_queue_depth() -> int:
    """Current depth of the deferred-embedding retry queue."""
    return len(_pending_embeds)


async def flush_pending_embeds(max_items: Optional[int] = None) -> dict:
    """Retry deferred article embeddings (queued while Ollama was down).

    Without this the pending-embeds queue was write-only: articles whose
    embedding was deferred were permanently invisible to semantic search —
    they existed only in SQL and only via the LIKE fallback.

    Called opportunistically before each wiki search; safe to call from any
    task. Returns {attempted, drained, remaining}.
    """
    if not _pending_embeds:
        return {"attempted": 0, "drained": 0, "remaining": 0}
    try:
        from tools.embeddings import (
            VectorStore, embed_text, EMBED_MODEL, NEAR_DUP_THRESHOLD,
        )
    except Exception as e:  # noqa: BLE001 — embeddings module unavailable
        logger.debug(f"flush_pending_embeds: embeddings unavailable: {e}")
        return {"attempted": 0, "drained": 0, "remaining": len(_pending_embeds)}

    items = list(_pending_embeds)
    if max_items is not None:
        items = items[:max_items]
    attempted = drained = 0
    store: Optional[VectorStore] = None
    for item in items:
        try:
            embedding = await asyncio.wait_for(
                embed_text(item["text"]), timeout=30.0)
        except Exception:
            break  # embed server still down — keep the queue intact
        if store is None:
            store = VectorStore(
                os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"))
            await store.initialize()
        try:
            await store.store_or_merge(
                WIKI_COLLECTION, item["text"], embedding, item["metadata"],
                model_name=EMBED_MODEL,
                near_dup_threshold=NEAR_DUP_THRESHOLD,
            )
            _pending_embeds.remove(item)
            drained += 1
            _embed_queue_stats["drained"] += 1
        except Exception as e:  # noqa: BLE001 — one bad payload must not stop the drain
            logger.warning(f"flush_pending_embeds: store failed for "
                           f"'{item.get('topic')}': {e}")
            _pending_embeds.remove(item)
            _embed_queue_stats["dropped"] += 1
        finally:
            attempted += 1
    if store is not None:
        await store.close()
    if drained:
        logger.info(f"flush_pending_embeds: drained {drained}/{attempted} "
                    f"deferred article embeddings; {len(_pending_embeds)} remain")
    return {"attempted": attempted, "drained": drained,
            "remaining": len(_pending_embeds)}

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

# ── Compilation intervals ──────────────────────────────

COMPILE_INTERVAL_CYCLES = 7     # Compile every 7 research cycles (~35 min)
LINT_INTERVAL_CYCLES = 11       # Lint every 11 cycles (~55 min, coprime with compile)
MAX_SOURCES_PER_COMPILE = 30    # Don't overwhelm the local model
MAX_ARTICLE_LENGTH = 4000       # Characters — keep articles focused
STALE_THRESHOLD_HOURS = 72      # Flag articles not updated in 3 days


# ── Article confidence: min-of-sources, never the mean ───────────────────
#
# Averaging identical uncorroborated sources manufactures corroboration
# (instance4 finding: two 0.75 SECONDARY items averaged into a CORROBORATED
# article). An article is a retrieval aid, not evidence — it must never be
# stronger than the weakest thing that fed it.

def _article_confidence(sources: list[dict]) -> float:
    """MIN of source confidences (round to 3dp like the writers do).

    Two fail-closed defaults (red-team R7):
      - an article compiled from NO sources has earned nothing — 0.0, not
        the historical 0.5 that happened to sit at the compile-admission
        threshold and let an empty article look compile-worthy;
      - a source dict with no confidence field counts as 0.0, never as a
        silent 0.5 floor for the whole article. Omitting the field must
        pull confidence DOWN, not hold it up.
    """
    if not sources:
        return 0.0
    return round(min(float(s.get("confidence", 0.0) or 0.0) for s in sources), 3)


def _merged_article_confidence(*, existing_confidence: float, compile_count: int,
                               new_sources: list[dict]) -> float:
    """Confidence after merging new sources into an existing article.

    The historical weighted average is computed for continuity, but the
    result is clamped to the weakest current input: min(existing, new-min).
    New garbage pulls an article down promptly instead of glacially; new
    strong sources cannot lift it above what is already there plus their
    own weakness. Missing-confidence sources count as 0.0 (fail closed,
    same rule as _article_confidence).
    """
    if not new_sources:
        return round(float(existing_confidence), 3)
    old_weight = max(int(compile_count), 1)
    new_confs = [float(s.get("confidence", 0.0) or 0.0) for s in new_sources]
    new_conf = sum(new_confs) / len(new_confs)
    weighted = (float(existing_confidence) * old_weight + new_conf) / (old_weight + 1)
    floor_of_inputs = min(float(existing_confidence), min(new_confs))
    return round(min(weighted, floor_of_inputs), 3)


# Task classes this layer uses with the ProviderRouter (config/providers.yaml).
# Model-per-purpose stays a CONFIG concern — never a hardcoded provider.
TASK_CLASS_COMPILE = "knowledge_compile"   # article compilation   (synthesis-shaped)
TASK_CLASS_LINT = "knowledge_lint"         # contradiction pairing (classification-shaped)

# Structured-output schemas shared by the routed and legacy paths.
_COMPILE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "content": {"type": "string"},
        "related_topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "content"],
}

_LINT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair": {"type": "integer"},
                    "article_a": {"type": "string"},
                    "article_b": {"type": "string"},
                    "claim_a": {"type": "string"},
                    "claim_b": {"type": "string"},
                    "severity": {"type": "string"},
                },
            },
        },
    },
    "required": ["contradictions"],
}


class KnowledgeWiki:
    """LLM-compiled persistent knowledge base."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._initialized = False

    async def _routed_json(
        self, task_class: str, prompt: str, schema: dict, temperature: float,
        timeout: float = 180.0,
    ) -> Optional[dict]:
        """One structured completion via the ProviderRouter, parsed as JSON.

        Returns None when the router is unavailable or every endpoint fails —
        callers then fall back to the legacy direct-Ollama path, so behaviour
        degrades instead of breaking where providers.yaml is absent.
        """
        try:
            from inference import get_router
            router = get_router()
        except Exception as e:  # noqa: BLE001 — degrade, don't crash the loop
            logger.info(f"Wiki: router unavailable for {task_class} ({e}); using legacy path")
            return None
        try:
            resp = await asyncio.wait_for(
                router.complete(
                    task_class,
                    [{"role": "user", "content": prompt}],
                    schema=schema,
                    temperature=temperature,
                ),
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Wiki: routed {task_class} failed ({e}); trying legacy path")
            return None
        parsed = resp.get("parsed_json")
        if isinstance(parsed, dict):
            return parsed
        from inference import _parse_json_response
        return _parse_json_response(resp.get("content") or "")

    async def initialize(self, db: aiosqlite.Connection) -> None:
        """Create wiki tables if they don't exist."""
        if self._initialized:
            return
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (s.strip() for s in WIKI_SCHEMA_SQL.split(";") if s.strip()):
            await db.execute(stmt)
        await db.commit()
        self._initialized = True

    # ──────────────────────────────────────────────────
    # COMPILE: Synthesize raw research into wiki articles
    # ──────────────────────────────────────────────────

    async def compile(self, db: aiosqlite.Connection, cycle: int) -> dict:
        """
        Incremental compilation: read recent evidence/sessions, update wiki.

        Returns stats dict: {articles_created, articles_updated, duration_seconds}
        """
        await self.initialize(db)
        start = time.monotonic()
        stats = {"articles_created": 0, "articles_updated": 0}

        # 1. Find recent sessions not yet compiled into wiki
        uncompiled = await self._get_uncompiled_sources(db)
        if not uncompiled:
            logger.debug("Wiki compile: no new sources to compile")
            return stats

        logger.info(f"Wiki compile: {len(uncompiled)} new sources to process")

        # 2. Group sources by topic (extracted from hypothesis names, session queries, etc.)
        topic_groups = self._group_by_topic(uncompiled)

        # 3. For each topic group, compile or update the wiki article
        for topic, sources in topic_groups.items():
            try:
                existing = await self._get_article(db, topic)
                if existing:
                    await self._update_article(db, topic, existing, sources)
                    stats["articles_updated"] += 1
                else:
                    await self._create_article(db, topic, sources)
                    stats["articles_created"] += 1
            except Exception as e:
                logger.warning(f"Wiki compile failed for topic '{topic}': {e}")

        # 4. Update cross-references between articles
        await self._update_cross_references(db)

        # 5. Log compilation
        duration = time.monotonic() - start
        stats["duration_seconds"] = round(duration, 1)
        await db.execute(
            "INSERT INTO wiki_compile_log (cycle, articles_created, articles_updated, "
            "duration_seconds, compiled_at) VALUES (?, ?, ?, ?, ?)",
            (cycle, stats["articles_created"], stats["articles_updated"],
             duration, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()

        logger.info(
            f"Wiki compile done: {stats['articles_created']} created, "
            f"{stats['articles_updated']} updated in {duration:.1f}s"
        )
        return stats

    async def _get_uncompiled_sources(self, db: aiosqlite.Connection) -> list[dict]:
        """Get recent sessions and evidence not yet in any wiki article.

        Epistemics (instance4 mechanism 3): only seal-verified bytes are
        admitted as sessions. A row with a seal_hash that fails
        AGPSession.verify_seal is REJECTED (tampered or corrupt). A row with
        seal_hash NULL (pre-keying legacy) is admitted but marked
        provenance_class=INFERRED with confidence capped at the INFERRED
        ceiling — legacy content may inform compilation, never inflate it.
        """
        from agp import AGPSession
        from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

        inferred_cap = MAX_CONFIDENCE_BY_SOURCE["INFERRED"]

        # Get the latest compile timestamp
        cursor = await db.execute(
            "SELECT MAX(compiled_at) FROM wiki_compile_log"
        )
        row = await cursor.fetchone()
        last_compile = row[0] if row and row[0] else "2000-01-01T00:00:00"

        sources = []

        # Recent AGP sessions with conclusions — seal-gated
        cursor = await db.execute(
            "SELECT session_id, query, domain, conclusion, confidence_score, "
            "sealed_at, full_session, seal_hash "
            "FROM sessions WHERE sealed_at > ? AND conclusion IS NOT NULL "
            "ORDER BY sealed_at DESC LIMIT ?",
            (last_compile, MAX_SOURCES_PER_COMPILE),
        )
        rejected = 0
        for row in await cursor.fetchall():
            (sid, query, domain, conclusion, conf, sealed_at,
             full_session_json, seal_hash) = row
            if seal_hash:
                try:
                    stored = json.loads(full_session_json) if full_session_json else {}
                except (TypeError, ValueError):
                    stored = {}
                stored.setdefault("seal_hash", seal_hash)
                if not AGPSession.verify_seal(stored):
                    rejected += 1
                    continue  # seal present but fails → tampered/corrupt
                provenance_class = None  # seal-verified: keep stored confidence
            else:
                provenance_class = "INFERRED"  # legacy unsealed row
            if provenance_class == "INFERRED":
                conf = min(float(conf or 0.5), inferred_cap)
            sources.append({
                "type": "session",
                "id": sid,
                "query": query,
                "domain": domain,
                "content": conclusion,
                "confidence": conf or 0.5,
                "provenance_class": provenance_class,
                "timestamp": sealed_at,
            })
        if rejected:
            logger.warning(
                f"Wiki compile: rejected {rejected} session(s) with failing seals"
            )

        # Recent high-confidence evidence entries
        cursor = await db.execute(
            "SELECT entry_id, content, domain, confidence_score, source_name, created_at "
            "FROM catalogue WHERE created_at > ? AND confidence_score >= 0.6 "
            "ORDER BY created_at DESC LIMIT ?",
            (last_compile, MAX_SOURCES_PER_COMPILE),
        )
        for row in await cursor.fetchall():
            sources.append({
                "type": "evidence",
                "id": str(row[0]),
                "query": "",
                "domain": row[2],
                "content": row[1],
                "confidence": row[3],
                "timestamp": row[5],
            })

        # Recent hermes learnings
        cursor = await db.execute(
            "SELECT key, value, confidence, learned_at "
            "FROM hermes_learnings WHERE learned_at > ? AND confidence >= 0.5 "
            "ORDER BY learned_at DESC LIMIT ?",
            (last_compile, MAX_SOURCES_PER_COMPILE),
        )
        for row in await cursor.fetchall():
            sources.append({
                "type": "learning",
                "id": row[0],
                "query": row[0],
                "domain": "GENERAL",
                "content": row[1],
                "confidence": row[2],
                "timestamp": row[3],
            })

        return sources

    def _group_by_topic(self, sources: list[dict]) -> dict[str, list[dict]]:
        """Group sources into topic clusters based on content similarity.

        Uses keyword extraction — no embeddings needed. Topics are slugified
        hypothesis names, sport+market combos, or domain categories.
        """
        groups: dict[str, list[dict]] = {}

        for src in sources:
            topic = self._extract_topic(src)
            if topic not in groups:
                groups[topic] = []
            groups[topic].append(src)

        return groups

    def _extract_topic(self, source: dict) -> str:
        """Extract a topic slug from a source entry.

        Strategy:
          - Hypothesis names → use directly (e.g., "mlb_early_home_fav")
          - Session queries → extract sport + market type
          - Evidence → extract domain + key terms
          - Learnings → use the key directly
        """
        content = (source.get("query", "") + " " + source.get("content", "")).lower()

        # Hypothesis-derived topics (most specific)
        # Look for hypothesis name patterns: sport_descriptor_market
        import re
        hyp_match = re.search(r'\b((?:mlb|nba|nfl|nhl|ncaab|ncaaw|soccer)_[a-z_]+)\b', content)
        if hyp_match:
            return hyp_match.group(1)

        # Sport + market type
        sports = {"mlb": "mlb", "nba": "nba", "nfl": "nfl", "nhl": "nhl",
                  "baseball": "mlb", "basketball": "nba", "football": "nfl", "hockey": "nhl",
                  "ncaab": "ncaab", "ncaaw": "ncaaw", "soccer": "soccer"}
        markets = {"spread": "spread", "moneyline": "moneyline", "h2h": "moneyline",
                   "total": "totals", "over": "totals", "under": "totals",
                   "prop": "props", "player": "props", "parlay": "parlays",
                   "sgp": "sgp", "alt": "alt_lines", "live": "live",
                   "boost": "boosts", "free_bet": "free_bets"}

        found_sport = None
        for key, slug in sports.items():
            if key in content:
                found_sport = slug
                break

        found_market = None
        for key, slug in markets.items():
            if key in content:
                found_market = slug
                break

        if found_sport and found_market:
            return f"{found_sport}_{found_market}"
        if found_sport:
            return f"{found_sport}_general"

        # Domain-level fallback
        domain = source.get("domain", "GENERAL").lower()

        # Pipeline/system topics
        system_keywords = {
            "pipeline": "system_pipeline", "backtest": "system_backtesting",
            "self-repair": "system_self_repair", "integrity": "system_integrity",
            "paper trad": "system_paper_trading", "promotion": "system_promotion",
            "odds": "market_odds", "line movement": "market_line_movement",
            "edge": "market_edges", "devig": "market_devig",
            "injury": "context_injuries", "weather": "context_weather",
            "regime": "market_regimes",
        }
        for key, slug in system_keywords.items():
            if key in content:
                return slug

        return f"{domain}_misc"

    async def _get_article(self, db: aiosqlite.Connection, topic: str) -> Optional[dict]:
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

    async def _create_article(
        self, db: aiosqlite.Connection, topic: str, sources: list[dict],
        source_task_id: Optional[str] = None,
    ) -> None:
        """Create a new wiki article from sources using local LLM."""
        compiled = await self._llm_compile(topic, sources, existing_content=None)
        if not compiled:
            return

        now = datetime.now(timezone.utc).isoformat()
        session_ids = [s["id"] for s in sources if s["type"] == "session"]
        entry_ids = [s["id"] for s in sources if s["type"] in ("evidence", "learning")]
        # Min-of-sources: an article is only as strong as its weakest input.
        avg_confidence = _article_confidence(sources)
        domain = self._best_domain(sources)
        content_hash = hashlib.md5(compiled["content"].encode()).hexdigest()[:12]

        # Insert. Include source_task_id only when the column exists (post-migration).
        cols, vals, placeholders = self._insert_columns_for(db, source_task_id)
        base_cols = [
            "topic", "title", "content", "summary", "related_topics",
            "source_sessions", "source_entries", "domain", "confidence",
            "created_at", "updated_at", "compile_count", "content_hash",
        ]
        base_vals = [
            topic, compiled["title"], compiled["content"], compiled["summary"],
            json.dumps(compiled.get("related_topics", [])),
            json.dumps(session_ids), json.dumps(entry_ids),
            domain, round(avg_confidence, 3), now, now, 1, content_hash,
        ]
        if "source_task_id" in cols:
            base_cols.append("source_task_id")
            base_vals.append(source_task_id)
        sql = (
            f"INSERT INTO wiki_articles ({', '.join(base_cols)}) "
            f"VALUES ({', '.join('?' for _ in base_vals)})"
        )
        await db.execute(sql, tuple(base_vals))
        await db.commit()
        logger.info(
            f"Wiki: created article '{topic}' ({len(compiled['content'])} chars, "
            f"task={source_task_id or 'none'})"
        )

        # Emit the article as a semantic embedding. Non-blocking on Ollama
        # failure — the article row is already persisted above.
        await self._emit_article_embedding(
            topic, compiled, domain, round(avg_confidence, 3),
            source_task_id=source_task_id,
        )

    def _best_domain(self, sources: list[dict]) -> str:
        """Pick the most common domain across sources (not just sources[0]).

        Replaces the old ``sources[0].get("domain", "GENERAL")`` behaviour,
        which was order-dependent and mis-classified Hermes learnings that
        are hardcoded to ``GENERAL``.
        """
        from collections import Counter
        domains = [s.get("domain") or "GENERAL" for s in sources]
        # Prefer non-GENERAL when there's a tie, since GENERAL is the
        # "I don't know" bucket (Hermes learnings, uncategorised tasks).
        counts = Counter(domains)
        if len(counts) > 1 and "GENERAL" in counts:
            non_general = [d for d in domains if d != "GENERAL"]
            if non_general:
                return Counter(non_general).most_common(1)[0][0]
        return counts.most_common(1)[0][0]

    def _insert_columns_for(self, db: aiosqlite.Connection, _task_id) -> tuple:
        """Stub — actual column introspection happens lazily inside the write.

        Historically this used a PRAGMA probe; that's unnecessary here because
        ``_safe_add_column`` runs at startup and always adds ``source_task_id``.
        We keep the hook so the caller remains explicit and a future schema
        change can gate behaviour here without touching call sites.
        """
        return ({"source_task_id"}, None, None)

    async def _update_article(
        self, db: aiosqlite.Connection, topic: str, existing: dict,
        new_sources: list[dict], source_task_id: Optional[str] = None,
    ) -> None:
        """Update an existing wiki article with new sources."""
        compiled = await self._llm_compile(topic, new_sources, existing_content=existing["content"])
        if not compiled:
            return

        now = datetime.now(timezone.utc).isoformat()

        # Merge source lists (dedup)
        session_ids = list(set(
            existing["source_sessions"] +
            [s["id"] for s in new_sources if s["type"] == "session"]
        ))
        entry_ids = list(set(
            existing["source_entries"] +
            [s["id"] for s in new_sources if s["type"] in ("evidence", "learning")]
        ))

        # Weighted confidence: existing weight = compile_count, new = 1 —
        # then clamped to the weakest current input (min-of-sources). New
        # garbage demotes promptly; nothing can exceed its weakest source.
        merged_conf = _merged_article_confidence(
            existing_confidence=existing["confidence"],
            compile_count=existing["compile_count"],
            new_sources=new_sources,
        )

        content_hash = hashlib.md5(compiled["content"].encode()).hexdigest()[:12]

        if source_task_id:
            await db.execute(
                "UPDATE wiki_articles SET content = ?, summary = ?, title = ?, "
                "related_topics = ?, source_sessions = ?, source_entries = ?, "
                "confidence = ?, updated_at = ?, compile_count = compile_count + 1, "
                "content_hash = ?, source_task_id = ? WHERE topic = ?",
                (compiled["content"], compiled["summary"], compiled["title"],
                 json.dumps(compiled.get("related_topics", [])),
                 json.dumps(session_ids), json.dumps(entry_ids),
                 round(merged_conf, 3), now, content_hash, source_task_id, topic),
            )
        else:
            await db.execute(
                "UPDATE wiki_articles SET content = ?, summary = ?, title = ?, "
                "related_topics = ?, source_sessions = ?, source_entries = ?, "
                "confidence = ?, updated_at = ?, compile_count = compile_count + 1, "
                "content_hash = ? WHERE topic = ?",
                (compiled["content"], compiled["summary"], compiled["title"],
                 json.dumps(compiled.get("related_topics", [])),
                 json.dumps(session_ids), json.dumps(entry_ids),
                 round(merged_conf, 3), now, content_hash, topic),
            )
        await db.commit()
        logger.info(
            f"Wiki: updated article '{topic}' (compile #{existing['compile_count'] + 1}, "
            f"task={source_task_id or 'none'})"
        )

        # Re-embed the merged content so semantic search reflects the update.
        await self._emit_article_embedding(
            topic, compiled, existing.get("domain", "GENERAL"), round(merged_conf, 3),
            source_task_id=source_task_id,
        )

    async def _emit_article_embedding(
        self, topic: str, compiled: dict, domain: str, confidence: float,
        source_task_id: Optional[str] = None,
    ) -> None:
        """Write an article's (title + summary + content) into the
        ``wiki_articles`` vector collection. Keyed by topic slug so re-writes
        update the same vector (via near-dup merge above the 0.97 threshold).

        Catches Ollama failures so a dead embed server never blocks a wiki
        write. When that happens we queue the payload for later retry.
        """
        try:
            from tools.embeddings import (
                VectorStore, embed_text, EMBED_MODEL, NEAR_DUP_THRESHOLD,
            )
        except Exception as e:
            logger.warning(f"Wiki embed skipped (import): {e}")
            return

        text = self._article_embed_text(topic, compiled)
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
                _embed_queue_stats["queued"] += 1
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

    def _article_embed_text(self, topic: str, compiled: dict) -> str:
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

    async def _llm_compile(
        self, topic: str, sources: list[dict], existing_content: Optional[str]
    ) -> Optional[dict]:
        """Compile sources into a wiki article via an LLM.

        Primary: ProviderRouter (task class ``knowledge_compile`` — model is
        whatever providers.yaml routes). Fallback when no router: direct
        Ollama with a hardcoded local model. Final fallback: template.

        Returns {"title", "content", "summary", "related_topics"} or None on failure.
        """

        # Build source material summary
        source_text = []
        for s in sources[:15]:  # Cap to avoid context overflow
            label = f"[{s['type'].upper()}] (conf={s['confidence']:.2f})"
            source_text.append(f"{label}\n{s['content'][:800]}")
        sources_block = "\n---\n".join(source_text)

        update_instruction = ""
        if existing_content:
            # Truncate existing to leave room for new sources
            existing_truncated = existing_content[:2000]
            update_instruction = (
                f"\n\nEXISTING ARTICLE TO UPDATE:\n{existing_truncated}\n\n"
                "Integrate the new sources into the existing article. "
                "Update claims if new evidence contradicts old ones. "
                "Add new findings. Remove stale information."
            )

        prompt = (
            f"You are a knowledge compiler for an autonomous research system.\n\n"
            f"TOPIC: {topic}\n\n"
            f"NEW SOURCES:\n{sources_block}\n"
            f"{update_instruction}\n\n"
            f"Compile these sources into a structured wiki article. Output ONLY valid JSON:\n"
            f'{{"title": "Human-readable title",'
            f' "summary": "1-2 sentence summary of key findings",'
            f' "content": "Full article with sections. Use ## headers. Include key stats, '
            f'dates, confidence levels. Max {MAX_ARTICLE_LENGTH} chars.",'
            f' "related_topics": ["topic_slug_1", "topic_slug_2"]}}\n\n'
            f"Rules:\n"
            f"- Be factual — only include claims supported by the sources\n"
            f"- Include specific numbers (win rates, p-values, edge %, dates)\n"
            f"- Flag uncertainty: 'Evidence suggests...' vs 'Confirmed:'\n"
            f"- related_topics: other topic slugs this connects to\n"
            f"- Keep content under {MAX_ARTICLE_LENGTH} characters"
        )

        # Primary path: the ProviderRouter. Model-per-purpose is a config
        # entry (providers.yaml task_classes) — never a hardcoded provider.
        routed = await self._routed_json(
            TASK_CLASS_COMPILE, prompt, _COMPILE_JSON_SCHEMA,
            temperature=0.3,
        )
        if routed is not None:
            parsed = routed
        else:
            # Legacy path: direct Ollama with a hardcoded model name.
            # Kept only as fallback for when providers.yaml is absent.
            try:
                from inference import OllamaInference, AgentConfig
                config = AgentConfig(
                    model="gemma4",
                    default_options={"temperature": 0.3, "num_predict": 2048},
                    think=False,
                    supports_native_tools=False,
                )
                llm = OllamaInference(config)
                response = await llm.achat(
                    messages=[{"role": "user", "content": prompt}],
                    format=_COMPILE_JSON_SCHEMA,
                )
                text = response.get("content", "") or response.get("message", {}).get("content", "")
            except Exception as e:
                logger.warning(f"Wiki LLM compile failed for '{topic}': {e}")
                # Fallback: template-based compilation (no LLM needed)
                return self._template_compile(topic, sources, existing_content)
            if not text:
                logger.warning(f"Wiki compile: empty LLM response for '{topic}'")
                return None
            from inference import _parse_json_response
            parsed = _parse_json_response(text)

        try:
            if not parsed or not isinstance(parsed, dict):
                logger.warning(f"Wiki compile: failed to parse JSON for '{topic}'")
                return None

            # Validate required fields
            if not parsed.get("title") or not parsed.get("content"):
                logger.warning(f"Wiki compile: missing required fields for '{topic}'")
                return None

            # Enforce max length
            if len(parsed["content"]) > MAX_ARTICLE_LENGTH * 2:
                parsed["content"] = parsed["content"][:MAX_ARTICLE_LENGTH]

            return parsed

        except Exception as e:
            logger.warning(f"Wiki LLM compile failed for '{topic}': {e}")
            # Fallback: template-based compilation (no LLM needed)
            return self._template_compile(topic, sources, existing_content)

    def _template_compile(
        self, topic: str, sources: list[dict], existing_content: Optional[str]
    ) -> dict:
        """Fallback compilation without LLM — structured template."""
        title = topic.replace("_", " ").title()

        sections = []
        if existing_content:
            sections.append(existing_content[:1500])
            sections.append("\n## New Findings\n")

        # Group by type
        sessions = [s for s in sources if s["type"] == "session"]
        evidence = [s for s in sources if s["type"] == "evidence"]
        learnings = [s for s in sources if s["type"] == "learning"]

        if sessions:
            sections.append("## Research Sessions\n")
            for s in sessions[:5]:
                sections.append(f"- **{s['query'][:80]}** (conf={s['confidence']:.2f}): {s['content'][:200]}\n")

        if evidence:
            sections.append("\n## Evidence\n")
            for s in evidence[:5]:
                sections.append(f"- [{s['domain']}] (conf={s['confidence']:.2f}): {s['content'][:200]}\n")

        if learnings:
            sections.append("\n## Learnings\n")
            for s in learnings[:5]:
                sections.append(f"- **{s['id']}**: {s['content'][:200]}\n")

        content = "".join(sections)[:MAX_ARTICLE_LENGTH]
        summary = f"{len(sources)} sources compiled for {title}"

        return {
            "title": title,
            "summary": summary,
            "content": content,
            "related_topics": [],
        }

    async def _update_cross_references(self, db: aiosqlite.Connection) -> None:
        """Scan all articles and update related_topics based on content overlap."""
        cursor = await db.execute(
            "SELECT topic, content, related_topics FROM wiki_articles"
        )
        articles = await cursor.fetchall()
        if len(articles) < 2:
            return

        # Build topic -> content index
        topic_index = {row[0]: row[1].lower() for row in articles}
        existing_refs = {row[0]: set(json.loads(row[2])) for row in articles}

        updates = []
        for topic, content in topic_index.items():
            new_refs = set()
            for other_topic in topic_index:
                if other_topic == topic:
                    continue
                # Check if other topic's slug appears in this article's content
                # or if they share a sport prefix
                slug_parts = other_topic.split("_")
                if other_topic in content:
                    new_refs.add(other_topic)
                elif len(slug_parts) >= 2:
                    # Same sport prefix = related
                    my_parts = topic.split("_")
                    if my_parts[0] == slug_parts[0] and my_parts[0] in (
                        "mlb", "nba", "nfl", "nhl", "ncaab", "soccer"
                    ):
                        new_refs.add(other_topic)

            merged = existing_refs.get(topic, set()) | new_refs
            if merged != existing_refs.get(topic, set()):
                updates.append((json.dumps(sorted(merged)), topic))

        for refs_json, topic in updates:
            await db.execute(
                "UPDATE wiki_articles SET related_topics = ? WHERE topic = ?",
                (refs_json, topic),
            )
        if updates:
            await db.commit()
            logger.debug(f"Wiki: updated cross-references for {len(updates)} articles")

    # ──────────────────────────────────────────────────
    # LINT: Detect contradictions, stale claims, gaps
    # ──────────────────────────────────────────────────

    async def lint(self, db: aiosqlite.Connection, cycle: int) -> dict:
        """
        Knowledge integrity check — find problems in the wiki.

        Returns stats dict with findings.
        """
        await self.initialize(db)
        start = time.monotonic()
        stats = {
            "contradictions_found": 0,
            "stale_articles": 0,
            "orphan_articles": 0,
        }

        # 1. Stale article detection
        stale_threshold = (
            datetime.now(timezone.utc) - timedelta(hours=STALE_THRESHOLD_HOURS)
        ).isoformat()
        cursor = await db.execute(
            "SELECT topic, updated_at FROM wiki_articles WHERE updated_at < ?",
            (stale_threshold,),
        )
        stale = await cursor.fetchall()
        stats["stale_articles"] = len(stale)
        if stale:
            topics = [r[0] for r in stale[:5]]
            logger.info(f"Wiki lint: {len(stale)} stale articles (>{STALE_THRESHOLD_HOURS}h): {topics}")

        # 2. Orphan detection (articles with no cross-references)
        cursor = await db.execute(
            "SELECT topic FROM wiki_articles WHERE related_topics = '[]'"
        )
        orphans = await cursor.fetchall()
        stats["orphan_articles"] = len(orphans)

        # 3. Contradiction detection via LLM
        contradictions = await self._detect_contradictions(db)
        stats["contradictions_found"] = len(contradictions)

        # 4. Log lint results
        duration = time.monotonic() - start
        stats["duration_seconds"] = round(duration, 1)
        await db.execute(
            "UPDATE wiki_compile_log SET contradictions_found = ?, stale_claims_flagged = ? "
            "WHERE id = (SELECT MAX(id) FROM wiki_compile_log)",
            (stats["contradictions_found"], stats["stale_articles"]),
        )
        await db.commit()

        if any(v > 0 for k, v in stats.items() if k != "duration_seconds"):
            logger.info(
                f"Wiki lint: {stats['contradictions_found']} contradictions, "
                f"{stats['stale_articles']} stale, {stats['orphan_articles']} orphans "
                f"({duration:.1f}s)"
            )

        return stats

    async def _detect_contradictions(self, db: aiosqlite.Connection) -> list[dict]:
        """Use local LLM to find contradicting claims between articles.

        Only compares articles within the same domain or same sport to keep
        the search space manageable.
        """
        from inference import OllamaInference, AgentConfig, _parse_json_response

        cursor = await db.execute(
            "SELECT topic, summary, content, domain FROM wiki_articles "
            "ORDER BY updated_at DESC LIMIT 30"
        )
        articles = await cursor.fetchall()
        if len(articles) < 2:
            return []

        # Build pairs to check: same domain or same sport prefix
        pairs = []
        for i, a in enumerate(articles):
            for b in articles[i + 1:]:
                a_domain, b_domain = a[3], b[3]
                a_sport = a[0].split("_")[0]
                b_sport = b[0].split("_")[0]
                if a_domain == b_domain or a_sport == b_sport:
                    pairs.append((a, b))

        if not pairs:
            return []

        # Batch pairs into a single LLM call (up to 10 pairs)
        pairs = pairs[:10]
        pair_texts = []
        for i, (a, b) in enumerate(pairs):
            pair_texts.append(
                f"PAIR {i + 1}:\n"
                f"  Article A [{a[0]}]: {a[1]}\n"
                f"  Article B [{b[0]}]: {b[1]}"
            )

        prompt = (
            "You are a knowledge integrity checker. Examine these article pairs "
            "for CONTRADICTIONS — claims in one article that directly conflict with "
            "claims in another.\n\n"
            + "\n\n".join(pair_texts) +
            "\n\nOutput JSON: {\"contradictions\": ["
            "{\"pair\": 1, \"article_a\": \"topic\", \"article_b\": \"topic\", "
            "\"claim_a\": \"what A says\", \"claim_b\": \"what B says\", "
            "\"severity\": \"low|medium|high\"}]}\n"
            "If no contradictions found, return {\"contradictions\": []}\n"
            "Only flag GENUINE contradictions, not merely different aspects of a topic."
        )

        # Primary path: ProviderRouter (task class ``knowledge_lint``).
        parsed = await self._routed_json(
            TASK_CLASS_LINT, prompt, _LINT_JSON_SCHEMA, temperature=0.0,
        )

        # Legacy fallback: direct Ollama, hardcoded fast classifier.
        if parsed is None:
            try:
                from inference import OllamaInference, AgentConfig
                config = AgentConfig(
                    model="qwen3.5:4b",  # Fast classifier — contradiction detection is classification
                    default_options={"temperature": 0.0, "num_predict": 1024},
                    think=False,
                )
                llm = OllamaInference(config)
                response = await llm.achat(
                    messages=[{"role": "user", "content": prompt}],
                    format=_LINT_JSON_SCHEMA,
                )
                text = response.get("content", "") or response.get("message", {}).get("content", "")
                if not text:
                    return []
                from inference import _parse_json_response
                parsed = _parse_json_response(text)
            except Exception as e:
                logger.warning(f"Wiki contradiction detection failed: {e}")
                return []

        if not parsed:
            return []

        found = parsed.get("contradictions", [])
        now = datetime.now(timezone.utc).isoformat()

        # Store new contradictions
        for c in found:
            if not c.get("article_a") or not c.get("claim_a"):
                continue
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO wiki_contradictions "
                    "(article_a, article_b, claim_a, claim_b, severity, detected_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (c["article_a"], c.get("article_b", ""),
                     c["claim_a"], c.get("claim_b", ""),
                     c.get("severity", "low"), now),
                )
            except Exception as e:
                logger.debug(f"Failed to store contradiction: {e}")
        await db.commit()
        return found

    # ──────────────────────────────────────────────────
    # WRITE: Direct schema-correct article upsert (no LLM compile)
    # ──────────────────────────────────────────────────

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

        Unlike ``_create_article``/``_update_article`` this does NOT invoke the
        LLM compile pipeline — it's for "we already know the lesson, file it"
        cases like LIVE-demotion post-mortems and backtest null results.

        Uses the REAL table schema (topic PK, title, content, summary,
        related_topics, source_sessions, source_entries, domain, confidence,
        created_at, updated_at, compile_count, content_hash) — the previous
        demotion writer tried ``(article_id, body)`` which failed every call.

        Returns ``{"action": "created"|"updated"|"failed", "topic": ..., "error": ...}``.
        On failure the error is logged loudly and the ``_wiki_writes_failed``
        module counter is incremented — never swallowed silently.
        """
        global _wiki_writes_failed, _wiki_writes_succeeded
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
            existing = await self._get_article(db, topic)
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
                _wiki_writes_succeeded += 1
                logger.info(
                    f"Wiki lesson: updated '{topic}' (domain={domain}, "
                    f"compile_count+=1)"
                )
                try:
                    await self._emit_article_embedding(
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
                _wiki_writes_succeeded += 1
                logger.info(
                    f"Wiki lesson: created '{topic}' (domain={domain}, "
                    f"{len(content)} chars)"
                )
                # Emit the embedding so the article is retrievable via
                # semantic search — without this the wiki-in-the-loop
                # retrieval path would never find lesson articles.
                try:
                    await self._emit_article_embedding(
                        topic,
                        {"title": title, "summary": summary, "content": content},
                        domain, round(float(confidence), 3),
                    )
                except Exception as e:
                    logger.debug(f"Wiki lesson embed deferred for '{topic}': {e}")
                return {"action": "created", "topic": topic}
        except Exception as e:
            _wiki_writes_failed += 1
            logger.error(
                f"Wiki lesson write FAILED for '{topic}': {type(e).__name__}: {e}. "
                f"writes_failed={_wiki_writes_failed}"
            )
            return {"action": "failed", "topic": topic, "error": str(e)}

    # ──────────────────────────────────────────────────
    # QUERY: Search wiki articles
    # ──────────────────────────────────────────────────

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

        # Opportunistic retry of embeddings deferred while Ollama was down —
        # without this, deferred articles stay invisible to semantic search.
        try:
            await flush_pending_embeds(max_items=10)
        except Exception as e:  # noqa: BLE001 — never block a search on the drain
            logger.debug(f"flush_pending_embeds failed during search: {e}")

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

    async def get_article(self, db: aiosqlite.Connection, topic: str) -> Optional[dict]:
        """Get a specific wiki article."""
        await self.initialize(db)
        return await self._get_article(db, topic)

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

    # ──────────────────────────────────────────────────
    # FILE: Auto-file query results back into wiki
    # ──────────────────────────────────────────────────

    async def file_task_result(
        self, db: aiosqlite.Connection, query: str, conclusion: str,
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
        await self.initialize(db)

        if not task_id:
            logger.warning(
                "Wiki.file_task_result: called without task_id — lineage will be "
                "incomplete. Caller should pass the real task_queue id."
            )
            source_id = f"task_anon_{int(time.time())}"
        else:
            source_id = session_id or f"task_{task_id}"

        source = {
            "type": "session",
            "id": source_id,
            "query": query,
            "domain": domain,
            "content": conclusion,
            "confidence": confidence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        topic = self._extract_topic(source)

        try:
            existing = await self._get_article(db, topic)
            if existing:
                await self._update_article(
                    db, topic, existing, [source], source_task_id=task_id,
                )
            else:
                await self._create_article(
                    db, topic, [source], source_task_id=task_id,
                )

            logger.info(f"Wiki: filed task result under '{topic}' (task={task_id})")
            return topic
        except Exception as e:
            logger.warning(f"Wiki: failed to file task result: {e}")
            return None


# ── Module-level singleton ──────────────────────────────

_wiki: Optional[KnowledgeWiki] = None


def get_wiki(db_path: Optional[str] = None) -> KnowledgeWiki:
    """Get or create the wiki singleton."""
    global _wiki
    if _wiki is None:
        import os
        path = db_path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        _wiki = KnowledgeWiki(path)
    return _wiki
