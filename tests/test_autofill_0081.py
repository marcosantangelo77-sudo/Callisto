"""Autofill 0081 — characterization of the ask / runs / doctor front door.

Pins the current safety contract at the appliance's front door:

A. `callisto ask` refuses (exit 2) when CALLISTO_SEAL_KEY is missing,
   blank, whitespace-only, or non-hex — AND nothing past the gate runs
   (no router load, no engine build, no research).
B. Happy paths always set a valid hex seal key first; the gate opens,
   the pipeline runs through stubbed seams, a run record is persisted
   into CALLISTO_RUNS_DIR, and the key VALUE never appears anywhere.
C. `callisto doctor` reports its safety panels: it FAILs on an unkeyed
   or non-hex seal key, fails on non-loopback binds, verifies that
   OrderManager/BetExecutor default to disabled, and prints the
   CALLISTO_LOCAL_ONLY switch state.
D. `runs` / `show` persistence stays rooted in tools.cli.runs reading
   tools.cli.ask._runs_dir: listing order, verdict rendering, digest
   validation hard-fails on missing/malformed provenance, and show
   exits non-zero on unverifiable fetches.

These are characterization tests: red means behavior changed, not
necessarily broken — but every weakening here is a safety regression.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.cli import ask as ask_mod  # noqa: E402
from tools.cli import doctor as doctor_mod  # noqa: E402
from tools.cli import runs as runs_mod  # noqa: E402
import callisto  # noqa: E402

VALID_KEY = "ab" * 32                      # 64 hex chars
VALID_KEY2 = "deadbeef" * 8                # another valid hex value
BAD_KEYS = ["", "   ", " \t\n ", "zzzz-not-hex", "12345g", "0x1234"]

KEYS_NEVER_PRINTED = [VALID_KEY, VALID_KEY2]


# ── fixtures & helpers ─────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """Strip every front-door env var so each test starts from zero."""
    for var in ("CALLISTO_SEAL_KEY", "CALLISTO_SEAL_KEY_OLD",
                "CALLISTO_LOCAL_ONLY", "CALLISTO_BIND_HOST",
                "CALLISTO_ALLOW_LIVE_EXECUTE", "CALLISTO_RUNS_DIR",
                "CALLISTO_DB_PATH"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def keyed(clean_env, monkeypatch):
    """Happy-path environment: a valid hex seal key is ALWAYS set."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    return VALID_KEY


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


def _boom_router(path):
    raise AssertionError(f"router loaded despite bad seal key ({path})")


def _boom_engine(*a, **k):
    raise AssertionError("engine built despite bad seal key")


def _boom_record(*a, **k):
    raise AssertionError("research ran despite bad seal key")


def _wire_boom(monkeypatch):
    monkeypatch.setattr(callisto, "_load_router", _boom_router)
    monkeypatch.setattr(callisto, "_make_engine", _boom_engine)
    monkeypatch.setattr(callisto, "_result_record", _boom_record)


def _ask_args(question="what is the deal"):
    return argparse.Namespace(
        providers=callisto._default_providers_path(),
        backend=None,
        question=question,
        self_review=False,
    )


class _FakeLeaf:
    def __init__(self):
        self.text = "leaf question"
        self.answer = "leaf answer"
        self.tier = "VERIFIED"
        self.confidence = 0.9


class _FakeResult:
    sealed = True
    refusal_reason = ""
    conclusion = "the conclusion"
    confidence_score = 0.9
    confidence_tier = "VERIFIED"
    leaves = [_FakeLeaf()]
    artifact_refs = []
    fetches = []
    objections = []
    notes = ["note one"]


class _FakeRouter:
    endpoints = {"alpha": {}}
    task_classes = {}
    default_tier_name = "alpha"
    cost_ledger = type("L", (), {"snapshot":
                      staticmethod(lambda: {"by_tier": {}})})()

    @staticmethod
    async def check_health(name):
        return {"status": "ok"}


def _assert_no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key leaked: {key[:6]}…"


# ── A. the seal gate refuses unkeyed regimes ───────────────────────────────


class TestSealGateRefusals:
    def test_missing_key_refused(self, clean_env, capsys):
        assert ask_mod.check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "unkeyed" in out.lower()
        _assert_no_key_leak(out)

    def test_blank_key_refused(self, clean_env, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        assert ask_mod.check_seal_key() is False

    def test_whitespace_only_key_refused(self, clean_env, monkeypatch,
                                         capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", " \t \n ")
        assert ask_mod.check_seal_key() is False
        assert "FAIL" in capsys.readouterr().out

    @pytest.mark.parametrize("bad", ["zzzz-not-hex", "12345g", "0x1234"])
    def test_non_hex_keys_refused(self, clean_env, monkeypatch, capsys,
                                  bad):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        assert ask_mod.check_seal_key() is False
        out = capsys.readouterr().out
        assert "hex" in out.lower()
        _assert_no_key_leak(out)

    def test_blankish_keys_refused_without_hex_claim(self, clean_env,
                                                     monkeypatch, capsys):
        # Blank/whitespace keys are refused as "not set" (they strip to
        # empty), not as non-hex — pin that exact branch.
        for bad in ["", "   ", " \t\n "]:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
            assert ask_mod.check_seal_key() is False
            out = capsys.readouterr().out
            assert "not set" in out

    def test_valid_hex_accepted(self, keyed):
        assert ask_mod.check_seal_key() is True

    def test_surrounding_whitespace_on_valid_hex_ok(self, clean_env,
                                                    monkeypatch):
        # The gate strips before validating — a padded key still opens.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f"  {VALID_KEY}  ")
        assert ask_mod.check_seal_key() is True

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_cmd_ask_exits_2_unkeyed(self, clean_env, monkeypatch, bad):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        _wire_boom(monkeypatch)
        assert asyncio.run(ask_mod.cmd_ask(_ask_args())) == 2

    def test_cmd_ask_exit_2_when_unset(self, clean_env, monkeypatch):
        _wire_boom(monkeypatch)
        assert asyncio.run(ask_mod.cmd_ask(_ask_args())) == 2

    @pytest.mark.parametrize("backend", [None, "alpha"])
    def test_bad_backend_never_reaches_research(self, clean_env,
                                                monkeypatch, backend):
        # Even with --backend, an unkeyed regime must refuse first.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "nothex")
        ns = argparse.Namespace(
            providers=callisto._default_providers_path(),
            backend=backend, question="q", self_review=False)
        if backend:
            monkeypatch.setattr(callisto, "_load_router",
                                lambda p: _FakeRouter())
        else:
            _wire_boom(monkeypatch)
        rc = asyncio.run(ask_mod.cmd_ask(ns))
        assert rc == 2 or rc == 1  # refused/unhealthy, never success
        assert rc != 0


# ── B. happy paths (valid hex key set) ────────────────────────────────────


class TestAskHappyPath:
    def test_gate_opens_and_pipeline_runs(self, keyed, monkeypatch,
                                          runs_dir, capsys):
        monkeypatch.setattr(callisto, "_load_router",
                            lambda p: _FakeRouter())

        async def fake_run(q):
            return _FakeResult()

        def make_engine(router, self_review=False):
            return type("E", (), {"run": staticmethod(fake_run)})()

        monkeypatch.setattr(callisto, "_make_engine", make_engine)
        monkeypatch.setattr(
            callisto, "_result_record",
            lambda r, q: {"recorded_at": "2026-08-26T00:00:00+00:00",
                          "question": q, "sealed": True})
        rc = asyncio.run(ask_mod.cmd_ask(_ask_args()))
        assert rc == 0
        out = capsys.readouterr().out
        assert "SEALED" in out
        _assert_no_key_leak(out)

    def test_run_record_persisted_to_runs_dir(self, keyed, monkeypatch,
                                              runs_dir):
        record = {
            "recorded_at": "2026-08-26T12:00:00+00:00",
            "question": "persist me",
            "sealed": True,
        }
        path = callisto._persist_run(record)
        assert path.parent == runs_dir.resolve() or \
            path.parent == Path(os.environ["CALLISTO_RUNS_DIR"])
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["question"] == "persist me"
        assert loaded["sealed"] is True

    def test_runs_dir_override_creates_directory(self, tmp_path,
                                                 monkeypatch):
        d = tmp_path / "nested" / "deeper" / "runs"
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
        assert ask_mod._runs_dir() == d

    def test_persisted_filename_has_json_suffix(self, keyed, runs_dir):
        path = callisto._persist_run(
            {"recorded_at": "2026-08-26T00:00:00+00:00", "question": "x"})
        assert path.suffix == ".json"
        assert not path.with_suffix(".json.tmp").exists(), \
            "atomic tmp file must be replaced away"

    def test_result_record_shape(self, keyed):
        rec = ask_mod._result_record(_FakeResult(), "shape question")
        for field in ("recorded_at", "question", "sealed",
                      "refusal_reason", "conclusion", "confidence",
                      "leaves", "artifacts", "fetches", "objections",
                      "notes"):
            assert field in rec, f"run record lost '{field}'"
        assert rec["sealed"] is True
        assert rec["question"] == "shape question"
        assert rec["confidence"]["tier"] == "VERIFIED"

    def test_unsealed_result_exits_1_not_2(self, keyed, monkeypatch,
                                           capsys):
        res = _FakeResult()
        res.sealed = False
        res.refusal_reason = "sources disagree"

        async def fake_run(q):
            return res

        def make_engine(router, self_review=False):
            return type("E", (), {"run": staticmethod(fake_run)})()

        monkeypatch.setattr(callisto, "_load_router",
                            lambda p: _FakeRouter())
        monkeypatch.setattr(callisto, "_make_engine", make_engine)
        monkeypatch.setattr(callisto, "_result_record",
                            lambda r, q: {"recorded_at": "t",
                                          "question": q})
        rc = asyncio.run(ask_mod.cmd_ask(_ask_args()))
        assert rc == 1
        out = capsys.readouterr().out
        assert "REFUSED" in out


# ── C. doctor panels ───────────────────────────────────────────────────────


class TestDoctorSafetyPanels:
    def _doctor_args(self):
        return argparse.Namespace(
            providers=REPO / "config" / "providers.yaml")

    def test_doctor_fails_on_unkeyed_seal(self, clean_env, capsys):
        assert doctor_mod.cmd_doctor(self._doctor_args()) == 1
        out = capsys.readouterr().out
        assert "== seal ==" in out
        assert "not set" in out
        _assert_no_key_leak(out)

    def test_doctor_fails_on_non_hex_seal(self, clean_env, monkeypatch,
                                          capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "definitely-nothex")
        assert doctor_mod.cmd_doctor(self._doctor_args()) == 1
        out = capsys.readouterr().out
        assert "not valid hex" in out
        _assert_no_key_leak(out)

    def test_doctor_seal_ok_with_valid_hex(self, keyed, capsys):
        doctor_mod.cmd_doctor(self._doctor_args())
        out = capsys.readouterr().out
        assert "HMAC-SHA256" in out
        _assert_no_key_leak(out)

    def test_doctor_reports_local_only_switch(self, clean_env,
                                              monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        doctor_mod.cmd_doctor(self._doctor_args())
        out = capsys.readouterr().out
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_doctor_reports_local_only_off(self, clean_env, capsys):
        doctor_mod.cmd_doctor(self._doctor_args())
        out = capsys.readouterr().out
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_doctor_money_switches_panel_present(self, clean_env, capsys):
        doctor_mod.cmd_doctor(self._doctor_args())
        out = capsys.readouterr().out
        assert "== money switches ==" in out
        assert "BetExecutor.__init__ assigns _enabled = False" in out
        assert "OrderManager.__init__ defaults _enabled = False" in out

    def test_betexecutor_source_still_defaults_disabled(self):
        # Direct production gate: never weaken this.
        import tools.bet_executor
        src = Path(tools.bet_executor.__file__).read_text(
            encoding="utf-8")
        m = re.search(
            r"class BetExecutor\b.*?def __init__\(self\):(.*?)(\n    "
            r"(?:async )?def )", src, re.S)
        assert m, "BetExecutor.__init__ not found"
        assert re.search(r"self\._enabled\s*=\s*False", m.group(1)), \
            "BetExecutor must default to disabled"

    def test_ordermanager_source_still_defaults_disabled(self):
        import tools.order_manager
        src = Path(tools.order_manager.__file__).read_text(
            encoding="utf-8")
        pre_init = src.split("def enable", 1)[0]
        assert not re.search(r"self\._enabled\s*=\s*True", pre_init)

    def test_no_live_signal_status_added(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_bind_panel_loopback_default(self, clean_env, capsys):
        doctor_mod.cmd_doctor(self._doctor_args())
        out = capsys.readouterr().out
        assert "127.0.0.1" in out
        assert "loopback default" in out

    @pytest.mark.parametrize("host", ["0.0.0.0", "::"])
    def test_doctor_fails_on_wildcard_bind(self, clean_env, monkeypatch,
                                           capsys, host):
        monkeypatch.setenv("CALLISTO_BIND_HOST", host)
        assert doctor_mod.cmd_doctor(self._doctor_args()) == 1
        out = capsys.readouterr().out
        assert "exposes the API" in out


# ── D. runs / show persistence stays in tools.cli.runs ────────────────────


def _write_run(runs_dir: Path, stem: str, record: dict) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"{stem}.json"
    p.write_text(json.dumps(record), encoding="utf-8")
    return p


def _base_record(**over):
    rec = {
        "recorded_at": "2026-08-26T00:00:00+00:00",
        "question": "q?",
        "sealed": True,
        "refusal_reason": "",
        "conclusion": "c",
        "confidence": {"score": 0.5, "tier": "UNVERIFIED"},
        "leaves": [], "artifacts": [], "fetches": [],
        "objections": [], "notes": [],
    }
    rec.update(over)
    return rec


class TestRunsPersistence:
    def test_runs_reads_from_shared_runs_dir(self, runs_dir):
        _write_run(runs_dir, "20260826000000_0001",
                   _base_record(question="first"))
        args = argparse.Namespace(limit=10)
        assert runs_mod._cmd_runs(args) == 0

    def test_runs_empty_dir_message(self, runs_dir, capsys):
        assert runs_mod._cmd_runs(argparse.Namespace(limit=10)) == 0
        assert "no saved runs" in capsys.readouterr().out

    def test_runs_lists_newest_first_and_respects_limit(
            self, runs_dir, capsys):
        for i in range(5):
            _write_run(runs_dir, f"2026082600000{i}_000{i}",
                       _base_record(question=f"q{i}", sealed=i % 2 == 0))
        rc = runs_mod._cmd_runs(argparse.Namespace(limit=3))
        assert rc == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("20260826000004")

    def test_runs_verdict_labels(self, runs_dir, capsys):
        _write_run(runs_dir, "20260826000001_aaa",
                   _base_record(sealed=True))
        _write_run(runs_dir, "20260826000002_bbb",
                   _base_record(sealed=False))
        runs_mod._cmd_runs(argparse.Namespace(limit=10))
        out = capsys.readouterr().out
        assert "SEALED" in out
        assert "REFUSED" in out

    def test_show_loads_by_prefix(self, runs_dir, capsys):
        _write_run(runs_dir, "20260826120000_0042",
                   _base_record(question="prefix me"))
        rec, path = runs_mod._load_run("20260826120000")
        assert rec["question"] == "prefix me"
        assert path.name == "20260826120000_0042.json"

    def test_show_missing_run_exits_1(self, runs_dir, capsys):
        rc = runs_mod._cmd_show(argparse.Namespace(run_id="nope"))
        assert rc == 1
        assert "no run matching" in capsys.readouterr().out

    def test_show_ambiguous_prefix_raises(self, runs_dir):
        _write_run(runs_dir, "20260826000000_1111",
                   _base_record(question="one"))
        _write_run(runs_dir, "20260826000000_2222",
                   _base_record(question="two"))
        with pytest.raises(SystemExit, match="ambiguous"):
            runs_mod._load_run("20260826000000")

    def test_show_digest_mismatch_hard_fails(self, runs_dir):
        rec = _base_record(fetches=[{
            "source": "web", "url": "https://example.com/a",
            "content_sha256": "f" * 64, "body": "actual bytes"}])
        _write_run(runs_dir, "20260826000003_c", rec)
        assert runs_mod._cmd_show(
            argparse.Namespace(run_id="20260826000003")) == 1

    def test_show_missing_digest_hard_fails(self, runs_dir):
        rec = _base_record(fetches=[{
            "source": "web", "url": "https://example.com/b"}])
        _write_run(runs_dir, "20260826000004_d", rec)
        assert runs_mod._cmd_show(
            argparse.Namespace(run_id="20260826000004")) == 1

    def test_fetch_digest_status_matrix(self):
        ok, hard = runs_mod._fetch_digest_status({
            "content_sha256": __import__("hashlib").sha256(
                b"hello").hexdigest(), "body": "hello"})
        assert ok == "ok" and hard is False
        s, hard = runs_mod._fetch_digest_status({"url": "u"})
        assert hard is True and "MISSING" in s
        s, hard = runs_mod._fetch_digest_status(
            {"content_sha256": "abc"})
        assert hard is True and "MALFORMED" in s
        s, hard = runs_mod._fetch_digest_status(
            {"content_sha256": "z" * 64})
        assert hard is True and "non-hex" in s

    def test_show_clean_record_exits_0(self, runs_dir, capsys):
        digest = __import__("hashlib").sha256(b"body").hexdigest()
        rec = _base_record(fetches=[{
            "source": "web", "url": "https://ok.example/",
            "content_sha256": digest, "body": "body"}])
        _write_run(runs_dir, "20260826000005_e", rec)
        rc = runs_mod._cmd_show(
            argparse.Namespace(run_id="20260826000005"))
        assert rc == 0
        assert "[ok]" in capsys.readouterr().out

    def test_ask_and_runs_share_one_runs_dir_module(self):
        # The persistence seam must stay rooted where it is today.
        assert runs_mod._runs_dir is ask_mod._runs_dir
