"""Tests for the tools.embeddings -> tools.embedstore split.

Verifies:
  1. The facade re-exports the full public surface of the old module.
  2. Vector math / hashing / blob serialization helpers behave identically.
  3. The Ollama HTTP client (embed_text / embed_batch) against a mock transport.
  4. VectorStore end-to-end (store, search, dedup, merge, batch, stats,
     clustering) against a real temporary SQLite database.
"""

import json
import math

import httpx
import numpy as np
import pytest
import pytest_asyncio  # noqa: F401

import tools.embeddings as emb
from tools.embedstore import client as embed_client
from tools.embedstore import store as store_mod
from tools.embedstore.store import NEAR_DUP_THRESHOLD, VectorStore
from tools.embedstore.vectors import (
    _content_hash,
    _deserialize_embedding,
    _from_blob,
    _to_blob,
    cosine_similarity,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fake_db_utils(monkeypatch):
    """Replace tools.db_utils write helpers with direct passthroughs so
    VectorStore methods run against a plain aiosqlite connection."""
    import types

    async def execute_with_retry(db, sql, params=(), operation="op"):
        cur = await db.execute(sql, params)
        return cur

    async def commit_with_retry(db, operation="op"):
        await db.commit()

    class _NoLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def get_write_lock():
        return _NoLock()

    fake = types.SimpleNamespace(
        execute_with_retry=execute_with_retry,
        commit_with_retry=commit_with_retry,
        get_write_lock=get_write_lock,
    )
    # Methods import "from tools.db_utils import ..." lazily — patch the module.
    import tools.db_utils as real_db_utils

    monkeypatch.setattr(real_db_utils, "execute_with_retry", execute_with_retry)
    monkeypatch.setattr(real_db_utils, "commit_with_retry", commit_with_retry)
    monkeypatch.setattr(real_db_utils, "get_write_lock", get_write_lock)
    yield fake


@pytest_asyncio.fixture
async def store(tmp_path):
    """VectorStore on a temp SQLite file with a realistic schema."""
    vs = VectorStore(db_path=str(tmp_path / "test_vec.db"))
    await vs.initialize()
    await vs._db.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collection TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content_text TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            embedding_blob BLOB,
            metadata_json TEXT,
            model_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(collection, content_hash)
        )
        """
    )
    await vs._db.commit()
    yield vs
    await vs.close()


def rand_unit(dim=768, seed=None):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim)
    return (v / np.linalg.norm(v)).tolist()


# ── Facade surface ──────────────────────────────────────────────────────


class TestFacadeSurface:
    def test_public_names_reexported(self):
        for name in [
            "EMBED_DIM", "EMBED_MODEL", "OLLAMA_BASE", "DB_PATH",
            "NEAR_DUP_THRESHOLD", "VectorStore", "close_client",
            "cosine_similarity", "embed_text", "embed_batch",
            "embed_game_context", "embed_prop_outcome",
        ]:
            assert hasattr(emb, name), f"facade missing {name}"

    def test_private_helpers_still_importable(self):
        for name in ["_content_hash", "_to_blob", "_from_blob",
                     "_deserialize_embedding", "_get_client"]:
            assert hasattr(emb, name)

    def test_identity_with_submodules(self):
        assert emb.VectorStore is store_mod.VectorStore
        assert emb.embed_text is embed_client.embed_text
        assert emb.NEAR_DUP_THRESHOLD == NEAR_DUP_THRESHOLD

    def test_constants_values(self):
        assert emb.EMBED_DIM == 768
        assert emb.NEAR_DUP_THRESHOLD == 0.97

    def test_import_tools_embeddings_does_not_shadow_package(self):
        # tools/embeddings.py must remain a module (not be shadowed by a
        # tools/embeddings/ directory) so existing imports keep working.
        import importlib.util

        spec = importlib.util.find_spec("tools.embeddings")
        assert spec is not None
        assert spec.origin.endswith("tools/embeddings.py")


# ── Vector helpers ──────────────────────────────────────────────────────


class TestVectors:
    def test_content_hash_deterministic_and_short(self):
        h1 = _content_hash("hello world")
        h2 = _content_hash("hello world")
        h3 = _content_hash("hello world!")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16

    def test_cosine_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-9

    def test_cosine_orthogonal(self):
        assert abs(cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_cosine_opposite(self):
        assert abs(cosine_similarity([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9

    def test_cosine_zero_vector_safe(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_cosine_known_value(self):
        got = cosine_similarity([3.0, 4.0], [4.0, 3.0])
        expected = 24.0 / 25.0
        assert abs(got - expected) < 1e-9

    def test_blob_roundtrip(self):
        emb_list = rand_unit(seed=42)
        blob = _to_blob(emb_list)
        assert isinstance(blob, bytes)
        assert len(blob) == 768 * 4  # float32
        restored = _from_blob(blob)
        assert restored.dtype == np.float32
        np.testing.assert_allclose(restored, np.array(emb_list, dtype=np.float32))

    def test_from_blob_corrupt_size_raises(self):
        with pytest.raises(ValueError, match="Corrupted embedding blob"):
            _from_blob(b"\x00" * 10)

    def test_deserialize_prefers_blob(self):
        vec = rand_unit(seed=7)
        blob = _to_blob(vec)
        out = _deserialize_embedding(blob, "[1,2,3]")
        np.testing.assert_allclose(out, np.array(vec, dtype=np.float32))

    def test_deserialize_json_fallback(self):
        out = _deserialize_embedding(None, "[0.5, 0.25]")
        np.testing.assert_allclose(out, np.array([0.5, 0.25], dtype=np.float32))


# ── Ollama client ───────────────────────────────────────────────────────


def _mock_embed_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=60.0)


class TestOllamaClient:
    @pytest.mark.anyio
    async def test_embed_text_single(self, monkeypatch):
        calls = []

        async def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"embeddings": [[0.1] * 8]})

        ac = _mock_embed_transport(handler)

        def fake_get_client():
            return ac

        monkeypatch.setattr(embed_client, "_get_client", fake_get_client)
        vec = await embed_client.embed_text("nomic hello")
        assert len(vec) == 8
        assert calls[0]["input"] == "nomic hello"
        await ac.aclose()

    @pytest.mark.anyio
    async def test_embed_text_empty_raises(self, monkeypatch):
        async def handler(request):
            return httpx.Response(200, json={"embeddings": []})

        ac = _mock_embed_transport(handler)

        def fake_get_client():
            return ac

        monkeypatch.setattr(embed_client, "_get_client", fake_get_client)
        with pytest.raises(ValueError, match="No embedding returned"):
            await embed_client.embed_text("nothing here")
        await ac.aclose()

    @pytest.mark.anyio
    async def test_embed_batch_batches_requests(self, monkeypatch):
        payloads = []

        async def handler(request):
            body = json.loads(request.content)
            payloads.append(body["input"])
            n = len(body["input"])
            return httpx.Response(
                200, json={"embeddings": [[float(i)] * 4 for i in range(n)]}
            )

        ac = _mock_embed_transport(handler)

        def fake_get_client():
            return ac

        monkeypatch.setattr(embed_client, "_get_client", fake_get_client)
        texts = [f"t{i}" for i in range(5)]
        out = await embed_client.embed_batch(texts, batch_size=2)
        assert len(out) == 5
        assert payloads == [["t0", "t1"], ["t2", "t3"], ["t4"]]
        await ac.aclose()

    @pytest.mark.anyio
    async def test_close_client_resets_singleton(self, monkeypatch):
        opened = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.is_closed = False
                opened.append(self)

            async def aclose(self):
                self.is_closed = True

        monkeypatch.setattr(embed_client.httpx, "AsyncClient", FakeClient)
        c1 = embed_client._get_client()
        await embed_client.close_client()
        assert c1.is_closed
        c2 = embed_client._get_client()
        assert c2 is not c1
        await embed_client.close_client()


# ── VectorStore ─────────────────────────────────────────────────────────


class TestVectorStoreCRUD:
    @pytest.mark.anyio
    async def test_store_returns_rowid_and_persists(self, store):
        vec = rand_unit(seed=1)
        row_id = await store.store("coll_a", "some text", vec, {"k": "v"})
        assert row_id > 0
        rows = await store.get_all("coll_a")
        assert len(rows) == 1
        assert rows[0]["text"] == "some text"
        assert rows[0]["metadata"] == {"k": "v"}

    @pytest.mark.anyio
    async def test_store_dedup_by_content_hash(self, store):
        vec = rand_unit(seed=2)
        id1 = await store.store("c", "dup text", vec)
        id2 = await store.store("c", "dup text", vec)
        assert id1 == id2
        stats = await store.get_collection_stats("c")
        assert stats["count"] == 1

    @pytest.mark.anyio
    async def test_search_top1_self_similarity(self, store):
        vecs = [rand_unit(seed=i) for i in range(5)]
        for i, v in enumerate(vecs):
            await store.store("s", f"text {i}", v)
        results = await store.search("s", vecs[3], top_k=1)
        assert len(results) == 1
        assert results[0]["text"] == "text 3"
        assert results[0]["similarity"] >= 0.999999

    @pytest.mark.anyio
    async def test_search_orders_descending(self, store):
        base = rand_unit(seed=10)
        near = list(np.array(base) + np.array(rand_unit(seed=11)) * 0.05)
        far = list(np.array(base) * -1.0)  # maximally dissimilar
        await store.store("o", "far", far)
        await store.store("o", "near", near)
        await store.store("o", "base", base)
        results = await store.search("o", base, top_k=3)
        assert results[0]["text"] == "base"
        sims = [r["similarity"] for r in results]
        assert sims == sorted(sims, reverse=True)

    @pytest.mark.anyio
    async def test_search_min_similarity_filters(self, store):
        base = rand_unit(seed=20)
        far = list(np.array(base) * -1.0)
        await store.store("m", "far", far)
        results = await store.search("m", base, top_k=10, min_similarity=0.5)
        assert results == []

    @pytest.mark.anyio
    async def test_search_empty_collection(self, store):
        assert await store.search("nothing", rand_unit(seed=30)) == []

    @pytest.mark.anyio
    async def test_model_filter_excludes_drift_rows(self, store):
        v1 = rand_unit(seed=40)
        v2 = rand_unit(seed=41)
        await store.store("d", "old model row", v1, model_name="old-model")
        await store.store("d", "new model row", v2, model_name="new-model")
        res_new = await store.search("d", v2, top_k=5, model_name="new-model")
        assert [r["text"] for r in res_new] == ["new model row"]
        # NULL-model rows stay included (backcompat): insert one directly.
        await store.store("d", "legacy row", rand_unit(seed=42))
        await store._db.execute(
            "UPDATE embeddings SET model_name = NULL WHERE content_text = 'legacy row'"
        )
        await store._db.commit()
        res_all = await store.search("d", v2, top_k=10, model_name="new-model")
        texts = {r["text"] for r in res_all}
        assert "legacy row" in texts
        assert "old model row" not in texts

    @pytest.mark.anyio
    async def test_store_batch_counts_inserts(self, store):
        items = [(f"batch text {i}", rand_unit(seed=50 + i), {"i": i}) for i in range(4)]
        items.append(("batch text 0", rand_unit(seed=99), None))  # dup hash
        count = await store.store_batch("b", items)
        assert count == 4

    @pytest.mark.anyio
    async def test_delete_collection(self, store):
        await store.store("gone", "t1", rand_unit(seed=60))
        await store.store("stays", "t2", rand_unit(seed=61))
        deleted = await store.delete_collection("gone")
        assert deleted == 1
        assert (await store.get_collection_stats("gone"))["count"] == 0
        assert (await store.get_collection_stats("stays"))["count"] == 1

    @pytest.mark.anyio
    async def test_get_collection_stats_global(self, store):
        await store.store("x", "a", rand_unit(seed=70))
        await store.store("y", "b", rand_unit(seed=71))
        await store.store("y", "c", rand_unit(seed=72))
        stats = await store.get_collection_stats()
        assert stats["total"] == 3
        assert stats["collections"]["y"] == 2


class TestStoreOrMerge:
    @pytest.mark.anyio
    async def test_exact_duplicate_short_circuit(self, store):
        vec = rand_unit(seed=80)
        first = await store.store_or_merge("nm", "same words", vec)
        again = await store.store_or_merge("nm", "same words", vec)
        assert first["action"] == "inserted"
        assert again == {
            "action": "duplicate",
            "id": first["id"],
            "similarity": 1.0,
        }

    @pytest.mark.anyio
    async def test_near_duplicate_merges_metadata(self, store):
        base = rand_unit(seed=81)
        await store.store_or_merge(
            "nm", "original claim text", base, {"source": "a", "merge_count": 1}
        )
        noisy = list(np.array(base) + np.array(rand_unit(seed=82)) * 0.001)
        result = await store.store_or_merge(
            "nm", "slightly different claim", noisy, {"source": "b"}
        )
        assert result["action"] == "merged"
        rows = await store._db.execute(
            "SELECT metadata_json FROM embeddings WHERE id = ?", (result["id"],)
        )
        meta = json.loads((await rows.fetchone())[0])
        assert meta["source"] == "b"  # new keys win
        assert meta["merge_count"] >= 2
        assert "last_merged_at" in meta

    @pytest.mark.anyio
    async def test_dissimilar_vector_inserts(self, store):
        a = rand_unit(seed=83)
        b = rand_unit(seed=84)
        r1 = await store.store_or_merge("ins", "alpha", a)
        r2 = await store.store_or_merge("ins", "beta", b)
        assert r1["action"] == "inserted"
        assert r2["action"] == "inserted"
        assert r2["similarity"] is None
        # Two random unit 768-dim vectors are almost surely below threshold.
        assert r2["id"] != r1["id"]

    @pytest.mark.anyio
    async def test_threshold_parameter_is_honored(self, store):
        base = rand_unit(seed=85)
        await store.store_or_merge("th", "first", base)
        noisy = list(np.array(base) + np.array(rand_unit(seed=86)) * 0.01)
        strict = await store.store_or_merge(
            "th", "second", noisy, near_dup_threshold=0.99999999
        )
        assert strict["action"] == "inserted"


class TestPeriodAndCoverage:
    @pytest.mark.anyio
    async def test_get_embeddings_by_period(self, store):
        from datetime import datetime

        await store.store(
            "p", "hist", rand_unit(seed=90),
            {"data_period": "historical", "game_date": "2025-01-01"},
        )
        await store.store(
            "p", "recent", rand_unit(seed=91),
            {"data_period": "recent", "game_date": "2026-03-01"},
        )
        hist = await store.get_embeddings_by_period("p", "historical")
        assert [r["text"] for r in hist] == ["hist"]
        everything = await store.get_embeddings_by_period("p", "all")
        assert len(everything) == 2
        assert all(isinstance(r, dict) for r in everything)

    @pytest.mark.anyio
    async def test_embedding_coverage_shape(self, store):
        await store.store(
            "game_contexts", "g1", rand_unit(seed=92),
            {"sport": "nba", "game_date": "2025-11-05", "data_period": "recent"},
        )
        cov = await store.get_embedding_coverage()
        assert cov["total"] >= 1
        assert "game_contexts" in cov["collections"]
        assert cov["collections"]["game_contexts"]["count"] >= 1
        assert "recent" in cov["game_contexts_by_period"]
        assert cov["game_contexts_by_sport"].get("nba") == 1


class TestClustering:
    @pytest.mark.anyio
    async def test_cluster_groups_similar_items(self, store):
        center_a = rand_unit(seed=100)
        cluster_a = [
            list(np.array(center_a) + np.array(rand_unit(seed=101 + i)) * 0.01)
            for i in range(3)
        ]
        center_b = rand_unit(seed=110)
        cluster_b = [
            list(np.array(center_b) + np.array(rand_unit(seed=111 + i)) * 0.01)
            for i in range(2)
        ]
        for i, v in enumerate(cluster_a + cluster_b):
            await store.store("clu", f"item {i}", v)
        clusters = await store.cluster_by_similarity("clu", threshold=0.95)
        sizes = sorted([len(c) for c in clusters], reverse=True)
        assert sizes == [3, 2]

    @pytest.mark.anyio
    async def test_cluster_empty_collection(self, store):
        assert await store.cluster_by_similarity("void") == []

    @pytest.mark.anyio
    async def test_cluster_data_period_filter(self, store):
        c = rand_unit(seed=120)
        twin = list(np.array(c) + np.array(rand_unit(seed=121)) * 0.005)
        await store.store("cf", "h1", c, {"data_period": "historical"})
        await store.store("cf", "r1", twin, {"data_period": "recent"})
        clusters = await store.cluster_by_similarity(
            "cf", threshold=0.9, data_period="historical"
        )
        assert clusters == []  # only one historical row -> no pair clusters
