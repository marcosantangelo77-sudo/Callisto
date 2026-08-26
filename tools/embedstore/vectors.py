"""Vector math, hashing, and blob serialization helpers.

Extracted from tools/embeddings.py.
"""

import hashlib
import json

import numpy as np

EMBED_DIM = 768  # nomic-embed-text output dimension


def _content_hash(text: str) -> str:
    """SHA-256 hash of content for dedup."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Pure Python fallback for single pairs."""
    import math

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
