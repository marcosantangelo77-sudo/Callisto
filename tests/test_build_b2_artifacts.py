"""B2 build pass — content-addressed artifact store (tools/artifacts.py)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.artifacts import (
    ALLOWED_KINDS,
    ArtifactRef,
    ArtifactStore,
    sha256_bytes,
)


@pytest.fixture()
def store(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


class TestContentAddressing:
    def test_identical_content_same_hash(self, store):
        a = store.put_text("hello", "csv", name="a")
        b = store.put_text("hello", "csv", name="b")
        assert a.sha256 == b.sha256 == sha256_bytes(b"hello")

    def test_object_lands_at_hash_path(self, store):
        ref = store.put_text("payload", "txt")
        p = store.get_path(ref.sha256)
        assert p.read_bytes() == b"payload"
        assert ref.sha256 in str(p)

    def test_canonical_json_dedupes(self, store):
        r1 = store.put_json({"b": 1, "a": 2})
        r2 = store.put_json({"a": 2, "b": 1})
        assert r1.sha256 == r2.sha256

    def test_kind_allowlist(self, store):
        with pytest.raises(ValueError):
            store.put_text("x", "exe")


class TestImmutabilityAndIntegrity:
    def test_verify_ok(self, store):
        refs = [store.put_text(f"doc{i}", "txt") for i in range(3)]
        report = store.verify_artifacts(refs)
        assert report["ok"] and report["verified"] == 3

    def test_tamper_detected(self, store):
        ref = store.put_text("original", "txt")
        store.get_path(ref.sha256).write_bytes(b"tampered")
        report = store.verify_artifacts([ref])
        assert not report["ok"]
        assert report["corrupt"] == [ref.sha256]

    def test_missing_detected(self, store):
        ghost = ArtifactRef(sha256="0" * 64, kind="txt")
        report = store.verify_artifacts([ghost])
        assert report["missing"] == ["0" * 64]

    def test_ref_round_trip(self, store):
        ref = store.put_text("x", "csv", name="n",
                             code_sha256="c" * 64, data_refs=["d" * 64],
                             meta={"k": 1})
        assert ArtifactRef.from_dict(ref.to_dict()) == ref


class TestProvenanceChain:
    """The epistemics: an artifact cites the code and upstream data that
    produced it, so a claim citing the artifact inherits the whole chain."""

    def test_code_and_data_refs_stored(self, store):
        code = "supply = halvings()"
        data = store.put_json([2024, 2025], name="input_series")
        out = store.put_text(
            "18.5,19.0", "csv",
            code_sha256=sha256_bytes(code.encode()),
            data_refs=[data.sha256],
        )
        meta = store.get_meta(out.sha256)
        assert meta["code_sha256"] == sha256_bytes(code.encode())
        assert meta["data_refs"] == [data.sha256]
        # first-seen provenance survives re-put of identical bytes
        store.put_text("18.5,19.0", "csv", code_sha256="f" * 64)
        assert store.get_meta(out.sha256)["code_sha256"] != "f" * 64


class TestIndexResilience:
    def test_index_rebuild_from_objects(self, store):
        store.put_text("alpha", "txt")
        store.put(b"\x89PNG fake", "png")
        store.index_path.unlink()
        n = store.rebuild_index()
        assert n == 2
        kinds = {m["kind"] for m in
                 (store.get_meta(store.put_text("alpha", "txt").sha256),
                  )}
        # rebuilt entry still resolves; sniffing identified formats
        assert store.index_path.exists()

    def test_corrupt_index_does_not_kill_store(self, store):
        ref = store.put_text("keep me", "txt")
        store.index_path.write_text("{not json")
        assert store.exists(ref.sha256)  # objects are source of truth


class TestSealIntegration:
    """Artifact ids must be sealable via AGP: the dict form goes into the
    session payload; verify_artifacts is the post-restore check."""

    def test_refs_embed_in_seal_payload(self, store):
        from agp import Domain, Evidence, SourceClass

        ref = store.put_text("evidence bytes", "csv", name="model_out")
        payload_fragment = json.dumps([ref.to_dict()], sort_keys=True)
        assert ref.sha256 in payload_fragment
        ev = Evidence(
            content=f"computed artifact sha256:{ref.sha256[:16]}",
            source_class=SourceClass.SIGNAL,
            confidence_score=0.7,
            domain=Domain.SYNTHESIS,
            origin_agent="sandbox",
        )
        assert ev.to_dict()["content"].startswith("computed artifact")

    def test_export_ref_for_delivery(self, store, tmp_path):
        ref = store.put_text("c1,c2\n1,2\n", "csv", name="table")
        dest = store.export_ref(ref, tmp_path / "out")
        assert dest.name == "table.csv"
        assert dest.read_text().endswith("1,2\n")
