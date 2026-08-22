"""B2 build pass — sandboxed execution (tools/sandbox.py).

Targeted subset only; the full suite belongs to the merge gate.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sandbox import SandboxResult, run_python


def _isolation_layers(result):
    return set(result.isolation["layers"])


class TestBasicExecution:
    def test_stdout_and_return_value(self):
        r = run_python("print('hi')\nresult = {'answer': 6 * 7}", wall_clock_s=30)
        assert r.status == "ok"
        assert r.return_value == {"answer": 42}
        assert "hi" in r.stdout

    def test_inputs_reach_child_as_json(self):
        code = ("import json\n"
                "result = sum(json.load(open('nums.json')))\n")
        r = run_python(code, inputs={"nums": [1, 2, 3, 4]}, wall_clock_s=30)
        assert r.status == "ok" and r.return_value == 10

    def test_invalid_input_name_rejected(self):
        with pytest.raises(ValueError):
            run_python("pass", inputs={"../evil": 1})

    def test_error_status_on_exception(self):
        r = run_python("raise ValueError('boom')", wall_clock_s=30)
        assert r.status == "error"
        assert "boom" in r.stderr_tail

    def test_syntax_error_is_error_not_crash(self):
        r = run_python("def broken(:", wall_clock_s=30)
        assert r.status == "error"

    def test_result_dict_serialisable(self):
        d = run_python("result=1", wall_clock_s=30).to_dict()
        assert d["status"] == "ok"


class TestBoundary:
    def test_network_blocked_in_child(self):
        r = run_python(
            "import socket\nsocket.socket()\n", wall_clock_s=20
        )
        assert r.status == "error"
        assert "network access is disabled" in r.stderr_tail

    def test_env_scrubbed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "super-secret-test-key")
        monkeypatch.setenv("SOME_API_TOKEN", "hunter2")
        r = run_python(
            "import os\nresult = sorted(k for k in os.environ if 'SECRET' in k or 'TOKEN' in k)",
            wall_clock_s=20,
        )
        assert r.status == "ok"
        assert r.return_value == []

    def test_home_not_real_home(self):
        r = run_python("import os\nresult = os.environ.get('HOME','')",
                       wall_clock_s=20)
        assert r.status == "ok"
        home = r.return_value
        assert home != os.path.expanduser("~")

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt")
    def test_write_outside_workspace_denied_macos(self, tmp_path):
        probe = tmp_path / "escape_probe.txt"
        r = run_python(
            f"open({str(probe)!r}, 'w').write('x')", wall_clock_s=20
        )
        assert r.status == "error"
        assert not probe.exists()

    def test_write_inside_workspace_captured(self):
        r = run_python(
            "open('out.txt', 'w').write('hello')\nresult = 1", wall_clock_s=30
        )
        assert r.status == "ok"
        names = [f["name"] for f in r.files]
        assert "out.txt" in names
        rec = next(f for f in r.files if f["name"] == "out.txt")
        # sha256("hello") — content hash proves what was produced
        assert (
            rec["sha256"]
            == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )

    def test_workspace_destroyed_by_default(self):
        r = run_python("open('x.txt','w').write('x')", wall_clock_s=30)
        assert not hasattr(r, "workspace") or not getattr(r, "workspace", None)

    def test_keep_workspace_preserves(self):
        from pathlib import Path

        r = run_python("open('x.txt','w').write('x')",
                       wall_clock_s=30, keep_workspace=True)
        ws = Path(getattr(r, "workspace"))
        try:
            assert (ws / "x.txt").exists()
        finally:
            import shutil

            shutil.rmtree(ws, ignore_errors=True)


class TestLimits:
    def test_wall_clock_timeout(self):
        r = run_python("while True: pass", wall_clock_s=2)
        assert r.status == "timeout"
        assert "wall-clock limit exceeded" in (r.error or "")

    def test_timeout_kills_process(self):
        # If the child were still spinning after timeout, this box would be
        # loaded; run two sequential timeouts and confirm duration is bounded.
        for _ in range(2):
            r = run_python("while True: pass", wall_clock_s=2)
            assert r.status == "timeout"
            assert r.duration_s < 10


class TestReproducibilityPayload:
    """The seal story: code + stdout + return + file hashes must be a
    complete, serialisable record of the computation."""

    def test_full_payload_round_trips(self):
        import json

        r = run_python(
            "import json\n"
            "json.dump({'series': [1,2,3]}, open('data.json','w'))\n"
            "print('computed')\n"
            "result = {'n': 3}\n",
            wall_clock_s=30,
        )
        payload = r.to_dict()
        blob = json.dumps(payload)  # must be JSON-serialisable
        restored = json.loads(blob)
        assert restored["code"] == r.code
        assert restored["status"] == "ok"
        files = {f["name"]: f["sha256"] for f in restored["files"]}
        assert set(files) >= {"data.json"}

    def test_deterministic_output_hash(self):
        code = "open('o.txt','w').write(str(sorted([3,1,2])))\nresult=None"
        hashes = []
        for _ in range(2):
            r = run_python(code, wall_clock_s=30)
            hashes.append(next(f["sha256"] for f in r.files if f["name"] == "o.txt"))
        assert hashes[0] == hashes[1]
