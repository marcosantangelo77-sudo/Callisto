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
