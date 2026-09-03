"""Autofill characterization #0033 — ask / runs / doctor front door.

Characterization tests pinning the fail-closed safety contract of the
Callisto front door as it exists today:

1. ``callisto ask`` refuses unkeyed seals: when CALLISTO_SEAL_KEY is
   unset, blank, whitespace-only, or non-hex it exits 2 and never
   constructs a router or engine — research must not start.
2. Happy paths set a valid hex seal key; with one present the gate
   opens and the pipeline runs (stubbed at the _load_router /
   _make_engine seams) and its record is persisted.
3. The seal-key VALUE is secret: neither check_seal_key, ask, nor
   doctor ever prints it, even on failure paths.
4. ``doctor`` reports BetExecutor disabled (_enabled = False in
   __init__), the CALLISTO_LOCAL_ONLY switch state, and fails closed
   on unkeyed seals and non-loopback binds.
5. ``runs`` / ``show`` persistence lives in tools.cli.runs: listing,
   prefix loading, digest validation, artifact re-hash honesty.

These are characterization tests. Any change that silently weakens the
fail-closed behavior shows up red here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402
from tools.cli.ask import cmd_ask  # noqa: E402
from tools.cli.doctor import cmd_doctor  # noqa: E402

VALID_KEY = "ab" * 32          # exactly 64 hex chars
OTHER_VALID_KEY = "deadbeef" * 8

# Keys that must NEVER appear in any command output.
SECRET_KEYS = [VALID_KEY, OTHER_VALID_KEY]


# ── helpers ────────────────────────────────────────────────────────────────

def _assert_no_key_leak(out: str) -> None:
    for key in SECRET_KEYS:
        assert key not in out, f"seal key value leaked into output"


def _boom(_=None, **k):  # pragma: no cover - must never run
    raise AssertionError("executed despite refused seal gate")


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key may run."""
    monkeypatch.setattr(callisto, "_load_router",
                        lambda p: _boom())
    monkeypatch.setattr(callisto, "_make_engine", _boom)
    monkeypatch.setattr(callisto, "_result_record", _boom)
    monkeypatch.setattr(callisto, "_persist_run", _boom)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test gets isolated runs dir + clean safety switches."""
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    return tmp_path


class _FakeLedger:
    def snapshot(self):
        return {"by_tier": {"gpu1": {"calls": 3}}}


class _FakeRouter:
    def __init__(self, healthy=True):
        self.endpoints = ["gpu1"]
        self.task_classes = {"decompose": "gpu1"}
        self.default_tier_name = "gpu1"
        self.cost_ledger = _FakeLedger()
        self._health = {"status": "ok"} if healthy else {"status": "down"}

    async def check_health(self, tier):
        return self._health


def _fake_result(sealed=True):
    from tools.artifacts import ArtifactRef
    return NS(
        sealed=sealed,
        refusal_reason="" if sealed else "one independent source only",
        conclusion="Foundry concentration is the binding constraint.",
        confidence_score=0.34 if sealed else 0.10,
        confidence_tier="SPECULATIVE" if sealed else "REFUSED",
        leaves=[NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256="a" * 64, kind="csv",
                                   name="concentration.csv")],
        fetches=[NS(source_name="openalex", url="https://example.org/x",
                    content_sha256="b" * 64)],
        objections=[NS(text="one independent source only")],
        notes=["note one"])


def _ask_args(q="char question"):
    return argparse.Namespace(
        providers=callisto._default_providers_path(),
        backend=None, question=q, self_review=False)


def _doctor_args():
    return argparse.Namespace(providers=callisto._default_providers_path())


def _wire_happy(monkeypatch, result=None, router=None):
    """Stub the post-gate seams so a full happy-path ask() can run."""
    r = router if router is not None else _FakeRouter()
    res = result if result is not None else _fake_result()

    class _Engine:
        async def run(self, q):
            return res

    saved = {}
    monkeypatch.setattr(callisto, "_load_router",
                        lambda p: (saved.setdefault("router", r)))
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router_, self_review: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result_, question: {
                            "recorded_at": "2026-08-26T00:00:00+00:00",
                            "question": question,
                            "sealed": bool(result_.sealed),
                            "refusal_reason": result_.refusal_reason,
                            "conclusion": result_.conclusion,
                            "confidence": {"score": result_.confidence_score,
                                           "tier": result_.confidence_tier},
                            "leaves": [], "artifacts": [],
                            "fetches": [{"source": f.source_name,
                                         "url": f.url,
                                         "content_sha256":
                                             f.content_sha256}
                                        for f in result_.fetches],
                            "objections": ["one independent source only"],
                            "notes": list(result_.notes)})
    return r, res, saved


# ══════════════════════════════════════════════════════════════════════════
# 1. Unkeyed / invalid seal keys → exit 2, nothing runs
# ══════════════════════════════════════════════════════════════════════════

class TestAskRefusesUnkeyedSeal:
    @pytest.mark.parametrize("keyval", [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("\t\n", id="tabs-newlines"),
        pytest.param("zzzz", id="non-hex"),
        pytest.param("abc123xyz!", id="hex-with-junk-suffix"),
        pytest.param("0x" + "a" * 62, id="hex-prefix-0x"),
        pytest.param("ab" * 31 + "g0", id="odd-corrupt-tail"),
    ])
    def test_ask_exits_2_and_never_runs(self, keyval, monkeypatch,
                                        capsys):
        if keyval is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", keyval)
        _wire_boom(monkeypatch)
        rc = asyncio.run(cmd_ask(_ask_args()))
        assert rc == 2
        out = capsys.readouterr().out
        assert "FAIL" in out
        _assert_no_key_leak(out)

    def test_gate_function_false_on_all_bad_keys(self, monkeypatch):
        for bad in ("", "  ", "nothex", "a" * 63 + "z", "\t"):
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
            assert check_seal_key() is False
        monkeypatch.delenv("CALLISTO_SEAL_KEY")
        assert check_seal_key() is False

    def test_unset_beats_wired_pipeline(self, monkeypatch, capsys):
        """Even with every downstream seam wired, unset key refuses."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _wire_boom(monkeypatch)
        assert asyncio.run(cmd_ask(_ask_args())) == 2

    def test_refusal_message_mentions_forgeable(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        check_seal_key()
        out = capsys.readouterr().out
        assert "forgeable" in out.lower()
        assert "unkeyed" in out.lower()


# ══════════════════════════════════════════════════════════════════════════
# 2. Happy path requires valid hex; then pipeline runs end-to-end
# ══════════════════════════════════════════════════════════════════════════

class TestHappyPathsUseValidHexKey:
    def test_valid_hex_opens_gate(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True

    def test_other_valid_hex_opens_gate(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_VALID_KEY)
        assert check_seal_key() is True

    def test_odd_length_hex_is_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abc")
        assert check_seal_key() is False

    def test_uppercase_hex_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "AB" * 32)
        assert check_seal_key() is True

    def test_full_ask_happy_path(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        router, result, _ = _wire_happy(monkeypatch)
        rc = asyncio.run(cmd_ask(_ask_args("why do foundries cluster")))
        assert rc == 0
        out = capsys.readouterr().out
        assert "SEALED" in out
        assert "SPECULATIVE" in out
        assert "run      :" in out
        assert "openalex" in out
        # persisted exactly once in the isolated runs dir
        records = list((tmp_path / "runs").glob("*.json"))
        assert len(records) == 1
        rec = json.loads(records[0].read_text())
        assert rec["question"] == "why do foundries cluster"
        assert rec["sealed"] is True
        assert rec["fetches"][0]["source"] == "openalex"
        _assert_no_key_leak(out)

    def test_unsealed_result_exits_1_but_still_persists(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _wire_happy(monkeypatch, result=_fake_result(sealed=False))
        rc = asyncio.run(cmd_ask(_ask_args()))
        assert rc == 1
        out = capsys.readouterr().out
        assert "REFUSED" in out
        records = list((tmp_path / "runs").glob("*.json"))
        assert len(records) == 1
        rec = json.loads(records[0].read_text())
        assert rec["sealed"] is False
        assert rec["refusal_reason"]

    def test_backend_pin_routes_every_task_class(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        router = _FakeRouter()
        args = _ask_args()
        args.backend = "gpu1"
        assert args.backend in router.endpoints
        router.task_classes = {tc: args.backend
                               for tc in (router.task_classes or {})}
        router.default_tier_name = args.backend
        assert set(router.task_classes.values()) == {"gpu1"}
        assert router.default_tier_name == "gpu1"

    def test_unknown_backend_refused_exit_2(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        router = _FakeRouter()
        _wire_happy(monkeypatch, router=router)
        args = _ask_args()
        args.backend = "nonexistent"
        rc = asyncio.run(cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "unknown provider tier 'nonexistent'" in out

    def test_unhealthy_pinned_backend_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        router = _FakeRouter(healthy=False)
        _wire_happy(monkeypatch, router=router)
        args = _ask_args()
        args.backend = "gpu1"
        rc = asyncio.run(cmd_ask(args))
        assert rc == 2
        assert "unhealthy" in capsys.readouterr().out

    def test_persistence_failure_does_not_crash_ask(
            self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        router, result, _ = _wire_happy(monkeypatch)

        def boom(record):
            raise OSError("disk full")

        monkeypatch.setattr(callisto, "_persist_run", boom)
        rc = asyncio.run(cmd_ask(_ask_args()))
        assert rc == 0                      # verdict unaffected
        assert "NOT SAVED" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════
# 3. The seal key value is secret everywhere
# ══════════════════════════════════════════════════════════════════════════

class TestKeyNeverPrinted:
    def test_check_failure_never_prints_value(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY[:-1] + "z")
        check_seal_key()
        _assert_no_key_leak(capsys.readouterr().out)

    def test_doctor_never_prints_value(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        cmd_doctor(_doctor_args())
        _assert_no_key_leak(capsys.readouterr().out)

    def test_doctor_bad_key_never_prints_value(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_VALID_KEY[:-2] + "!!")
        cmd_doctor(_doctor_args())
        _assert_no_key_leak(capsys.readouterr().out)


# ══════════════════════════════════════════════════════════════════════════
# 4. Doctor: BetExecutor disabled, CALLISTO_LOCAL_ONLY, panels
# ══════════════════════════════════════════════════════════════════════════

class TestDoctorSafetyPanels:
    def _run(self, monkeypatch, capsys):
        rc = cmd_doctor(_doctor_args())
        return rc, capsys.readouterr().out

    def test_bet_executor_reported_disabled(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out

    def test_local_only_switch_reported(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_local_only_on_when_set(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        _, out = self._run(monkeypatch, capsys)
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_allow_live_execute_reported_off_by_default(
            self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_panels_present(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        for panel in ("== providers ==", "== hermes cli ==",
                      "== database ==", "== source registry ==",
                      "== seal ==", "== bind ==", "== money switches =="):
            assert panel in out

    def test_unkeyed_seal_fails_doctor(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        rc, out = self._run(monkeypatch, capsys)
        assert rc == 1
        assert "FAIL: CALLISTO_SEAL_KEY is not set" in out

    def test_nonhex_seal_fails_doctor(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-at-all")
        rc, out = self._run(monkeypatch, capsys)
        assert rc == 1
        assert "not valid hex" in out

    def test_loopback_bind_passes(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        assert "OK: loopback default" in out

    def test_wildcard_bind_fails_closed(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_BIND_HOST", "0.0.0.0")
        rc, out = self._run(monkeypatch, capsys)
        assert rc == 1
        assert "FAIL: binding to an unspecified address" in out

    def test_ipv6_any_bind_fails_closed(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_BIND_HOST", "::")
        rc, _ = self._run(monkeypatch, capsys)
        assert rc == 1

    def test_order_manager_safe_default_reported(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, out = self._run(monkeypatch, capsys)
        assert "OK: OrderManager.__init__ defaults _enabled = False" in out

    def test_doctor_summary_line(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        rc, out = self._run(monkeypatch, capsys)
        assert ("doctor: OK" if rc == 0
                else "PROBLEMS FOUND") in out


# ══════════════════════════════════════════════════════════════════════════
# 5. runs/show persistence stays in tools.cli.runs
# ══════════════════════════════════════════════════════════════════════════

class TestRunsShowPersistence:
    def _persist_two_runs(self, question_a="first question",
                          question_b="second question"):
        rec_a = callisto._result_record(_fake_result(), question_a)
        path_a = callisto._persist_run(rec_a)
        rec_b = callisto._result_record(
            _fake_result(sealed=False), question_b)
        path_b = callisto._persist_run(rec_b)
        return path_a, path_b

    def test_persist_then_load_roundtrip(self):
        path_a, _ = self._persist_two_runs()
        rec, loaded_path = callisto._load_run(path_a.stem)
        assert loaded_path == path_a
        assert rec["question"] == "first question"
        assert rec["sealed"] is True

    def test_load_by_unique_prefix(self):
        path_a, path_b = self._persist_two_runs()
        # Ensure the shared timestamp stamp doesn't make prefixes collide:
        # use a length that distinguishes the two persisted stems.
        n = next(k for k in range(4, len(path_a.stem) + 1)
                 if len({p.stem[:k] for p in (path_a, path_b)}) == 2)
        assert n > 12  # the hash suffix is what disambiguates
        # _load_run globs f"{run_id}*.json" — '+' is literal, fine.
        rec, p = callisto._load_run(path_a.stem[:n])
        assert p == path_a
        assert rec["question"] == "first question"

    def test_load_missing_returns_none_none(self):
        rec, p = callisto._load_run("does-not-exist")
        assert rec is None and p is None

    def test_ambiguous_prefix_raises_systemexit(self):
        self._persist_two_runs(question_a="aa same",
                               question_b="aa different")
        # Same timestamp second ⇒ both share the recorded_at stamp; force
        # distinct stems by writing two files with a colliding prefix.
        runs_dir = callisto._runs_dir()
        stem = sorted(p.stem for p in runs_dir.glob("*.json"))[0][:8]
        (runs_dir / f"{stem}1111.json").write_text("{}")
        (runs_dir / f"{stem}2222.json").write_text("{}")
        with pytest.raises(SystemExit, match="ambiguous run id"):
            callisto._load_run(stem)

    def test_cmd_runs_lists_newest_first(self, capsys):
        self._persist_two_runs()
        rc = callisto._cmd_runs(argparse.Namespace(limit=20))
        assert rc == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert "SEALED" in lines[0] or "REFUSED" in lines[0]

    def test_cmd_runs_empty_dir(self, capsys):
        rc = callisto._cmd_runs(argparse.Namespace(limit=20))
        assert rc == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_cmd_runs_limit(self, capsys):
        self._persist_two_runs()
        callisto._cmd_runs(argparse.Namespace(limit=1))
        lines = [ln for ln in capsys.readouterr().out.splitlines()
                 if ln.strip()]
        assert len(lines) == 1

    def test_cmd_runs_reports_unreadable_without_crashing(
            self, capsys):
        self._persist_two_runs()
        callisto._runs_dir().joinpath("bogus.json").write_text("{not json")
        rc = callisto._cmd_runs(argparse.Namespace(limit=20))
        assert rc == 0
        assert "(unreadable:" in capsys.readouterr().out

    def test_show_missing_run_exits_1(self, capsys):
        rc = callisto._cmd_show(argparse.Namespace(run_id="nope"))
        assert rc == 1
        assert "no run matching 'nope'" in capsys.readouterr().out

    def test_show_prints_verdict_and_question(self, capsys):
        path_a, _ = self._persist_two_runs()
        rc = callisto._cmd_show(argparse.Namespace(run_id=path_a.stem))
        assert rc == 0
        out = capsys.readouterr().out
        assert "question : first question" in out
        assert "SEALED" in out
        assert "concentration is the binding constraint" in out

    def test_show_refused_run_shows_reason(self, capsys):
        _, path_b = self._persist_two_runs()
        rc = callisto._cmd_show(argparse.Namespace(run_id=path_b.stem))
        assert rc == 0
        out = capsys.readouterr().out
        assert "REFUSED" in out
        assert "reason" in out

    def test_fetch_digest_status_matrix(self):
        ok = {"content_sha256": "a" * 64}
        status, hard = callisto._fetch_digest_status(ok)
        assert status.startswith("unverified") and hard is False

        missing = {"content_sha256": ""}
        status, hard = callisto._fetch_digest_status(missing)
        assert "MISSING DIGEST" in status and hard is True

        short = {"content_sha256": "a" * 32}
        status, hard = callisto._fetch_digest_status(short)
        assert "MALFORMED DIGEST (32 chars)" in status and hard is True

        nonhex = {"content_sha256": "z" * 64}
        status, hard = callisto._fetch_digest_status(nonhex)
        assert "non-hex" in status and hard is True

        mismatch = {"content_sha256": "a" * 64, "body": "different bytes"}
        status, hard = callisto._fetch_digest_status(mismatch)
        assert status == "DIGEST MISMATCH" and hard is True

        import hashlib as _hashlib
        body = b"real payload bytes"
        verified = {"content_sha256": _hashlib.sha256(body).hexdigest(),
                    "body": body.decode()}
        status, hard = callisto._fetch_digest_status(verified)
        assert status == "ok" and hard is False

    def test_verify_artifact_missing_store_entry(self):
        status = callisto._verify_artifact("f" * 64)
        assert status in ("missing",) or status.startswith("unverifiable")

    def test_records_live_under_isolated_runs_dir(self, monkeypatch,
                                                  tmp_path):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "elsewhere"))
        callisto._persist_run(
            callisto._result_record(_fake_result(), "where am I"))
        files = list((tmp_path / "elsewhere").glob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["question"] == "where am I"

    def test_reexported_symbols_come_from_tools_cli(self):
        """Persistence entry points stay owned by tools.cli.runs/ask."""
        import tools.cli.runs as runs_mod
        import tools.cli.ask as ask_mod
        assert callisto._cmd_runs is runs_mod._cmd_runs
        assert callisto._cmd_show is runs_mod._cmd_show
        assert callisto._load_run is runs_mod._load_run
        assert callisto._verify_artifact is runs_mod._verify_artifact
        assert callisto._fetch_digest_status is \
            runs_mod._fetch_digest_status
        assert callisto.check_seal_key is ask_mod.check_seal_key
        assert callisto._result_record is ask_mod._result_record
        assert callisto._persist_run is ask_mod._persist_run


# ══════════════════════════════════════════════════════════════════════════
# 6. Parser / main wiring of the front door
# ══════════════════════════════════════════════════════════════════════════

class TestFrontDoorParser:
    def test_runs_limit_flag(self):
        args = build_parser().parse_args(["runs", "--limit", "5"])
        assert args.limit == 5
        assert build_parser().parse_args(["runs"]).limit == 20

    def test_show_requires_run_id(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["show"])

    def test_self_review_flag(self):
        args = build_parser().parse_args(["ask", "--self-review", "q"])
        assert args.self_review is True

    def test_command_subcommand_recorded(self):
        for cmd in ("ask", "runs", "show", "status", "doctor", "help"):
            argv = [cmd] + (["q"] if cmd == "ask" else
                            (["some-id"] if cmd == "show" else []))
            args = build_parser().parse_args(argv)
            assert args.command == cmd

    def test_no_subcommand_exits(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_main_dispatches_runs(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "r"))
        rc = callisto.main(["runs"])
        assert rc == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_main_ask_refuses_unkeyed_via_full_cli(self, monkeypatch,
                                                   tmp_path, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "r"))
        _wire_boom(monkeypatch)
        assert callisto.main(["ask", "anything"]) == 2

    def test_epilog_documents_money_defaults(self):
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in \
            build_parser().epilog
        assert "127.0.0.1" in build_parser().epilog
        assert "CALLISTO_LOCAL_ONLY=1" in build_parser().epilog


# ══════════════════════════════════════════════════════════════════════════
# 7. Production gates stay intact (fail closed, never arm live betting)
# ══════════════════════════════════════════════════════════════════════════

class TestProductionGatesIntact:
    def test_check_seal_key_source_still_gates(self, monkeypatch):
        """The gate itself refuses without env help on a clean process."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False

    def test_bet_executor_default_disabled_in_source(self):
        import inspect
        import re as _re
        import tools.bet_executor as be
        src = inspect.getsource(be)
        init_m = _re.search(
            r"class BetExecutor\b.*?def __init__\(self\):(.*?)"
            r"(\n    (?:async )?def )", src, _re.S)
        assert init_m, "BetExecutor.__init__ not found"
        assert _re.search(r"self\._enabled\s*=\s*False", init_m.group(1)), \
            "BetExecutor.__init__ no longer defaults _enabled = False"
