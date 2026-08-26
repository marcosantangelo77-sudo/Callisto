"""Tests for `callisto runs` / `callisto show` — tools.cli.runs.

The product path: `callisto ask` persists a run record; `runs` and `show`
read it back and re-verify its integrity (artifact hashes re-hashed against
the artifact store, fetch digests re-checked). These tests pin that
roundtrip and the honesty contract: a mismatch is reported, never
swallowed, and the seal key value is never printed.
"""
import asyncio
import hashlib
import json

import pytest

from argparse import Namespace
from types import SimpleNamespace as NS

from callisto import (
    _cmd_ask,
    _cmd_runs,
    _cmd_show,
    _load_run,
    _persist_run,
    _result_record,
    _verify_artifact,
)
from tools.cli.runs import _fetch_digest_status


def _fake_result():
    """A PipelineResult-shaped object like a real sealed run."""
    from tools.artifacts import ArtifactRef
    return NS(
        sealed=True, refusal_reason="",
        conclusion="Foundry concentration is the binding constraint.",
        confidence_score=0.34, confidence_tier="SPECULATIVE",
        leaves=[NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256="a" * 64, kind="csv",
                                   name="concentration.csv")],
        fetches=[NS(source_name="openalex", url="https://api.openalex.org/x",
                    content_sha256="b" * 64)],
        objections=[NS(text="one independent source only")],
        notes=[])


class _FakeRouter:
    class _Ledger:
        def snapshot(self):
            return {"by_tier": {"gpu1": {"calls": 3}}}

    def __init__(self):
        self.endpoints = ["gpu1"]
        self.task_classes = {"decompose": "gpu1"}
        self.default_tier_name = "gpu1"
        self._health = {"status": "ok"}
        self.cost_ledger = self._Ledger()

    async def check_health(self, tier):
        return self._health


@pytest.fixture(autouse=True)
def _valid_seal_key(monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)


@pytest.fixture
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _wired(monkeypatch, result=None):
    router = _FakeRouter()
    engine = NS(adversary_router=None)

    async def run(q):
        return result if result is not None else _fake_result()

    engine.run = run

    def load_router(path):
        return router

    def make_engine(router_, self_review):
        return engine

    monkeypatch.setattr("callisto._load_router", load_router)
    monkeypatch.setattr("callisto._make_engine", make_engine)


# ── persist roundtrip ─────────────────────────────────────────────────────

class TestPersistRoundtrip:
    def test_ask_persists_record_that_runs_and_show_can_read(
            self, runs_env, tmp_path, monkeypatch, capsys):
        """The full product path: ask -> persisted JSON -> show reprints."""
        # real bytes behind the artifact hash so show can verify them
        payload = b"payload-bytes-for-roundtrip"
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(tmp_path / "arts"))
        from tools.artifacts import ArtifactStore
        ArtifactStore(root=tmp_path / "arts").put(
            payload, kind="csv", name="concentration.csv")
        result = _fake_result()
        result.artifact_refs[0].sha256 = digest
        _wired(monkeypatch, result)

        from argparse import Namespace as _Args
        rc = asyncio.run(_cmd_ask(_Args(question="why now", backend=None,
                                        self_review=False, providers="x")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "run      :" in out

        saved = list(runs_env.glob("*.json"))
        assert len(saved) == 1
        rec = json.loads(saved[0].read_text())
        assert rec["question"] == "why now"
        assert rec["sealed"] is True
        assert rec["artifacts"][0]["sha256"] == digest

        # runs lists it; show re-prints with verified artifact
        rc = _cmd_runs(Namespace(limit=20))
        assert rc == 0 and "SEALED" in capsys.readouterr().out
        rc = _cmd_show(Namespace(run_id=saved[0].stem))
        out = capsys.readouterr().out
        assert rc == 0
        assert "Foundry concentration" in out
        assert "[ok" in out

    def test_record_roundtrip_preserves_every_field(self, runs_env):
        rec = _result_record(_fake_result(), "some question")
        path = _persist_run(rec)
        loaded, loaded_path = _load_run(path.stem)
        assert loaded_path == path
        again = json.loads(json.dumps(rec))
        assert again == loaded

    def test_load_run_by_unique_prefix(self, runs_env):
        path = _persist_run(_result_record(_fake_result(), "q"))
        stem = path.stem
        prefix = stem[:12]
        loaded, _ = _load_run(prefix)
        assert loaded is not None
        assert loaded["question"] == "q"

    def test_load_run_ambiguous_prefix_raises(self, runs_env):
        p1 = _persist_run(_result_record(_fake_result(), "q one"))
        rec2 = _result_record(_fake_result(), "q two")
        dup = runs_env / (p1.stem[:16] + "zzzz.json")
        dup.write_text(json.dumps(rec2))
        with pytest.raises(SystemExit, match="ambiguous"):
            _load_run(p1.stem[:16])

    def test_runs_empty_dir_is_friendly_exit_zero(self, runs_env, capsys):
        rc = _cmd_runs(Namespace(limit=20))
        assert rc == 0
        assert "no saved runs yet" in capsys.readouterr().out


# ── missing run id ────────────────────────────────────────────────────────

class TestMissingRunId:
    def test_show_unknown_id_exits_one_with_hint(self, runs_env, capsys):
        rc = _cmd_show(Namespace(run_id="nope"))
        out = capsys.readouterr().out
        assert rc == 1
        assert "no run matching 'nope'" in out
        assert "`callisto runs`" in out          # next step named

    def test_load_run_returns_none_for_no_match(self, runs_env):
        loaded, path = _load_run("ghost")
        assert loaded is None and path is None


# ── artifact re-hash mismatch reported, not swallowed ─────────────────────

class TestRehashHonesty:
    def test_corrupt_artifact_is_reported_as_CORRUPT(self, runs_env, capsys):
        rec = _result_record(_fake_result(), "q")
        path = _persist_run(rec)
        # put DIFFERENT bytes under the recorded hash's slot: simulate via
        # a store whose get returns wrong content.
        from tools.artifacts import ArtifactStore
        orig_get = ArtifactStore.get_bytes

        def bad_get(self, sha256):
            if sha256 == "a" * 64:
                return b"tampered"
            return orig_get(self, sha256)

        import tools.artifacts as art_mod
        art_mod.ArtifactStore.get_bytes = bad_get
        try:
            status = _verify_artifact("a" * 64)
            rc = _cmd_show(Namespace(run_id=path.stem))
        finally:
            art_mod.ArtifactStore.get_bytes = orig_get
        out = capsys.readouterr().out
        assert status == "CORRUPT"
        assert rc == 0                       # artifacts are soft-flagged…
        assert "CORRUPT" in out              # …but loudly, never swallowed

    def test_missing_artifact_reported_honestly(self, runs_env, capsys):
        rec = _result_record(_fake_result(), "q")
        path = _persist_run(rec)   # hash exists nowhere
        rc = _cmd_show(Namespace(run_id=path.stem))
        out = capsys.readouterr().out
        assert rc == 0
        assert "missing" in out or "unverifiable" in out

    def test_fetch_digest_mismatch_makes_show_exit_nonzero(
            self, runs_env, capsys):
        rec = _result_record(_fake_result(), "q")
        rec["fetches"].append({
            "source": "wiki", "url": "https://x/y",
            "content_sha256": hashlib.sha256(b"real").hexdigest(),
            "body": "tampered text",
        })
        path = _persist_run(rec)
        rc = _cmd_show(Namespace(run_id=path.stem))
        out = capsys.readouterr().out
        assert rc == 1
        assert "DIGEST MISMATCH" in out
        assert "WARNING" in out

    def test_fetch_digest_missing_is_hard_fail(self, runs_env, capsys):
        rec = _result_record(_fake_result(), "q")
        rec["fetches"][0] = {"source": "openalex",
                             "url": "https://api.openalex.org/x"}
        path = _persist_run(rec)
        rc = _cmd_show(Namespace(run_id=path.stem))
        out = capsys.readouterr().out
        assert rc == 1
        assert "MISSING DIGEST" in out

    def test_digest_status_matrix(self):
        good = hashlib.sha256(b"body").hexdigest()
        ok, hard = _fetch_digest_status(
            {"content_sha256": good, "body": "body"})
        assert ok == "ok" and hard is False
        for rec in ({}, {"content_sha256": ""}, {"content_sha256": None},
                    {"content_sha256": "z" * 64},
                    {"content_sha256": "abc"}):
            status, hard = _fetch_digest_status(rec)
            assert hard is True and status != "ok"
        # valid hex but no local payload -> soft unverified (legacy compat)
        status, hard = _fetch_digest_status({"content_sha256": good})
        assert hard is False and "unverified" in status


# ── seal key never printed ────────────────────────────────────────────────

class TestSealKeyNeverPrinted:
    KEY = "de" * 32      # distinctive key bytes to grep for

    def test_runs_and_show_output_never_contain_seal_key(
            self, runs_env, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", self.KEY)
        _persist_run(_result_record(_fake_result(), "q"))

        _cmd_runs(Namespace(limit=20))
        runs_out = capsys.readouterr().out
        assert self.KEY not in runs_out

        _cmd_show(Namespace(run_id="nope"))
        show_out = capsys.readouterr().out
        assert self.KEY not in show_out

    def test_persisted_records_do_not_embed_the_seal_key(self, runs_env):
        import os
        os.environ["CALLISTO_SEAL_KEY"] = self.KEY
        try:
            rec = _result_record(_fake_result(), "secret question")
            raw = json.dumps(rec)
            assert self.KEY not in raw
            assert "seal_key" not in raw
        finally:
            os.environ.pop("CALLISTO_SEAL_KEY", None)

    def test_ask_failure_output_does_not_leak_key(
            self, runs_env, tmp_path, monkeypatch, capsys):
        """Even on the refuse path (unset key mid-flight), no leak."""
        from callisto import check_seal_key
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        rc_ok = check_seal_key()
        out = capsys.readouterr().out + capsys.readouterr().err
        assert rc_ok is False
        assert self.KEY not in out
        assert "FAIL" in out                 # refusal explained without value
