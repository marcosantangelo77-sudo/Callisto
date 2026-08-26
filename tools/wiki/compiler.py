"""Knowledge wiki — compilation/lint layer.

Reads recent sessions/evidence/learnings and synthesizes them into
persistent wiki articles via a local LLM (Gemma 4 by default, with a
template fallback). Persistence, search and stats live in
``tools.wiki.store.WikiStore``; this module composes on top of it.

No silent evidence rewrites happen here: this module never updates
historical evidence flags or confidence thresholds — it only writes
wiki tables.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite

from tools.wiki.store import WikiStore

logger = logging.getLogger("callisto.wiki.compiler")

# Compilation intervals
COMPILE_INTERVAL_CYCLES = 7     # Compile every 7 research cycles (~35 min)
LINT_INTERVAL_CYCLES = 11       # Lint every 11 cycles (~55 min, coprime with compile)
MAX_SOURCES_PER_COMPILE = 30    # Don't overwhelm the local model
MAX_ARTICLE_LENGTH = 4000       # Characters — keep articles focused


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


class WikiCompiler:
    """LLM-compiled knowledge synthesis on top of WikiStore."""

    def __init__(self, store: WikiStore):
        self.store = store
        # Optional override installed by the KnowledgeWiki façade so callers
        # can monkeypatch KnowledgeWiki._llm_compile and affect compilation.
        self._external_llm_compile = None

    async def run_llm_compile(self, topic, sources, existing_content):
        """Dispatch through the external hook when one is installed."""
        if self._external_llm_compile is not None:
            return await self._external_llm_compile(topic, sources, existing_content)
        return await self._llm_compile(topic, sources, existing_content)

    @property
    def db_path(self) -> str:
        return self.store.db_path

    # ──────────────────────────────────────────────────
    # COMPILE: Synthesize raw research into wiki articles
    # ──────────────────────────────────────────────────

    async def compile(self, db: aiosqlite.Connection, cycle: int) -> dict:
        """
        Incremental compilation: read recent evidence/sessions, update wiki.

        Returns stats dict: {articles_created, articles_updated, duration_seconds}
        """
        await self.store.initialize(db)
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
                existing = await self.store.get_article(db, topic)
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

    async def _create_article(
        self, db: aiosqlite.Connection, topic: str, sources: list[dict],
        source_task_id: Optional[str] = None,
    ) -> None:
        """Create a new wiki article from sources using local LLM."""
        compiled = await self.run_llm_compile(topic, sources, existing_content=None)
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
        cols, vals, placeholders = _insert_columns_for(source_task_id)
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
        await self.store.emit_article_embedding(
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

    async def _update_article(
        self, db: aiosqlite.Connection, topic: str, existing: dict,
        new_sources: list[dict], source_task_id: Optional[str] = None,
    ) -> None:
        """Update an existing wiki article with new sources."""
        compiled = await self.run_llm_compile(topic, new_sources, existing_content=existing["content"])
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
        await self.store.emit_article_embedding(
            topic, compiled, existing.get("domain", "GENERAL"), round(merged_conf, 3),
            source_task_id=source_task_id,
        )

    async def _llm_compile(
        self, topic: str, sources: list[dict], existing_content: Optional[str]
    ) -> Optional[dict]:
        """Use local LLM (Gemma 4) to compile sources into a wiki article.

        Returns {"title", "content", "summary", "related_topics"} or None on failure.
        """
        from inference import OllamaInference, AgentConfig

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
            f"You are a knowledge compiler for an autonomous sports betting research system.\n\n"
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

        try:
            config = AgentConfig(
                model="gemma4",
                default_options={"temperature": 0.3, "num_predict": 2048},
                think=False,
                supports_native_tools=False,
            )
            llm = OllamaInference(config)
            response = await llm.achat(
                messages=[{"role": "user", "content": prompt}],
                format={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "content": {"type": "string"},
                        "related_topics": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["title", "summary", "content"],
                },
            )

            text = response.get("content", "") or response.get("message", {}).get("content", "")
            if not text:
                logger.warning(f"Wiki compile: empty LLM response for '{topic}'")
                return None

            # Parse JSON from response
            from inference import _parse_json_response
            parsed = _parse_json_response(text)
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
        from tools.wiki.store import STALE_THRESHOLD_HOURS

        await self.store.initialize(db)
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

        try:
            config = AgentConfig(
                model="qwen3.5:4b",  # Fast classifier — contradiction detection is classification
                default_options={"temperature": 0.0, "num_predict": 1024},
                think=False,
            )
            llm = OllamaInference(config)
            response = await llm.achat(
                messages=[{"role": "user", "content": prompt}],
                format={
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
                },
            )

            text = response.get("content", "") or response.get("message", {}).get("content", "")
            parsed = _parse_json_response(text)
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

        except Exception as e:
            logger.warning(f"Wiki contradiction detection failed: {e}")
            return []


def _insert_columns_for(_task_id) -> tuple:
    """Stub — actual column introspection happens lazily inside the write.

    Historically this used a PRAGMA probe; that's unnecessary here because
    ``_safe_add_column`` runs at startup and always adds ``source_task_id``.
    We keep the hook so the caller remains explicit and a future schema
    change can gate behaviour here without touching call sites.
    """
    return ({"source_task_id"}, None, None)
