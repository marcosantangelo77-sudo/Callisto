"""Improve pass: artifacts/sandbox/charts — sandbox output seals REAL bytes.

Before this pass the pipeline called run_python() without keep_workspace,
so by sealing time the produced files were deleted and store_sandbox_outputs
could only create child-attested refs — hashes pointing at bytes that no
longer existed. verify_artifacts() returned ok=False on every sealed claim
citing sandbox output, defeating property 3 at its own mechanism.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.artifacts import ArtifactStore, store_sandbox_outputs
from tools.sandbox import run_python


CODE_WRITES_FILE = (
    "open('table.csv','w').write('x,y\\n1,2\\n')\n"
    "print('done')\n"
    "result = {'rows': 1}\n"
)


def _run_with_workspace():
    return run_python(CODE_WRITES_FILE, wall_clock_s=30, keep_workspace=True)


class TestSandboxOutputSealsRealBytes:
    def test_engine_shape_refs_verify_ok(self, tmp_path):
        """The exact shape engine._store_sandbox now produces."""
        sbx = _run_with_workspace()
        assert sbx.status == "ok"
        from pathlib import Path

        store = ArtifactStore(root=tmp_path / "a")
        refs = store_sandbox_outputs(sbx, store, workspace=Path(sbx.workspace))
        file_ref = next(r for r in refs if r.name == "table.csv")
        # real bytes in the store, independently re-hashed — not attested-only
        assert not file_ref.meta.get("attested_by_child_only")
        assert store.get_bytes(file_ref.sha256) == b"x,y\n1,2\n"
        report = store.verify_artifacts(refs)
        assert report["ok"], report

    def test_child_hash_matches_store_bytes(self, tmp_path):
        """The hash the child reported is the hash of what got sealed."""
        from pathlib import Path

        sbx = _run_with_workspace()
        store = ArtifactStore(root=tmp_path / "a")
        refs = store_sandbox_outputs(sbx, store, workspace=Path(sbx.workspace))
        child = next(f for f in sbx.files if f["name"] == "table.csv")
        ref = next(r for r in refs if r.name == "table.csv")
        assert ref.sha256 == child["sha256"]

    def test_provenance_chain_intact(self, tmp_path):
        from pathlib import Path

        sbx = _run_with_workspace()
        store = ArtifactStore(root=tmp_path / "a")
        refs = store_sandbox_outputs(sbx, store, workspace=Path(sbx.workspace))
        stdout_ref = next(r for r in refs if r.name == "stdout")
        file_ref = next(r for r in refs if r.name == "table.csv")
        assert file_ref.data_refs == [stdout_ref.sha256]
        assert file_ref.code_sha256

    def test_error_run_still_seals_stdout_only(self, tmp_path):
        """A failed run has no trustworthy files; stdout still seals."""
        from pathlib import Path

        sbx = run_python("raise ValueError('boom')", keep_workspace=True)
        store = ArtifactStore(root=tmp_path / "a")
        refs = store_sandbox_outputs(sbx, store, workspace=None)
        assert all(r.name != "table.csv" for r in refs)
        assert store.verify_artifacts(refs)["ok"]


class TestEngineStoreSandbox:
    """engine._store_sandbox end-to-end: seals real bytes AND cleans up."""

    def test_seals_real_bytes_and_removes_workspace(self, tmp_path):
        from tools.pipeline.engine import _store_sandbox

        sbx = _run_with_workspace()
        ws = sbx.workspace
        store = ArtifactStore(root=tmp_path / "a")
        refs = _store_sandbox(sbx, store)
        file_ref = next(r for r in refs if r.name == "table.csv")
        assert store.get_bytes(file_ref.sha256) == b"x,y\n1,2\n"
        assert store.verify_artifacts(refs)["ok"]
        assert not os.path.exists(ws)  # scratch destroyed after sealing

    def test_no_workspace_attr_is_safe(self, tmp_path):
        from tools.pipeline.engine import _store_sandbox

        sbx = run_python("result = 1", wall_clock_s=30)  # default: no workspace
        store = ArtifactStore(root=tmp_path / "a")
        refs = _store_sandbox(sbx, store)
        assert any(r.name == "stdout" for r in refs)


class TestChartSpecArtifact:
    def test_store_chart_writes_one_spec_not_two(self, tmp_path):
        from tools.charts import chart_spec, store_chart

        store = ArtifactStore(root=tmp_path / "a")
        spec = chart_spec("t", {"s": [1, 2, 3]}, code="result = [1,2,3]")
        out = store_chart(spec, store, prefer_matplotlib=False)
        metas = [
            m for m in json.loads(store.index_path.read_text()).values()
            if (m.get("name") or "").endswith("spec")
        ]
        assert len(metas) == 1
        # and that one spec carries the code, so the chart regenerates
        stored = store.get_json(out["spec"].sha256)
        assert stored["code"] == "result = [1,2,3]"
