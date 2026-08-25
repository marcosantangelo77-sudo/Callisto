"""Artifacts/sandbox improve pass — regression pins.

Covers two fixes:
1. The pipeline's compute stage now runs run_python(keep_workspace=True),
   so sandbox-produced FILE BYTES are read and re-hashed into the artifact
   store. Previously only child-attested hashes were recorded
   (meta['attested_by_child_only']=True) and verify_artifacts reported them
   missing — evidence you could cite but not check.
2. charts.store_chart no longer mutates the caller's spec dict (it used to
   pop 'code' in place).
"""
import asyncio
import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_pipeline_test_module():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tests", "test_build_p1_pipeline.py")
    spec = importlib.util.spec_from_file_location("tp_reuse", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_pipeline_with_file_producing_compute(tmp_path):
    """Drive the REAL pipeline with a leaf that computes and writes a file."""
    tp = _load_pipeline_test_module()
    code = ("import json\n"
            "series = json.load(open('series.json'))\n"
            "json.dump({'mean': sum(series) / len(series)}, open('out.json','w'))\n"
            "result = {'mean': sum(series) / len(series)}")
    model = tp.ScriptedModel({
        "Architect": [{"content": tp._decompose_response()}],
        "Manager": [{"content": json.dumps(
                        {"answer": None,
                         "compute": {"code": code,
                                     "inputs": {"series": [1.0, 2.0, 3.0]}}})},
                    {"content": json.dumps(
                        {"answer": "mean computed as 2.0",
                         "proposed_confidence": 0.8})},
                    {"content": tp._answer(0.7)}],
    })
    pipeline, _ = tp._make(tmp_path, model=model)
    result = asyncio.new_event_loop().run_until_complete(
        pipeline.run("Compute question?", today=date(2026, 8, 22)))
    return pipeline, result


class TestSandboxBytesAreStoredNotAttested:
    def test_file_bytes_in_store_and_verifiable(self, tmp_path):
        pipeline, result = _run_pipeline_with_file_producing_compute(tmp_path)
        assert result.sealed, result.refusal_reason
        file_refs = [r for r in result.artifact_refs if r.name == "out.json"]
        assert file_refs, f"no out.json artifact among {result.artifact_refs}"
        ref = file_refs[0]
        # The bytes themselves are in the store...
        assert pipeline.store.exists(ref.sha256)
        payload = json.loads(pipeline.store.get_bytes(ref.sha256))
        assert payload == {"mean": 2.0}
        # ...so verification re-hashes real bytes rather than reporting missing.
        report = pipeline.store.verify_artifacts([ref])
        assert report["ok"], report

    def test_no_attested_only_refs_remain(self, tmp_path):
        pipeline, result = _run_pipeline_with_file_producing_compute(tmp_path)
        assert result.artifact_refs
        for ref in result.artifact_refs:
            meta = pipeline.store.get_meta(ref.sha256) or {}
            assert not meta.get("attested_by_child_only"), (
                f"ref {ref.name} is still child-attested only")

    def test_scratch_workspace_cleaned_up(self, tmp_path):
        import glob
        import tempfile
        import time

        started = time.time()
        pipeline, result = _run_pipeline_with_file_producing_compute(tmp_path)
        assert result.sealed or result.refusal_reason
        # Every workspace THIS run created must be gone. (mtime-windowed:
        # other processes on this shared machine leave their own stale
        # callisto_sbx_* dirs; those are not ours to assert about.)
        leftovers = [p for p in glob.glob(os.path.join(tempfile.gettempdir(),
                                                       "callisto_sbx_*"))
                     if os.path.getmtime(p) >= started]
        assert not leftovers


class TestStoreChartDoesNotMutateSpec:
    def test_caller_spec_unchanged(self, tmp_path):
        from tools.artifacts import ArtifactStore
        from tools.charts import store_chart

        store = ArtifactStore(root=tmp_path / "art")
        spec = {"title": "t", "series": {"a": [1.0, 2.0]},
                "code": "x=1", "notes": "n"}
        before = dict(spec)
        out = store_chart(spec, store)
        assert spec == before, "store_chart mutated the caller's spec"
        stored = store.get_json(out["spec"].sha256)
        assert stored["code"] == "x=1"  # code still sealed in the spec artifact

    def test_respec_reuse_gives_identical_chart(self, tmp_path):
        """The point of not mutating: the same spec renders identically twice."""
        from tools.artifacts import ArtifactStore
        from tools.charts import store_chart

        store = ArtifactStore(root=tmp_path / "art")
        spec = {"title": "t", "series": {"a": [1, 2]}, "code": "gen()"}
        r1 = store_chart(spec, store)
        r2 = store_chart(spec, store)  # would differ if 'code' had been popped
        assert r1["chart"].sha256 == r2["chart"].sha256


# ── Checkpoint artifact-ref integrity ──────────────────────────────────────
#
# Invariant: for every sealed run, session.to_dict()["artifact_refs"] exactly
# represents PipelineResult.artifact_refs and the keyed seal covers them —
# for fresh compute runs AND runs resumed from the same checkpointer/store.
# A hash-only (legacy/malformed) checkpoint must never produce a sealed
# artifactless session: the pipeline fails closed, unsealed.

def _load_ckpt_helpers():
    """Reuse the real checkpoint harness fixtures from test_fix_ckpt_confidence
    patterns but drive the file-producing compute model directly."""
    from tools.pipeline.checkpoint import FileCheckpointer  # noqa: F401


def _compute_model_and_pipeline(tp, tmp_path, checkpointer=None, store=None):
    code = ("import json\n"
            "series = json.load(open('series.json'))\n"
            "json.dump({'mean': sum(series) / len(series)}, open('out.json','w'))\n"
            "result = {'mean': sum(series) / len(series)}")
    model = tp.ScriptedModel({
        "Architect": [{"content": tp._decompose_response()}],
        "Manager": [{"content": json.dumps(
                        {"answer": None,
                         "compute": {"code": code,
                                     "inputs": {"series": [1.0, 2.0, 3.0]}}})},
                    {"content": json.dumps(
                        {"answer": "mean computed as 2.0",
                         "proposed_confidence": 0.8})},
                    {"content": tp._answer(0.7)}],
    })
    if store is None:
        store = tp.ArtifactStore(root=tmp_path / "artifacts")
    pipeline = tp.ResearchPipeline(
        model=model, adversary_router=tp._QuietAdversary(),
        transport=tp.fixture_transport(tp._routes()), store=store,
        ledger=tp.ProvenanceLedger(), checkpointer=checkpointer)
    return pipeline


def _run(pipe, question="Compute question?"):
    return asyncio.new_event_loop().run_until_complete(
        pipe.run(question, today=date(2026, 8, 22)))


class TestCheckpointArtifactRefIntegrity:
    def test_fresh_run_session_refs_match_result_refs_and_seal(self, tmp_path):
        tp = _load_pipeline_test_module()
        from agp import AGPSession
        pipeline = _compute_model_and_pipeline(tp, tmp_path)
        result = _run(pipeline)
        assert result.sealed, result.refusal_reason
        assert result.artifact_refs, "no refs produced by compute"
        session_refs = [r["sha256"] for r in
                        result.session.to_dict()["artifact_refs"]]
        assert session_refs == [r.sha256 for r in result.artifact_refs]
        # Full fidelity: kind/name/meta ride through, not just hashes.
        assert result.session.to_dict()["artifact_refs"] == \
            [r.to_dict() for r in result.artifact_refs]
        assert AGPSession.verify_seal(result.session.to_dict())
        # Tampering with a serialized ref breaks seal verification.
        d = result.session.to_dict()
        d["artifact_refs"][0]["name"] = "tampered.json"
        assert not AGPSession.verify_seal(d)

    def test_resumed_run_same_store_restores_refs_and_verifies(self, tmp_path):
        tp = _load_pipeline_test_module()
        from tools.pipeline.checkpoint import FileCheckpointer
        from agp import AGPSession
        cp = FileCheckpointer(root=tmp_path / "ckpt")
        shared_store = tp.ArtifactStore(root=tmp_path / "artifacts")
        fresh = _compute_model_and_pipeline(
            tp, tmp_path, checkpointer=cp, store=shared_store)
        r1 = _run(fresh)
        assert r1.sealed, r1.refusal_reason

        # Same question/domain/date + same checkpointer + same store.
        resumed = _compute_model_and_pipeline(
            tp, tmp_path / "r2", checkpointer=cp, store=shared_store)
        r2 = _run(resumed)
        assert r2.sealed, r2.refusal_reason
        answer_stages = [s for s in (r2.trace.stages or [])
                         if getattr(s, "stage", "") == "answer_leaf"]
        assert any(getattr(s, "resumed", False) for s in answer_stages), (
            "expected the compute answer stage to be resumed, not recomputed")
        assert [r.to_dict() for r in r2.artifact_refs] == \
            [r.to_dict() for r in r1.artifact_refs]
        assert r2.session.to_dict()["artifact_refs"] == \
            [r.to_dict() for r in r2.artifact_refs]
        assert AGPSession.verify_seal(r2.session.to_dict())

    def test_resume_with_empty_store_refuses_instead_of_sealing(self, tmp_path):
        tp = _load_pipeline_test_module()
        from tools.pipeline.checkpoint import FileCheckpointer
        cp = FileCheckpointer(root=tmp_path / "ckpt")
        fresh = _compute_model_and_pipeline(tp, tmp_path, checkpointer=cp)
        r1 = _run(fresh)
        assert r1.sealed, r1.refusal_reason
        assert r1.artifact_refs

        # New/empty store: cited bytes are gone -> A6 gate must refuse.
        resumed = _compute_model_and_pipeline(
            tp, tmp_path / "r2", checkpointer=cp,
            store=tp.ArtifactStore(root=tmp_path / "empty_art"))
        r2 = _run(resumed)
        assert not r2.sealed
        assert "artifact verification failed" in (r2.refusal_reason or "")

    def test_hash_only_checkpoint_cannot_seal_artifactlessly(self, tmp_path):
        tp = _load_pipeline_test_module()
        from tools.pipeline.checkpoint import FileCheckpointer
        cp = FileCheckpointer(root=tmp_path / "ckpt")
        shared_store = tp.ArtifactStore(root=tmp_path / "artifacts")
        fresh = _compute_model_and_pipeline(
            tp, tmp_path, checkpointer=cp, store=shared_store)
        r1 = _run(fresh)
        assert r1.sealed and r1.artifact_refs

        # Rewrite the saved answer_leaf checkpoints to legacy hash-only form:
        # artifact_sha256s present, full artifact_refs stripped.
        n_stripped = 0
        for ck in cp.list_all():
            if ck.stage != "answer_leaf":
                continue
            leaf = dict(ck.payload.get("leaf") or {})
            if leaf.get("artifact_sha256s"):
                leaf.pop("artifact_refs", None)
                ck.payload["leaf"] = leaf
                cp.save(ck.run, ck.stage, ck.input_hash, ck.payload,
                        claim_ids=ck.claim_ids)
                n_stripped += 1
        assert n_stripped, "no hash-only checkpoint produced"

        resumed = _compute_model_and_pipeline(
            tp, tmp_path / "r2", checkpointer=cp, store=shared_store)
        r2 = _run(resumed)
        assert not r2.sealed, (
            "a legacy hash-only checkpoint led to a sealed session with "
            f"refs={[(r.sha256) for r in r2.artifact_refs]}")
        assert r2.refusal_reason, "fail-closed must carry an honest reason"
        assert "refus" in r2.refusal_reason.lower()
