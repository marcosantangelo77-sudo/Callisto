"""Tests for the semantic wiki retrieval layer.

These tests bypass the real Gemma LLM compile and Ollama embed calls by
monkey-patching the relevant functions. What we're exercising is the
retrieval / near-dup / model-drift logic, not the embedding model itself.

The vector we use for "MLB home favorites drop 52% in day games" and the
natural-language query "afternoon favorites losing to visitors" are chosen
to have cosine similarity > 0.6 (they encode the same concept). The fake
embedder below produces deterministic vectors from an explicit mapping so
we don't need Ollama running to run these tests.
"""

from __future__ import annotations

import os
import math
import json
import random
import asyncio
from typing import Optional

import pytest
import pytest_asyncio
import aiosqlite

from tools import embeddings as emb_mod
from tools import knowledge_wiki as wiki_mod
from tools.knowledge_wiki import KnowledgeWiki, WIKI_COLLECTION
from tools.schema import ensure_schema


# ── Synthetic embedder ───────────────────────────────────────────
#
# The test fixture installs a deterministic ``embed_text`` that maps known
# phrases to hand-crafted 16-dim unit vectors. Two vectors are close iff
# they encode the same concept.

_CONCEPT_VECS: dict[str, list[float]] = {
    # "MLB home favorites drop 52% in day games" and
    # "afternoon favorites losing to visitors" share this direction.
    "home_fav_day_drop": [
        0.80, 0.15, 0.50, 0.10, 0.05, 0.00, 0.00, 0.00,
        0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    ],
    # An unrelated concept — to prove keyword LIKE would miss but semantic
    # might pick it up; we verify it's ranked below the target.
    "unrelated_weather": [
        0.00, 0.00, 0.00, 0.00, 0.00, 0.80, 0.30, 0.50,
        0.10, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00,
    ],
    "generic": [0.05] * 16,
}

# Keywords that route to each concept vector.
_KEYWORD_MAP: list[tuple[str, str]] = [
    ("home favorite", "home_fav_day_drop"),
    ("home favorites", "home_fav_day_drop"),
    ("home favs", "home_fav_day_drop"),
    ("home mlb favs", "home_fav_day_drop"),
    ("afternoon favorite", "home_fav_day_drop"),
    ("afternoon favorites", "home_fav_day_drop"),
    ("afternoon favorites losing", "home_fav_day_drop"),
    ("day game", "home_fav_day_drop"),
    ("daytime", "home_fav_day_drop"),
    ("weather", "unrelated_weather"),
    ("rain", "unrelated_weather"),
]


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n < 1e-9:
        return v
    return [x / n for x in v]


def _fake_embed_sync(text: str) -> list[float]:
    t = (text or "").lower()
    # Aggregate all matching concepts by summing vectors.
    acc = [0.0] * 16
    hits = 0
    for kw, concept in _KEYWORD_MAP:
        if kw in t:
            v = _CONCEPT_VECS[concept]
            acc = [a + b for a, b in zip(acc, v)]
            hits += 1
    if hits == 0:
        # Deterministic but unique per text so unrelated texts are unrelated.
        rng = random.Random(hash(t) & 0xFFFF)
        acc = [rng.random() * 0.01 for _ in range(16)]
        # Add tiny generic component so they're not orthogonal.
        acc = [a + b for a, b in zip(acc, _CONCEPT_VECS["generic"])]
    # nomic-embed-text is 768-dim; pad with zeros to match what the
    # VectorStore expects (EMBED_DIM=768).
    full = acc + [0.0] * (emb_mod.EMBED_DIM - len(acc))
    return _normalize(full)


async def _fake_embed(text: str) -> list[float]:
    return _fake_embed_sync(text)


async def _fake_embed_fails(text: str) -> list[float]:
    raise RuntimeError("Ollama down (simulated)")


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "wiki_test.db")


@pytest_asyncio.fixture
async def prepared_db(db_path, monkeypatch):
    # Ensure schema with the new model_name + source_task_id columns.
    await ensure_schema(db_path)
    # The wiki_articles table is created by KnowledgeWiki.initialize, not
    # ensure_schema. Run it once before _safe_add_column re-runs so the
    # source_task_id column gets added cleanly.
    wiki = KnowledgeWiki(db_path)
    async with aiosqlite.connect(db_path) as db:
        await wiki.initialize(db)
    await ensure_schema(db_path)
    # Install fake embedder.
    monkeypatch.setattr(emb_mod, "embed_text", _fake_embed)
    # Stabilise the model name the VectorStore stamps.
    monkeypatch.setattr(emb_mod, "EMBED_MODEL", "nomic-embed-text:latest")
    yield db_path


async def _insert_article_direct(
    db_path: str, topic: str, title: str, summary: str, content: str,
    domain: str = "SIGNAL", confidence: float = 0.7,
    source_task_id: Optional[str] = None,
) -> None:
    """Bypass the LLM compile path and insert a row + emit the embedding,
    the way _create_article would once compile returns successfully.
    """
    from datetime import datetime, timezone
    import hashlib
    now = datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.md5(content.encode()).hexdigest()[:12]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO wiki_articles (topic, title, content, summary, related_topics, "
            "source_sessions, source_entries, domain, confidence, created_at, updated_at, "
            "compile_count, content_hash, source_task_id) "
            "VALUES (?, ?, ?, ?, '[]', '[]', '[]', ?, ?, ?, ?, 1, ?, ?)",
            (topic, title, content, summary, domain, confidence, now, now,
             content_hash, source_task_id),
        )
        await db.commit()

    # Emit embedding using the real wiki helper.
    wiki = KnowledgeWiki(db_path)
    await wiki._emit_article_embedding(
        topic,
        {"title": title, "summary": summary, "content": content},
        domain, confidence, source_task_id=source_task_id,
    )


# ── Tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semantic_search_finds_synonym(prepared_db):
    """Write an article, query with a synonymous phrase — semantic path
    must surface it with similarity > 0.6. Keyword LIKE would miss.
    """
    db_path = prepared_db
    topic = "mlb_home_fav_day_drop"
    title = "MLB home favorites drop 52% in day games"
    summary = "Day-game MLB home favorites underperform."
    content = (
        "MLB home favorites drop 52% in day games compared to night games. "
        "The edge is statistically significant across the 2024 season."
    )
    await _insert_article_direct(
        db_path, topic, title, summary, content,
        domain="SIGNAL", source_task_id="42",
    )

    # 1. Semantic search with a phrase that shares zero keywords.
    wiki = KnowledgeWiki(db_path)
    async with aiosqlite.connect(db_path) as db:
        results = await wiki.search(
            db, "afternoon favorites losing to visitors", top_k=5,
        )
    assert results, "semantic search returned nothing"
    assert results[0]["topic"] == topic, f"expected {topic}, got {results[0]['topic']}"
    assert results[0]["similarity"] is not None
    assert results[0]["similarity"] > 0.6, (
        f"similarity {results[0]['similarity']} should exceed 0.6 for near-synonym query"
    )

    # 2. Prove keyword LIKE would return nothing for this query — the
    #    article has no 'afternoon', 'visitors', or 'losing' tokens.
    async with aiosqlite.connect(db_path) as db:
        q = "afternoon"
        cursor = await db.execute(
            "SELECT topic FROM wiki_articles "
            "WHERE content LIKE ? OR title LIKE ? OR topic LIKE ?",
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        like_rows = await cursor.fetchall()
    assert like_rows == [], (
        f"keyword LIKE unexpectedly found rows for 'afternoon': {like_rows}"
    )


@pytest.mark.asyncio
async def test_domain_filter_on_semantic_search(prepared_db):
    """Writing an article under FINANCIAL should be excludable when we
    filter to SIGNAL, even if the semantic vector is a hit.
    """
    db_path = prepared_db
    # SIGNAL article — will be near-dup merged into itself if we wrote it
    # twice, so we only write it once and differentiate by DOMAIN filter.
    await _insert_article_direct(
        db_path, "mlb_home_fav_signal", "Home fav signal", "summary",
        "MLB home favorites drop 52% in day games — the signal.",
        domain="SIGNAL",
    )
    # Unrelated concept article under FINANCIAL — won't merge with above.
    await _insert_article_direct(
        db_path, "wx_rain_note", "Weather note", "rain summary",
        "Weather analysis: heavy rain affects baseball totals.",
        domain="FINANCIAL",
    )

    wiki = KnowledgeWiki(db_path)
    async with aiosqlite.connect(db_path) as db:
        signal_results = await wiki.search(
            db, "afternoon favorites day game", top_k=5, domain="SIGNAL",
        )
        fin_results = await wiki.search(
            db, "rain weather", top_k=5, domain="FINANCIAL",
        )
    assert signal_results
    assert all(r["domain"] == "SIGNAL" for r in signal_results)
    assert fin_results
    assert all(r["domain"] == "FINANCIAL" for r in fin_results)


@pytest.mark.asyncio
async def test_near_duplicate_merges_not_inserts(prepared_db):
    """Writing a semantically-identical article after the first should
    MERGE into the existing vector row instead of inserting a new one.
    """
    db_path = prepared_db
    await _insert_article_direct(
        db_path, "mlb_home_fav_one", "First",
        "Day-game MLB home favs drop.",
        "MLB home favorites drop 52% in day games (sample 1).",
        domain="SIGNAL",
    )

    # Count vectors in the wiki collection after write 1.
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM embeddings WHERE collection = ?",
            (WIKI_COLLECTION,),
        )
        n_before = (await cursor.fetchone())[0]
    assert n_before == 1

    # Second article — different topic slug, paraphrased text that maps to
    # the same concept vector. Should be a near-duplicate (sim ~ 1.0).
    await _insert_article_direct(
        db_path, "mlb_home_fav_two", "Second",
        "Paraphrased day-game home fav finding.",
        "Home MLB favs drop 52% during daytime, identical finding, sample 2.",
        domain="SIGNAL",
    )

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM embeddings WHERE collection = ?",
            (WIKI_COLLECTION,),
        )
        n_after = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT json_extract(metadata_json, '$.merge_count') "
            "FROM embeddings WHERE collection = ?",
            (WIKI_COLLECTION,),
        )
        merge_counts = [r[0] for r in await cursor.fetchall()]
    assert n_after == 1, (
        f"expected near-duplicate to merge, got {n_after} rows in collection"
    )
    assert any(mc and mc >= 2 for mc in merge_counts), (
        f"merge_count should have incremented to >=2, got {merge_counts}"
    )


@pytest.mark.asyncio
async def test_model_drift_filter_logs_and_excludes(prepared_db, caplog):
    """A row stamped with a different embed model must be excluded from
    the current-model query results and the exclusion must be logged.
    """
    import logging
    db_path = prepared_db

    # Write the primary article on the current model.
    await _insert_article_direct(
        db_path, "mlb_day_fav_current", "Current-model article",
        "Summary current.",
        "MLB home favorites drop 52% in day games, embedded on current model.",
        domain="SIGNAL",
    )

    # Inject a fake row stamped with an older model — same concept vector,
    # so it would match the query if drift filtering didn't work.
    stale_vec = _fake_embed_sync("MLB home favorites day games stale")
    async with aiosqlite.connect(db_path) as db:
        import numpy as np
        await db.execute(
            "INSERT INTO embeddings (collection, content_hash, content_text, "
            "embedding_json, embedding_blob, metadata_json, model_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                WIKI_COLLECTION,
                "stalehash1234",
                "Stale article from an old model.",
                json.dumps(stale_vec),
                np.array(stale_vec, dtype=np.float32).tobytes(),
                json.dumps({"topic": "stale_fake_topic"}),
                "old-model-v1",
            ),
        )
        await db.commit()

    # Query and confirm the stale row is NOT returned + drift log was written.
    wiki = KnowledgeWiki(db_path)
    caplog.set_level(logging.INFO, logger="callisto.embeddings")
    async with aiosqlite.connect(db_path) as db:
        results = await wiki.search(db, "afternoon favorites day game", top_k=5)

    topics_returned = [r["topic"] for r in results]
    assert "stale_fake_topic" not in topics_returned, (
        f"stale-model row leaked into results: {topics_returned}"
    )
    # Drift log should have fired.
    drift_logged = any(
        "excluded" in rec.message and "embed models" in rec.message
        for rec in caplog.records
    )
    assert drift_logged, (
        "expected a drift-exclusion log line from vector_store.search"
    )


@pytest.mark.asyncio
async def test_like_fallback_when_ollama_down(prepared_db, monkeypatch):
    """If embed_text raises (Ollama down), search must fall back to LIKE
    and still return matching articles on a keyword overlap.
    """
    db_path = prepared_db
    await _insert_article_direct(
        db_path, "mlb_home_fav_fb", "Home fav fallback",
        "Summary.",
        "MLB home favorites drop 52% in day games - full keyword content.",
        domain="SIGNAL",
    )

    # Break embed_text for the query path.
    monkeypatch.setattr(emb_mod, "embed_text", _fake_embed_fails)

    wiki = KnowledgeWiki(db_path)
    async with aiosqlite.connect(db_path) as db:
        # Use a keyword present in the content so LIKE works.
        results = await wiki.search(db, "home favorites", top_k=5)
    assert results
    assert any(r["topic"] == "mlb_home_fav_fb" for r in results)
    # Fallback results carry similarity=None.
    assert results[0]["similarity"] is None


@pytest.mark.asyncio
async def test_file_task_result_preserves_task_id(prepared_db, monkeypatch):
    """file_task_result must write source_task_id into wiki_articles, not
    mint a ``task_{int(time.time())}`` fake id.
    """
    db_path = prepared_db

    # Patch the LLM compile to skip Gemma and return a deterministic shape.
    async def _fake_compile(self, topic, sources, existing_content):
        return {
            "title": f"Article: {topic}",
            "summary": "fake summary",
            "content": "MLB home favorites drop 52% in day games (from task).",
            "related_topics": [],
        }
    monkeypatch.setattr(KnowledgeWiki, "_llm_compile", _fake_compile)

    wiki = KnowledgeWiki(db_path)
    async with aiosqlite.connect(db_path) as db:
        topic = await wiki.file_task_result(
            db,
            query="mlb home fav day-game edge?",
            conclusion="Home favorites in day games drop 52%.",
            confidence=0.8,
            domain="SIGNAL",
            task_id="4242",
            session_id="sess_abc",
        )
    assert topic is not None

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT source_task_id FROM wiki_articles WHERE topic = ?",
            (topic,),
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "4242", f"expected source_task_id='4242', got {row[0]!r}"
