"""
Semantic embedding engine — nomic-embed-text via Ollama + SQLite vector store.

This is the pattern-discovery backbone of the autonomous research loop.
Embeddings let Callisto:
  1. Represent game contexts, prop lines, and historical outcomes as vectors
  2. Find similar situations via cosine similarity (no external vector DB needed)
  3. Cluster contexts to discover recurring mispricing patterns
  4. Generate hypotheses from statistical anomalies in clusters

Vector storage: SQLite with numpy binary blobs (3KB/vector) + JSON fallback.
Similarity search: numpy vectorized batch cosine (50-100x faster than pure Python).

Model: nomic-embed-text (137M params, 768-dim) via Ollama REST API.
Throughput: ~200 embeddings/sec on CPU, batched.
"""

import hashlib
import json
import logging
import math
import os
from typing import Optional

import aiosqlite
import httpx
import numpy as np
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.embeddings")

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
EMBED_DIM = 768  # nomic-embed-text output dimension

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _content_hash(text: str) -> str:
    """SHA-256 hash of content for dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Pure Python fallback for single pairs."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)


def _to_blob(embedding: list[float]) -> bytes:
    """Serialize embedding list to compact binary blob (768 * 4 = 3072 bytes)."""
    return np.array(embedding, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    """Deserialize binary blob back to numpy array."""
    expected_size = EMBED_DIM * 4  # 768 * 4 = 3072 bytes
    if len(blob) != expected_size:
        raise ValueError(
            f"Corrupted embedding blob: {len(blob)} bytes, expected {expected_size}"
        )
    return np.frombuffer(blob, dtype=np.float32).copy()


def _deserialize_embedding(row_blob, row_json) -> np.ndarray:
    """Prefer binary blob, fall back to JSON for pre-migration rows."""
    if row_blob is not None:
        return _from_blob(row_blob)
    return np.array(json.loads(row_json), dtype=np.float32)


async def embed_text(text: str) -> list[float]:
    """Get embedding vector for a single text string via Ollama."""
    client = _get_client()
    resp = await client.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    embeddings = data.get("embeddings", [])
    if not embeddings:
        raise ValueError(f"No embedding returned for text: {text[:50]}...")
    return embeddings[0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed multiple texts in batches."""
    all_embeddings = []
    client = _get_client()
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = await client.post(
            f"{OLLAMA_BASE}/api/embed",
            json={"model": EMBED_MODEL, "input": batch},
        )
        resp.raise_for_status()
        data = resp.json()
        all_embeddings.extend(data.get("embeddings", []))
    return all_embeddings


class VectorStore:
    """SQLite-backed vector store with cosine similarity search."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        logger.info("Vector store initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def store(
        self,
        collection: str,
        text: str,
        embedding: list[float],
        metadata: Optional[dict] = None,
    ) -> int:
        """Store a text + embedding. Returns row ID. Deduplicates by content hash."""
        from tools.db_utils import execute_with_retry, commit_with_retry
        content_hash = _content_hash(text)
        cursor = await execute_with_retry(
            self._db,
            "INSERT OR IGNORE INTO embeddings "
            "(collection, content_hash, content_text, embedding_json, embedding_blob, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                collection,
                content_hash,
                text,
                json.dumps(embedding),
                _to_blob(embedding),
                json.dumps(metadata) if metadata else None,
            ),
            operation="vector_store store",
        )
        await commit_with_retry(self._db, operation="vector_store store")
        return cursor.lastrowid

    async def store_batch(
        self,
        collection: str,
        items: list[tuple[str, list[float], Optional[dict]]],
    ) -> int:
        """Store multiple (text, embedding, metadata) tuples. Returns count stored.

        Uses the global write lock and execute_with_retry so concurrent
        embedders don't crash on SQLite WAL contention.
        """
        from tools.db_utils import get_write_lock, execute_with_retry, commit_with_retry
        write_lock = get_write_lock()
        count = 0
        async with write_lock:
            for text, embedding, metadata in items:
                content_hash = _content_hash(text)
                try:
                    cursor = await execute_with_retry(
                        self._db,
                        "INSERT OR IGNORE INTO embeddings "
                        "(collection, content_hash, content_text, embedding_json, embedding_blob, metadata_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            collection,
                            content_hash,
                            text,
                            json.dumps(embedding),
                            _to_blob(embedding),
                            json.dumps(metadata) if metadata else None,
                        ),
                        operation="vector_store store_batch insert",
                    )
                    if cursor.rowcount > 0:
                        count += 1
                except Exception as e:
                    logger.warning(f"store_batch: failed insert for hash {content_hash[:12]}: {e}")
            await commit_with_retry(self._db, operation="vector_store store_batch commit")
        return count

    async def search(
        self,
        collection: str,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """
        Find the top_k most similar items in a collection.

        Uses numpy vectorized cosine similarity for batch computation.
        Loads all embeddings once, computes all similarities in a single matrix op.
        """
        cursor = await self._db.execute(
            "SELECT id, content_text, embedding_blob, embedding_json, metadata_json "
            "FROM embeddings WHERE collection = ?",
            (collection,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        # Deserialize all embeddings into a matrix
        ids = []
        texts = []
        meta_jsons = []
        emb_list = []
        for row_id, text, emb_blob, emb_json, meta_json in rows:
            ids.append(row_id)
            texts.append(text)
            meta_jsons.append(meta_json)
            emb_list.append(_deserialize_embedding(emb_blob, emb_json))

        # Vectorized batch cosine similarity
        matrix = np.vstack(emb_list)  # (N, 768)
        query_vec = np.array(query_embedding, dtype=np.float32)  # (768,)
        norms = np.linalg.norm(matrix, axis=1)
        query_norm = np.linalg.norm(query_vec)
        # Avoid division by zero
        denom = norms * query_norm
        denom[denom < 1e-9] = 1e-9
        sims = (matrix @ query_vec) / denom  # (N,)

        # Filter and sort
        mask = sims >= min_similarity
        valid_indices = np.where(mask)[0]
        if len(valid_indices) == 0:
            del matrix, query_vec, norms, denom, sims, emb_list
            return []

        valid_sims = sims[valid_indices]
        top_count = min(top_k, len(valid_indices))
        top_local = np.argpartition(valid_sims, -top_count)[-top_count:]
        top_local = top_local[np.argsort(valid_sims[top_local])[::-1]]
        top_indices = valid_indices[top_local]

        results = [
            {
                "id": ids[i],
                "text": texts[i],
                "similarity": round(float(sims[i]), 6),
                "metadata": json.loads(meta_jsons[i]) if meta_jsons[i] else None,
            }
            for i in top_indices
        ]

        # Explicitly free large numpy arrays — they hold ~20MB for 6K+ embeddings
        del matrix, query_vec, norms, denom, sims, emb_list, valid_sims
        return results

    async def search_text(
        self,
        collection: str,
        query_text: str,
        top_k: int = 10,
        min_similarity: float = 0.0,
    ) -> list[dict]:
        """Search by text — embeds the query first, then searches."""
        query_emb = await embed_text(query_text)
        return await self.search(collection, query_emb, top_k, min_similarity)

    async def get_all(self, collection: str) -> list[dict]:
        """Get all items in a collection (without embeddings for memory efficiency)."""
        cursor = await self._db.execute(
            "SELECT id, content_text, metadata_json FROM embeddings WHERE collection = ?",
            (collection,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row_id,
                "text": text,
                "metadata": json.loads(meta_json) if meta_json else None,
            }
            for row_id, text, meta_json in rows
        ]

    async def get_collection_stats(self, collection: Optional[str] = None) -> dict:
        """Get stats about stored embeddings."""
        if collection:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM embeddings WHERE collection = ?",
                (collection,),
            )
            count = (await cursor.fetchone())[0]
            return {"collection": collection, "count": count}

        cursor = await self._db.execute(
            "SELECT collection, COUNT(*) as count "
            "FROM embeddings GROUP BY collection ORDER BY count DESC"
        )
        rows = await cursor.fetchall()
        return {
            "collections": {col: cnt for col, cnt in rows},
            "total": sum(cnt for _, cnt in rows),
        }

    async def get_embeddings_by_period(
        self, collection: str, period: str
    ) -> list[dict]:
        """
        Get all items in a collection filtered by data_period metadata.

        Args:
            collection: embedding collection name (e.g., 'game_contexts')
            period: 'historical', 'recent', or 'all'

        Returns list of dicts with id, text, metadata (no embedding vectors).
        """
        if period == "all":
            return await self.get_all(collection)

        cursor = await self._db.execute(
            "SELECT id, content_text, metadata_json "
            "FROM embeddings "
            "WHERE collection = ? "
            "AND json_extract(metadata_json, '$.data_period') = ?",
            (collection, period),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row_id,
                "text": text,
                "metadata": json.loads(meta_json) if meta_json else None,
            }
            for row_id, text, meta_json in rows
        ]

    async def get_embedding_coverage(self) -> dict:
        """
        Return embedding coverage stats: date ranges, counts per collection,
        and breakdown by data_period. Used by integrity checker to verify
        that historical embedding is complete.
        """
        result = {}

        # Per-collection stats
        cursor = await self._db.execute(
            "SELECT collection, COUNT(*) as count, "
            "MIN(json_extract(metadata_json, '$.game_date')) as min_date, "
            "MAX(json_extract(metadata_json, '$.game_date')) as max_date "
            "FROM embeddings GROUP BY collection ORDER BY count DESC"
        )
        rows = await cursor.fetchall()
        collections = {}
        for col, cnt, min_d, max_d in rows:
            collections[col] = {
                "count": cnt,
                "date_range": {"min": min_d, "max": max_d},
            }
        result["collections"] = collections
        result["total"] = sum(c["count"] for c in collections.values())

        # Per-period breakdown for game_contexts
        cursor = await self._db.execute(
            "SELECT "
            "json_extract(metadata_json, '$.data_period') as period, "
            "COUNT(*) as count, "
            "MIN(json_extract(metadata_json, '$.game_date')) as min_date, "
            "MAX(json_extract(metadata_json, '$.game_date')) as max_date "
            "FROM embeddings WHERE collection = 'game_contexts' "
            "GROUP BY period"
        )
        rows = await cursor.fetchall()
        result["game_contexts_by_period"] = {
            (period or "untagged"): {
                "count": cnt,
                "date_range": {"min": min_d, "max": max_d},
            }
            for period, cnt, min_d, max_d in rows
        }

        # Per-sport breakdown for game_contexts
        cursor = await self._db.execute(
            "SELECT "
            "json_extract(metadata_json, '$.sport') as sport, "
            "COUNT(*) as count "
            "FROM embeddings WHERE collection = 'game_contexts' "
            "GROUP BY sport ORDER BY count DESC"
        )
        rows = await cursor.fetchall()
        result["game_contexts_by_sport"] = {
            sport: cnt for sport, cnt in rows
        }

        return result

    async def delete_collection(self, collection: str) -> int:
        """Delete all embeddings in a collection. Returns count deleted."""
        cursor = await self._db.execute(
            "DELETE FROM embeddings WHERE collection = ?",
            (collection,),
        )
        await self._db.commit()
        return cursor.rowcount

    async def cluster_by_similarity(
        self,
        collection: str,
        threshold: float = 0.85,
        data_period: Optional[str] = None,
    ) -> list[list[dict]]:
        """
        Single-linkage clustering by cosine similarity.
        Groups items where similarity >= threshold.

        Uses precomputed similarity matrix for O(N^2) pairwise computation
        instead of per-pair Python loops.

        Args:
            collection: embedding collection to cluster
            threshold: minimum cosine similarity to join a cluster
            data_period: optional filter — 'historical', 'recent', or None for all

        Returns list of clusters, each cluster is a list of items.
        Good enough for finding recurring game context patterns.
        """
        if data_period:
            cursor = await self._db.execute(
                "SELECT id, content_text, embedding_blob, embedding_json, metadata_json "
                "FROM embeddings WHERE collection = ? "
                "AND json_extract(metadata_json, '$.data_period') = ?",
                (collection, data_period),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, content_text, embedding_blob, embedding_json, metadata_json "
                "FROM embeddings WHERE collection = ?",
                (collection,),
            )
        rows = await cursor.fetchall()

        if not rows:
            return []

        # Build items and embedding matrix
        items = []
        emb_list = []
        for row_id, text, emb_blob, emb_json, meta_json in rows:
            items.append({
                "id": row_id,
                "text": text,
                "metadata": json.loads(meta_json) if meta_json else None,
            })
            emb_list.append(_deserialize_embedding(emb_blob, emb_json))

        # Precompute full cosine similarity matrix
        matrix = np.vstack(emb_list)  # (N, 768)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms < 1e-9] = 1e-9
        normalized = matrix / norms
        sim_matrix = normalized @ normalized.T  # (N, N) pairwise cosine

        # Single-linkage clustering using precomputed matrix
        n = len(items)
        assigned = [False] * n
        clusters = []

        for i in range(n):
            if assigned[i]:
                continue
            cluster_indices = [i]
            assigned[i] = True

            for j in range(i + 1, n):
                if assigned[j]:
                    continue
                # Check if j is similar to ANY member of the cluster
                for member_idx in cluster_indices:
                    if sim_matrix[member_idx, j] >= threshold:
                        cluster_indices.append(j)
                        assigned[j] = True
                        break

            clusters.append(cluster_indices)

        # Explicitly free the large matrices before building results
        del matrix, norms, normalized, sim_matrix, emb_list

        # Sort by cluster size descending, return items without embeddings
        clusters.sort(key=len, reverse=True)
        return [
            [items[i] for i in cluster_indices]
            for cluster_indices in clusters
            if len(cluster_indices) >= 2
        ]


# ── Convenience functions for the autonomous loop ──

async def embed_game_context(
    store: VectorStore,
    sport: str,
    game_date: str,
    home_team: str,
    away_team: str,
    context: dict,
) -> int:
    """
    Embed a game context into the 'game_contexts' collection.

    The context dict should contain structured info like:
      - injuries, rest days, travel, pace, defensive rating, etc.
      - prop lines and devigged fair values
      - final scores and key player stats

    We serialize it into a natural language description for embedding.
    """
    # Build a textual representation for embedding
    parts = [
        f"{sport} game on {game_date}: {away_team} at {home_team}",
    ]
    if context.get("home_score") is not None:
        parts.append(
            f"Final: {home_team} {context['home_score']}, "
            f"{away_team} {context['away_score']}"
        )
    if context.get("total"):
        parts.append(f"Total: {context['total']}")
    if context.get("spread"):
        parts.append(f"Spread: {home_team} {context['spread']}")
    if context.get("injuries"):
        parts.append(f"Injuries: {', '.join(context['injuries'][:5])}")
    if context.get("rest_days_home") is not None:
        parts.append(
            f"Rest: {home_team} {context['rest_days_home']}d, "
            f"{away_team} {context.get('rest_days_away', '?')}d"
        )
    if context.get("key_props"):
        for prop in context["key_props"][:5]:
            parts.append(
                f"Prop: {prop['player']} {prop['market']} "
                f"{prop.get('line', '?')} (fair={prop.get('fair_prob', '?')})"
            )

    text = " | ".join(parts)
    embedding = await embed_text(text)

    # Classify as historical or recent based on date
    data_period = "recent" if game_date >= "2026-02-23" else "historical"

    metadata = {
        "sport": sport,
        "game_date": game_date,
        "home_team": home_team,
        "away_team": away_team,
        "data_period": data_period,
        **{k: v for k, v in context.items() if k not in ("key_props", "injuries")},
    }

    return await store.store("game_contexts", text, embedding, metadata)


async def embed_prop_outcome(
    store: VectorStore,
    sport: str,
    game_date: str,
    player: str,
    market: str,
    line: float,
    fair_prob_over: float,
    book_implied_over: float,
    actual_stat: Optional[float] = None,
    context: Optional[dict] = None,
) -> int:
    """
    Embed a prop outcome for pattern discovery.

    Encodes: player, market type, line value, edge size, and whether
    the prop hit. This lets us find clusters of similar prop situations.
    """
    edge = fair_prob_over - book_implied_over
    hit = actual_stat > line if actual_stat is not None and line else None

    parts = [
        f"{sport} prop {game_date}: {player} {market} {line}",
        f"Fair over: {fair_prob_over:.3f}, Book implied: {book_implied_over:.3f}",
        f"Edge: {edge:.3f} ({edge*100:.1f}%)",
    ]
    if actual_stat is not None:
        parts.append(f"Actual: {actual_stat}, Hit: {hit}")
    if context:
        if context.get("minutes"):
            parts.append(f"Minutes: {context['minutes']}")
        if context.get("opponent"):
            parts.append(f"vs {context['opponent']}")
        if context.get("home_away"):
            parts.append(f"({context['home_away']})")

    text = " | ".join(parts)
    embedding = await embed_text(text)

    metadata = {
        "sport": sport,
        "game_date": game_date,
        "player": player,
        "market": market,
        "line": line,
        "fair_prob_over": fair_prob_over,
        "book_implied_over": book_implied_over,
        "edge": round(edge, 4),
        "actual_stat": actual_stat,
        "hit": hit,
        **(context or {}),
    }

    return await store.store("prop_outcomes", text, embedding, metadata)
