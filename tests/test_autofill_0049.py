"""Autofill #0049 — characterization of the ask / runs / doctor front door.

This module pins the *current* fail-closed safety contract of the callisto
CLI surface so any future change that silently weakens it turns a test red:

1. The seal gate. `callisto ask` refuses to run ANY research when
   CALLISTO_SEAL_KEY is unset, blank, whitespace-only, or non-hex — with
   exit code 2 and no engine/router construction. Happy paths in this file
   always install a valid 64-hex-char key first.
2. Doctor's money-switch panel. `callisto doctor` must report that
   BetExecutor.__init__ assigns ``_enabled = False`` and must show the
   CALLISTO_LOCAL_ONLY switch state. If the source ever stops assigning
   the disabled default, doctor says FAIL and this suite stays red.
3. runs/show persistence lives in tools.cli.runs: ask persists a run
   record under an isolated CALLISTO_RUNS_DIR; runs lists it newest-first;
   show re-verifies artifact hashes and fetch digests loudly.
4. Secrecy invariant: no code path prints the seal-key VALUE.

These are tests-only characterizations; production gates are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3  # noqa: F401  (kept for parity with sibling suites)
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402

VALID_KEY = "ab" * 32                     # exactly 64 hex chars
OTHER_VALID_KEY = "deadbeef" * 8          # another valid hex value

# Every string that must NEVER appear in captured CLI output.
SECRET_KEYS = [VALID_KEY, OTHER_VALID_KEY, "1234abcd" * 8, "f00d" * 16]


# ── helpers ────────────────────────────────────────────────────────────────

def _args(q="0049 question", backend=None, self_review=False):
    return argparse.Namespace(
        providers=callisto._default_providers_path(),
        backend=backend,
        question=q,
        self_review=self_review,
    )


def _boom_engine(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("engine built despite unkeyed seal")


async def _boom_research(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite unkeyed seal")


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may
    execute when the gate refuses."""
    monkeypatch.setattr(
        callisto, "_load_router",
        lambda p: (_ for _ in ()).throw(
            AssertionError("router loaded despite bad seal")))
    monkeypatch.setattr(callisto, "_make_engine", _boom_engine)
    monkeypatch.setattr(callisto, "_result_record", _boom_research)


def assert_no_key_leak(out: str) -> None:
    for key in SECRET_KEYS:
        assert key not in out, f"seal key value leaked into output"


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


def _set_key(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    else:
        monkeypatch.setenv("CALLISTO_SEAL_KEY", value)


def _fake_pipeline(monkeypatch, reached, sealed=True):
    """Stub the post-gate seams so a keyed ask reaches a fake engine."""

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
# 1. The seal gate itself
# ══════════════════════════════════════════════════════════════════════════

class TestSealGate0049:
    def test_unset_key_refused(self, monkeypatch, capsys):
        _set_key(monkeypatch, None)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "unkeyed" in out.lower()

    @pytest.mark.parametrize("bad", [
        "",                    # blank
        "   ",                 # whitespace only
        " \t\n ",              # mixed whitespace
    ])
    def test_blankish_keys_refused(self, bad, monkeypatch, capsys):
        _set_key(monkeypatch, bad)
        assert check_seal_key() is False

    @pytest.mark.parametrize("bad", [
        "zz" * 32,             # right length, not hex
        "not-hex-at-all",
        "banana",
        "0x" + "a" * 64,       # hex-literal prefix is not hex digits
        "ab" * 31 + "zz",      # hex prefix, junk tail
        "abc",                 # odd length
    ])
    def test_malformed_keys_refused(self, bad, monkeypatch):
        _set_key(monkeypatch, bad)
        assert check_seal_key() is False

    def test_extra_long_hex_tolerated(self, monkeypatch):
        """Characterization: the gate validates hex-ness and does not pin
        an exact length beyond requiring valid hex."""
        _set_key(monkeypatch, "ab" * 40)
        assert check_seal_key() is True

    def test_uppercase_hex_accepted(self, monkeypatch):
        _set_key(monkeypatch, "ABCDEF0123456789" * 4)
        assert check_seal_key() is True

    def test_surrounding_whitespace_stripped_then_ok(self, monkeypatch):
        _set_key(monkeypatch, f"\n {VALID_KEY}\t")
        assert check_seal_key() is True

    @pytest.mark.parametrize("good", [VALID_KEY, OTHER_VALID_KEY,
                                      "0123456789abcdef" * 4])
    def test_valid_keys_accepted(self, good, monkeypatch):
        _set_key(monkeypatch, good)
        assert check_seal_key() is True


# ══════════════════════════════════════════════════════════════════════════
# 2. ask fails closed before any research starts
# ══════════════════════════════════════════════════════════════════════════

class TestAskFailsClosed0049:
    @pytest.fixture(autouse=True)
    def _isolated_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
        return tmp_path / "runs"

    @pytest.mark.parametrize("key_env", [None, "", "   ", "nothex",
                                         "zz" * 32, "abc"])
    def test_ask_never_starts_research_without_key(
            self, key_env, monkeypatch, capsys):
        _set_key(monkeypatch, key_env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert rc != 0
        assert "FAIL" in out
        # No partial run record may exist either.
        assert list(callisto._runs_dir().glob("*.json")) == []

    @pytest.mark.parametrize("key_env", [None, "", "not-hex", "zz" * 32])
    def test_ask_exit_code_is_exactly_two_for_unkeyed(
            self, key_env, monkeypatch, capsys):
        """Pin the exact code so callers can distinguish refusal from crash."""
        _set_key(monkeypatch, key_env)
        _wire_boom(monkeypatch)
        assert asyncio.run(callisto._cmd_ask(_args())) == 2

    def test_unkeyed_refusal_prints_no_attempted_value(
            self, monkeypatch, capsys):
        leaked = "f00d" * 16 + "!!"     # invalid hex containing a secret-ish run
        _set_key(monkeypatch, leaked)
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert "f00d" * 16 not in out
        assert_no_key_leak(out)

    def test_self_review_flag_does_not_bypass_gate(
            self, monkeypatch, capsys):
        _set_key(monkeypatch, None)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args(self_review=True)))
        assert rc == 2

    def test_backend_flag_does_not_bypass_gate(self, monkeypatch, capsys):
        _set_key(monkeypatch, "")
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args(backend="gpu1")))
        assert rc == 2

    def test_runs_dir_not_even_created_on_refusal(
            self, tmp_path, monkeypatch, capsys):
        rd = tmp_path / "never"
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(rd))
        _set_key(monkeypatch, None)
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_args()))
        assert not list(rd.glob("*"))


# ══════════════════════════════════════════════════════════════════════════
# 3. happy path: valid hex key opens the gate
# ══════════════════════════════════════════════════════════════════════════

class TestAskHappyPathKeyed0049:
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
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args("persist me")))
        saved = sorted(p.name for p in runs_isolated.glob("*.json"))
        assert len(saved) == 1
        assert re.match(r"^\d{8}T\d{6}[+\-]\d{4}_\d{4}\.json$", saved[0])

    def test_persisted_record_roundtrips_through_json(
            self, monkeypatch, capsys, runs_isolated):
        from tools.artifacts import ArtifactRef
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        asyncio.run(callisto._cmd_ask(_args("json roundtrip")))
        rec = json.loads(next(runs_isolated.glob("*.json")).read_text())
        assert rec["question"] == "json roundtrip"
        assert rec["recorded_at"].endswith("+00:00")
        assert ArtifactRef  # imported for shape parity

    def test_refused_result_ran_but_exits_nonzero(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached, sealed=False)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert reached.get("question") == "0049 question"
        assert rc != 0
        assert "REFUSED" in out

    def test_unknown_backend_tier_refused_after_gate(
            self, monkeypatch, capsys, runs_isolated):
        _set_key(monkeypatch, VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        rc = asyncio.run(callisto._cmd_ask(_args(backend="does-not-exist")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier 'does-not-exist'" in out
        assert_no_key_leak(out)


# ══════════════════════════════════════════════════════════════════════════
# 4. doctor: money switches panel (BetExecutor + LOCAL_ONLY)
# ══════════════════════════════════════════════════════════════════════════

class TestDoctorMoneySwitches0049:
    def test_panels_present_when_keyed(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            providers=str(REPO / "config" / "providers.yaml"),
            monkeypatch=monkeypatch, capsys=capsys)
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "== money switches ==" in out
        assert "HMAC-SHA256" in out
        assert rc == 0
        assert "doctor: OK" in out
        assert_no_key_leak(out)

    def test_betexecutor_disabled_reported_ok(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            providers=str(REPO / "config" / "providers.yaml"),
            monkeypatch=monkeypatch, capsys=capsys)
        assert "BetExecutor.__init__ assigns _enabled = False" in out
        assert "OrderManager.__init__ defaults _enabled = False" in out

    def test_local_only_switch_state_shown_on(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_LOCAL_ONLY": "1"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_switch_state_shown_off(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_LOCAL_ONLY": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_live_execute_switch_off_by_default(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_ALLOW_LIVE_EXECUTE": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_betexecutor_source_pin_holds_right_now(self):
        """Direct source-level pin (independent of doctor): the shipped
        BetExecutor still defaults to disabled. If this goes red someone
        armed real execution — fail closed, never arm live betting."""
        src = (REPO / "tools" / "bet_executor.py").read_text(encoding="utf-8")
        assert "self._enabled = False" in src
        # No live-arm switch anywhere in the executor source.
        for forbidden in ("CALLISTO_ALLOW_LIVE_EXECUTE", '"live"',
                          "'live'", "status == 'live'"):
            assert forbidden not in src

    def test_doctor_reports_config_problems_loudly(
            self, tmp_path, monkeypatch, capsys):
        rc, out = _run_doctor(
            providers=str(tmp_path / "missing.yaml"),
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "PROBLEMS FOUND" in out
        assert "config unreadable" in out

    def test_doctor_never_echoes_secret_even_when_set(
            self, monkeypatch, capsys):
        secret = SECRET_KEYS[2]
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": secret},
            monkeypatch=monkeypatch, capsys=capsys)
        assert secret not in out
        assert "seal key is set" in out


class TestDoctorUnkeyedFailsClosed0049:
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
        assert "loopback default" in out


# ══════════════════════════════════════════════════════════════════════════
# 5. runs / show — persistence lives in tools.cli.runs
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
        "leaves": [],
        "artifact_refs": [],
        "fetches": [],
        "objections": [],
        "notes": [],
    }


def _show_args(run_id="2026"):
    return build_parser().parse_args(["show", run_id])


class TestRunsPersistence0049:
    def test_empty_runs_dir_is_graceful_zero(
            self, runs_isolated, capsys):
        args = build_parser().parse_args(["runs"])
        assert _cmd_runs(args) == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_runs_lists_records_newest_first(
            self, runs_isolated, capsys):
        _write_run(runs_isolated, "20260101T000000+0000_aaaa",
                   _sample_rec(q="old"))
        _write_run(runs_isolated, "20260826T010203+0000_bbbb",
                   _sample_rec(q="new"))
        args = build_parser().parse_args(["runs"])
        assert _cmd_runs(args) == 0
        out = capsys.readouterr().out
        assert out.index("bbbb") < out.index("aaaa")

    def test_runs_marks_sealed_and_refused(
            self, runs_isolated, capsys):
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
        out_lines = [ln for ln in capsys.readouterr().out.splitlines()
                     if ln.strip()]
        assert len(out_lines) == 2

    def test_unreadable_record_reported_not_crashed(
            self, runs_isolated, capsys):
        runs_isolated.mkdir(parents=True, exist_ok=True)
        (runs_isolated / "20260826T000003+0000_bad.json").write_text("{nope")
        args = build_parser().parse_args(["runs"])
        assert _cmd_runs(args) == 0
        assert "unreadable" in capsys.readouterr().out

    def test_show_reprints_a_saved_record(self, runs_isolated, capsys):
        _write_run(runs_isolated, "20260826T010203+0000_abcd",
                   _sample_rec(q="the shaded line question"))
        rc = _cmd_show(_show_args("2026"))
        out = capsys.readouterr().out
        assert rc == 0
        assert "the shaded line question" in out

    def test_show_unknown_id_is_loud_nonzero(self, runs_isolated, capsys):
        rc = _cmd_show(_show_args("9999"))
        assert rc != 0
        assert "no run" in capsys.readouterr().out.lower()

    def test_load_run_prefix_match_unique(self, runs_isolated):
        _write_run(runs_isolated, "20260826T010203+0000_xyz",
                   _sample_rec())
        rec, path = _load_run("2026")
        assert rec is not None and path is not None
        assert rec["question"]

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
            {"source_name": "openalex",
             "url": "https://api.openalex.org/x",
             "content_sha256": digest, "body": payload})
        assert status == "ok" and hard_fail is False

    def test_fetch_digest_status_flags_corruption(self):
        digest = hashlib.sha256(b"original body").hexdigest()
        status, hard_fail = _fetch_digest_status(
            {"source_name": "s", "url": "u",
             "content_sha256": digest, "body": "tampered body"})
        assert status == "DIGEST MISMATCH" and hard_fail is True

    def test_fetch_digest_status_missing_digest_hard_fails(self):
        status, hard_fail = _fetch_digest_status(
            {"source_name": "s", "url": "u"})
        assert "MISSING DIGEST" in status and hard_fail is True

    def test_fetch_digest_status_malformed_digests_hard_fail(self):
        for bad in ("", "short", "z" * 64, 12345):
            status, hard_fail = _fetch_digest_status({"content_sha256": bad})
            assert hard_fail is True

    def test_fetch_digest_status_unverified_without_payload_is_soft(self):
        status, hard_fail = _fetch_digest_status(
            {"source_name": "s", "url": "u",
             "content_sha256": "a" * 64})
        assert "unverified" in status and hard_fail is False

    def test_verify_artifact_reports_missing(self):
        status = _verify_artifact("e" * 64)
        assert status in ("missing", "unverifiable") or \
               status.startswith("unverifiable")

    def test_full_ask_to_runs_roundtrip_via_cli_module(
            self, monkeypatch, capsys, runs_isolated):
        """The product loop end-to-end with seams stubbed: keyed ask writes
        a record; tools.cli.runs reads it back by prefix."""
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
# 6. cross-cutting: parser surface & secrecy
# ══════════════════════════════════════════════════════════════════════════

class TestFrontDoorSurface0049:
    def test_runs_command_exists_in_parser(self):
        args = build_parser().parse_args(["runs", "--limit", "3"])
        assert args.limit == 3

    def test_show_takes_run_id(self):
        assert _show_args("abc").run_id == "abc"

    def test_doctor_accepts_providers_flag(self):
        args = build_parser().parse_args(
            ["doctor", "--providers", "/tmp/p.yaml"])
        assert args.providers == "/tmp/p.yaml"

    def test_check_seal_key_reads_env_not_args(self, monkeypatch, capsys):
        _set_key(monkeypatch, VALID_KEY)
        assert check_seal_key() is True
        _set_key(monkeypatch, None)
        assert check_seal_key() is False
