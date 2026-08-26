"""tests/test_autofill_0057.py — autofill #0057 characterization.

Pins the ask / runs / doctor front door of the callisto CLI:

1. The seal gate. `callisto ask` refuses unkeyed CALLISTO_SEAL_KEY
   (unset / blank / whitespace / non-hex) with exit code 2 and no
   engine/router construction. Every happy path here installs a valid
   hex seal key first.
2. Doctor reports BetExecutor disabled (``_enabled = False``) and the
   CALLISTO_LOCAL_ONLY switch state.
3. runs/show persistence stays in tools.cli.runs.
4. The seal-key VALUE is never printed by any front-door code path.

Tests-only; production gates untouched. Never arm live betting.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402

VALID_KEY = "ab" * 32
OTHER_VALID_KEY = "c0ffee" * 10 + "ff"      # valid hex, 64 chars
SECRET_KEYS = [VALID_KEY, OTHER_VALID_KEY, "1234abcd" * 8]


# ── helpers ────────────────────────────────────────────────────────────────

def _args(q="0057 question", backend=None, self_review=False):
    return argparse.Namespace(
        providers=callisto._default_providers_path(),
        backend=backend,
        question=q,
        self_review=self_review,
    )


def _boom(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("post-gate seam ran despite unkeyed seal")


async def _aboom(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite unkeyed seal")


def _wire_boom(monkeypatch):
    monkeypatch.setattr(callisto, "_load_router", _boom)
    monkeypatch.setattr(callisto, "_make_engine", _boom)
    monkeypatch.setattr(callisto, "_result_record", _aboom)


def assert_no_key_leak(out: str) -> None:
    for key in SECRET_KEYS:
        assert key not in out, "seal key value leaked into output"


def _set_key(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    else:
        monkeypatch.setenv("CALLISTO_SEAL_KEY", value)


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


class _Ledger:
    def snapshot(self):
        return {"by_tier": {}}


class _Router:
    endpoints = ["gpu1"]
    task_classes = {"decompose": "gpu1"}
    default_tier_name = "gpu1"
    cost_ledger = _Ledger()

    async def check_health(self, name):
        return {"status": "ok"}


def _fake_pipeline(monkeypatch, reached, sealed=True):
    class _Engine:
        async def run(self, q):
            reached["question"] = q
            return NS(sealed=sealed,
                      refusal_reason="" if sealed else "stub refusal",
                      conclusion="c" if sealed else "",
                      confidence_score=0.5,
                      confidence_tier="SPECULATIVE",
                      leaves=[], fetches=[], objections=[],
                      notes=[], artifact_refs=[])

    router = _Router()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda r, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result, q: {
                            "recorded_at": "2026-08-26T00:00:00+00:00",
                            "question": q})
    return router


def _run_doctor(extra_env=None, providers=None, monkeypatch=None, capsys=None):
    for k, v in (extra_env or {}).items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    argv = ["doctor"]
    if providers:
        argv += ["--providers", providers]
    args = build_parser().parse_args(argv)
    rc = callisto._cmd_doctor(args)
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


# ══════════════════════════════════════════════════════════════════════════
# 1. seal gate
# ══════════════════════════════════════════════════════════════════════════

class TestSealGate0057:
    def test_unset_key_refused(self, monkeypatch, capsys):
        _set_key(monkeypatch, None)
        assert check_seal_key() is False
        assert "FAIL" in capsys.readouterr().out

    @pytest.mark.parametrize("bad", ["", "   ", " \t\n "])
    def test_blankish_keys_refused(self, bad, monkeypatch):
        _set_key(monkeypatch, bad)
        assert check_seal_key() is False

    @pytest.mark.parametrize("bad", [
        "zz" * 32, "not-hex-at-all", "banana",
        "0x" + "a" * 64, "ab" * 31 + "zz", "abc",
    ])
    def test_malformed_keys_refused(self, bad, monkeypatch):
        _set_key(monkeypatch, bad)
        assert check_seal_key() is False

    @pytest.mark.parametrize("good", [
        VALID_KEY, OTHER_VALID_KEY, "ABCDEF0123456789" * 4,
        f"\n {VALID_KEY}\t",
    ])
    def test_valid_keys_accepted(self, good, monkeypatch):
        _set_key(monkeypatch, good)
        assert check_seal_key() is True

    def test_gate_message_does_not_echo_bad_value(
            self, monkeypatch, capsys):
        bad = "f00d" * 16 + "!!"
        _set_key(monkeypatch, bad)
        check_seal_key()
        assert ("f00d" * 16) not in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════
# 2. ask fails closed on unkeyed seal — exit 2, no research
# ══════════════════════════════════════════════════════════════════════════

class TestAskFailsClosed0057:
    @pytest.fixture(autouse=True)
    def _isolated_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))

    @pytest.mark.parametrize("key_env", [None, "", "   ", "nothex",
                                         "zz" * 32, "abc"])
    def test_ask_exit_code_is_exactly_two_for_unkeyed(
            self, key_env, monkeypatch, capsys):
        _set_key(monkeypatch, key_env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        assert rc == 2
        assert "FAIL" in capsys.readouterr().out

    def test_no_partial_run_record_on_refusal(self, monkeypatch):
        _set_key(monkeypatch, None)
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_args()))
        assert list(callisto._runs_dir().glob("*.json")) == []

    @pytest.mark.parametrize("flag", ["self_review", "backend"])
    def test_flags_do_not_bypass_gate(self, flag, monkeypatch):
        kw = {"self_review": True} if flag == "self_review" \
            else {"backend": "gpu1"}
        _set_key(monkeypatch, "")
        _wire_boom(monkeypatch)
        assert asyncio.run(callisto._cmd_ask(_args(**kw))) == 2


# ══════════════════════════════════════════════════════════════════════════
# 3. happy path: valid hex key opens the gate
# ══════════════════════════════════════════════════════════════════════════

class TestAskHappyPath0057:
    def test_valid_key_runs_research_and_reports_sealed(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached)
        rc = asyncio.run(callisto._cmd_ask(_args("the question")))
        out = capsys.readouterr().out
        assert reached.get("question") == "the question"
        assert rc == 0
        assert "SEALED" in out
        assert_no_key_leak(out)

    def test_run_record_written_as_single_json(
            self, monkeypatch, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args("persist me")))
        saved = sorted(p.name for p in runs_isolated.glob("*.json"))
        assert len(saved) == 1
        assert re.match(r"^\d{8}T\d{6}[+\-]\d{4}_\d{4}\.json$", saved[0])

    def test_persisted_record_roundtrips_through_json(
            self, monkeypatch, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args("json roundtrip")))
        rec = json.loads(next(runs_isolated.glob("*.json")).read_text())
        assert rec["question"] == "json roundtrip"

    def test_refused_result_ran_but_exits_nonzero(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached, sealed=False)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert reached.get("question") == "0057 question"
        assert rc != 0
        assert "REFUSED" in out

    def test_unknown_backend_tier_refused_after_gate(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        rc = asyncio.run(
            callisto._cmd_ask(_args(backend="does-not-exist")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier 'does-not-exist'" in out


# ══════════════════════════════════════════════════════════════════════════
# 4. doctor: money switches (BetExecutor disabled + LOCAL_ONLY)
# ══════════════════════════════════════════════════════════════════════════

class TestDoctorMoneySwitches0057:
    def test_panels_present_when_keyed(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            providers=str(REPO / "config" / "providers.yaml"),
            monkeypatch=monkeypatch, capsys=capsys)
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "== money switches ==" in out
        assert rc == 0
        assert_no_key_leak(out)

    def test_betexecutor_disabled_reported_ok(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            providers=str(REPO / "config" / "providers.yaml"),
            monkeypatch=monkeypatch, capsys=capsys)
        assert "BetExecutor.__init__ assigns _enabled = False" in out

    def test_local_only_switch_state_shown_on(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY,
                       "CALLISTO_LOCAL_ONLY": "1"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_switch_state_shown_off(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY,
                       "CALLISTO_LOCAL_ONLY": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_betexecutor_source_pin_holds_right_now(self):
        src = (REPO / "tools" / "bet_executor.py").read_text(encoding="utf-8")
        assert "self._enabled = False" in src
        for forbidden in ("CALLISTO_ALLOW_LIVE_EXECUTE", "'live'",
                          '"live"', "status == 'live'"):
            assert forbidden not in src


# ══════════════════════════════════════════════════════════════════════════
# 5. doctor fails closed on unkeyed seal / bad bind
# ══════════════════════════════════════════════════════════════════════════

class TestDoctorUnkeyedFailsClosed0057:
    @pytest.mark.parametrize("key_env", [None, "", "   ", "banana"])
    def test_doctor_reports_problem(self, key_env, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": key_env},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "FAIL" in out
        assert "PROBLEMS FOUND" in out

    def test_nonhex_message_names_the_reason(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": "zz" * 32},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "not valid hex" in out
        assert_no_key_leak(out)

    def test_wildcard_bind_named_but_key_hidden(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_BIND_HOST": "0.0.0.0",
                       "CALLISTO_SEAL_KEY": OTHER_VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "0.0.0.0" in out
        assert_no_key_leak(out)

    def test_loopback_default_ok(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_BIND_HOST": None,
                       "CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "host: 127.0.0.1" in out

    def test_doctor_reports_config_problems_loudly(
            self, tmp_path, monkeypatch, capsys):
        rc, out = _run_doctor(
            providers=str(tmp_path / "missing.yaml"),
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "PROBLEMS FOUND" in out


# ══════════════════════════════════════════════════════════════════════════
# 6. runs / show — persistence lives in tools.cli.runs
# ══════════════════════════════════════════════════════════════════════════

from tools.cli.runs import (  # noqa: E402
    _cmd_runs,
    _cmd_show,
    _fetch_digest_status,
    _load_run,
    _verify_artifact,
)


def _write_run(runs_dir: Path, name: str, rec: dict) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"{name}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def _sample_rec(sealed=True, q="why do books shade lines?"):
    return {
        "recorded_at": "2026-08-26T01:02:03+0000",
        "question": q,
        "sealed": sealed,
        "refusal_reason": "" if sealed else "stub",
        "conclusion": "some conclusion" if sealed else "",
        "confidence": {"score": 0.42 if sealed else 0.0,
                       "tier": "LOW" if sealed else "NONE"},
        "leaves": [], "artifact_refs": [], "fetches": [],
        "objections": [], "notes": [],
    }


def _show_args(run_id="2026"):
    return build_parser().parse_args(["show", run_id])


class TestRunsPersistence0057:
    def test_empty_runs_dir_is_graceful_zero(self, runs_isolated, capsys):
        args = build_parser().parse_args(["runs"])
        assert _cmd_runs(args) == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_runs_lists_records_newest_first(self, runs_isolated, capsys):
        _write_run(runs_isolated, "20260101T000000+0000_aaaa",
                   _sample_rec(q="old"))
        _write_run(runs_isolated, "20260826T010203+0000_bbbb",
                   _sample_rec(q="new"))
        assert _cmd_runs(build_parser().parse_args(["runs"])) == 0
        out = capsys.readouterr().out
        assert out.index("bbbb") < out.index("aaaa")

    def test_runs_marks_sealed_and_refused(self, runs_isolated, capsys):
        _write_run(runs_isolated, "20260826T000001+0000_s1", _sample_rec(True))
        _write_run(runs_isolated, "20260826T000002+0000_r1", _sample_rec(False))
        _cmd_runs(build_parser().parse_args(["runs"]))
        out = capsys.readouterr().out
        assert "SEALED" in out and "REFUSED" in out

    def test_runs_limit_respected(self, runs_isolated, capsys):
        for i in range(5):
            _write_run(runs_isolated, f"2026082{i}T000000+0000_x{i}",
                       _sample_rec())
        _cmd_runs(build_parser().parse_args(["runs", "--limit", "2"]))
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_unreadable_record_reported_not_crashed(
            self, runs_isolated, capsys):
        runs_isolated.mkdir(parents=True, exist_ok=True)
        (runs_isolated / "20260826T000003+0000_bad.json").write_text("{nope")
        assert _cmd_runs(build_parser().parse_args(["runs"])) == 0
        assert "unreadable" in capsys.readouterr().out

    def test_show_reprints_a_saved_record(self, runs_isolated, capsys):
        _write_run(runs_isolated, "20260826T010203+0000_abcd",
                   _sample_rec(q="the shaded line question"))
        rc = _cmd_show(_show_args("2026"))
        assert rc == 0
        assert "the shaded line question" in capsys.readouterr().out

    def test_show_unknown_id_is_loud_nonzero(self, runs_isolated, capsys):
        rc = _cmd_show(_show_args("9999"))
        assert rc != 0
        assert "no run" in capsys.readouterr().out.lower()

    def test_load_run_ambiguous_prefix_raises_systemexit(self, runs_isolated):
        _write_run(runs_isolated, "20260826T010203+0000_one", _sample_rec())
        _write_run(runs_isolated, "20260827T010203+0000_two", _sample_rec())
        with pytest.raises(SystemExit) as ei:
            _load_run("2026")
        assert "ambiguous" in str(ei.value)

    def test_fetch_digest_status_ok_with_inline_body(self):
        payload = "openalex response body"
        digest = hashlib.sha256(payload.encode()).hexdigest()
        status, hard_fail = _fetch_digest_status(
            {"source_name": "openalex", "url": "u",
             "content_sha256": digest, "body": payload})
        assert status == "ok" and hard_fail is False

    def test_fetch_digest_status_flags_corruption(self):
        digest = hashlib.sha256(b"original body").hexdigest()
        status, hard_fail = _fetch_digest_status(
            {"source_name": "s", "url": "u",
             "content_sha256": digest, "body": "tampered body"})
        assert status == "DIGEST MISMATCH" and hard_fail is True

    def test_fetch_digest_status_missing_digest_hard_fails(self):
        status, hard_fail = _fetch_digest_status({"source_name": "s"})
        assert "MISSING DIGEST" in status and hard_fail is True

    def test_verify_artifact_reports_missing(self):
        status = _verify_artifact("e" * 64)
        assert status.startswith(("missing", "unverifiable"))

    def test_full_ask_to_runs_roundtrip_via_cli_module(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args("roundtrip via runs")))
        stem = next(runs_isolated.glob("*.json")).stem
        rc = _cmd_show(_show_args(stem[:8]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "roundtrip via runs" in out
        assert_no_key_leak(out)


# ══════════════════════════════════════════════════════════════════════════
# 7. parser surface & cross-cutting secrecy
# ══════════════════════════════════════════════════════════════════════════

class TestFrontDoorSurface0057:
    def test_runs_command_exists_in_parser(self):
        assert build_parser().parse_args(["runs", "--limit", "3"]).limit == 3

    def test_show_takes_run_id(self):
        assert _show_args("abc").run_id == "abc"

    def test_doctor_accepts_providers_flag(self):
        args = build_parser().parse_args(
            ["doctor", "--providers", "/tmp/p.yaml"])
        assert args.providers == "/tmp/p.yaml"

    def test_check_seal_key_reads_env_not_args(self, monkeypatch):
        _set_key(monkeypatch, VALID_KEY)
        assert check_seal_key() is True

    @pytest.mark.parametrize("secret", SECRET_KEYS)
    def test_ask_never_prints_valid_secret(self, secret, monkeypatch,
                                           capsys, runs_isolated):
        _set_key(monkeypatch, secret)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args()))
        assert secret not in capsys.readouterr().out

    def test_runs_module_is_tools_cli_runs(self):
        import tools.cli.runs as mod
        assert mod.__name__ == "tools.cli.runs"
