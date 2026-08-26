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

Implementation lives in the ``tools.embedstore`` package; this module is a
backwards-compatible facade re-exporting the full public surface so that
``import tools.embeddings`` keeps working for every existing caller.
"""

from tools.embedstore.client import (  # noqa: F401
    EMBED_MODEL,
    OLLAMA_BASE,
    close_client,
    embed_batch,
    embed_text,
    _get_client,
)
from tools.embedstore.store import (  # noqa: F401
    DB_PATH,
    NEAR_DUP_THRESHOLD,
    VectorStore,
)
from tools.embedstore.vectors import (  # noqa: F401
    EMBED_DIM,
    _content_hash,
    _deserialize_embedding,
    _from_blob,
    _to_blob,
    cosine_similarity,
)
from tools.embedstore.contexts import (  # noqa: F401
    embed_game_context,
    embed_prop_outcome,
)

__all__ = [
    "EMBED_DIM",
    "EMBED_MODEL",
    "OLLAMA_BASE",
    "DB_PATH",
    "NEAR_DUP_THRESHOLD",
    "VectorStore",
    "close_client",
    "cosine_similarity",
    "embed_text",
    "embed_batch",
    "embed_game_context",
    "embed_prop_outcome",
]
