"""
Persistence helpers for the hypothesis generator: DB access, wiki
retrieval, rejection-example retrieval, and the sharpening-loop wiki
write-back.

Extracted from tools/hypothesis_generator.py as part of the hypgen split.

IMPORTANT — write-safety contract:
  * The ONLY writes issued here are (a) HypothesisManager.create_hypothesis
    draft creations driven by the generator, and (b) the sharpening-loop
    INSERT OR REPLACE into wiki_articles (documented behavior).
  * There are NO `signal_generated` or `edge_threshold` UPDATE statements
    anywhere in this module. If such UPDATEs ever need to exist they must
    be explicitly gated and diagnose-only; this module deliberately does
    not perform them.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_text

load_dotenv()

logger = logging.getLogger("callisto.hypgen.persistence")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ──────────────────────────────────────────────────────────────
# Temporal isolation metadata helper
# ──────────────────────────────────────────────────────────────

TRAINING_PERIOD_START = "2023-01-01"


def compute_temporal_metadata(
    training_cutoff_date: Optional[str] = None,
) -> dict:
    """Compute training/forward-test window metadata.

    Args:
        training_cutoff_date: ISO date string (YYYY-MM-DD) or None.
            Invalid strings fall back to 30 days before today, matching
            historical behavior.

    Returns dict with training_period_start/end and forward_test_start.
    """
    today = datetime.now(timezone.utc).date()
    if training_cutoff_date:
        try:
            cutoff = datetime.strptime(training_cutoff_date, "%Y-%m-%d").date()
        except ValueError:
            cutoff = today - timedelta(days=30)
    else:
        cutoff = today - timedelta(days=30)

    return {
        "training_period_start": TRAINING_PERIOD_START,
        "training_period_end": str(cutoff),
        "forward_test_start": str(cutoff + timedelta(days=1)),
    }


# ──────────────────────────────────────────────────────────────
# DB connection lifecycle
# ──────────────────────────────────────────────────────────────

class HypgenDB:
    """Owns the generator's aiosqlite connection."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        logger.info("Hypothesis generator initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    def ensure_ready(self) -> bool:
        return self._db is not None


# ──────────────────────────────────────────────────────────────
# Wiki / rejection retrieval (read-only)
# ──────────────────────────────────────────────────────────────

async def retrieve_wiki_context(
    db: HypgenDB, sport: str, focus_market: Optional[str],
    top_k: int,
) -> list[dict]:
    """Semantic-search the wiki for articles related to sport/market."""
    try:
        from tools.knowledge_wiki import KnowledgeWiki
    except Exception as e:
        logger.debug(f"wiki import failed: {e}")
        return []

    query_parts = [sport.replace("_", " ")]
    if focus_market:
        query_parts.append(focus_market.replace("_", " "))
    query_parts.append("betting edge hypothesis")
    query = " ".join(query_parts)

    try:
        kw = KnowledgeWiki(db.db_path)
        # kw.search needs an aiosqlite connection; reuse ours.
        if db._db is None:
            await db.initialize()
        hits = await kw.search(db._db, query, top_k=top_k)
        return hits or []
    except Exception as e:
        logger.debug(f"wiki semantic search failed (non-fatal): {e}")
        return []


async def retrieve_rejection_examples(
    db: HypgenDB, sport: Optional[str], focus_market: Optional[str],
    limit: int,
) -> list[dict]:
    """Pull a few recent rejected hypotheses in the same cohort."""
    if db._db is None:
        await db.initialize()
    sql_parts = ["SELECT name, thesis, notes FROM hypotheses WHERE status='rejected'"]
    params: list = []
    if sport:
        sql_parts.append("AND sport = ?")
        params.append(sport)
    if focus_market:
        sql_parts.append("AND market_type = ?")
        params.append(focus_market)
    sql_parts.append("ORDER BY updated_at DESC LIMIT ?")
    params.append(limit)
    try:
        cur = await db._db.execute(" ".join(sql_parts), params)
        rows = await cur.fetchall()
        return [
            {"name": r[0], "thesis": r[1] or "", "notes": r[2] or ""}
            for r in rows
        ]
    except Exception as e:
        logger.debug(f"rejection-example retrieval failed: {e}")
        return []


async def recent_theses(db: HypgenDB, sport: str, limit: int = 50) -> list[str]:
    if db._db is None:
        await db.initialize()
    try:
        cur = await db._db.execute(
            "SELECT thesis FROM hypotheses WHERE sport = ? ORDER BY created_at DESC LIMIT ?",
            (sport, limit),
        )
        return [r[0] or "" for r in await cur.fetchall()]
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────
# Sharpening loop: post-backtest wiki article write-back
# ──────────────────────────────────────────────────────────────

async def record_backtest_outcome_to_wiki(
    db: HypgenDB,
    hypothesis_manager,
    hypothesis_id: str,
    outcome: str,   # "success" | "failure" | "inconclusive"
    stats: Optional[dict] = None,
) -> bool:
    """Write a wiki article summarizing why a hypothesis did or didn't work.

    Called from hypothesis_mgr post-backtest hook. Next generation cycle
    will retrieve the article via semantic search, so the LLM can avoid
    re-proposing near-duplicates.

    This is the only direct SQL write in the hypgen persistence layer.
    No `signal_generated` or `edge_threshold` UPDATEs are performed here.

    Returns True on wiki write, False on any error (non-fatal).
    """
    try:
        from tools.knowledge_wiki import KnowledgeWiki
    except Exception as e:
        logger.debug(f"wiki import for sharpening failed: {e}")
        return False

    hyp = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not hyp:
        logger.debug(f"sharpening: hypothesis {hypothesis_id} not found")
        return False

    topic = f"backtest_outcome_{hypothesis_id}"
    title = f"Backtest outcome: {hyp['name']} ({outcome})"
    stats_blob = json.dumps(stats or {}, default=str)
    summary = (
        f"Outcome={outcome}. Market={hyp['market_type']}, sport={hyp['sport']}. "
        f"Edge threshold={hyp['edge_threshold']}. "
        f"Thesis: {(hyp.get('thesis') or '')[:240]}"
    )
    content = (
        f"Hypothesis: {hyp['name']}\n"
        f"Thesis: {hyp.get('thesis', '')}\n"
        f"Outcome: {outcome}\n"
        f"Stats: {stats_blob}\n"
        f"Model config: {json.dumps(hyp.get('model_config') or {})[:1500]}\n"
    )

    try:
        kw = KnowledgeWiki(db.db_path)
        if db._db is None:
            await db.initialize()
        await kw.initialize(db._db)
        # Upsert path: use a minimal insert-or-replace so we don't
        # require the LLM-compiler for sharpening signals.
        now_iso = datetime.now(timezone.utc).isoformat()
        await db._db.execute(
            "INSERT OR REPLACE INTO wiki_articles "
            "(topic, title, content, summary, related_topics, "
            "source_sessions, source_entries, domain, confidence, "
            "created_at, updated_at, compile_count, content_hash) "
            "VALUES (?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, 1, ?)",
            (
                topic, title, content, summary, "SIGNAL",
                0.8 if outcome == "success" else 0.5,
                now_iso, now_iso,
                f"hypgen:{hypothesis_id}:{outcome}",
            ),
        )
        await db._db.commit()
        # Embed and stash for retrieval.
        try:
            emb = await embed_text(summary)
            store = VectorStore(db.db_path)
            await store.initialize()
            try:
                await store.store(
                    "wiki_articles", summary, emb,
                    metadata={"topic": topic, "outcome": outcome,
                              "hypothesis_id": hypothesis_id},
                )
            finally:
                await store.close()
        except Exception as e:
            logger.debug(f"sharpening: embed/store failed: {e}")
        return True
    except Exception as e:
        logger.warning(f"sharpening wiki write failed: {e}")
        return False
