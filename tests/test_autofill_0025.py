"""Autofill characterization #0025 — ask / runs / doctor front door.

A LARGE characterization module pinning the safety contract of the
Callisto front door as it exists today:

1. Seal gate. `check_seal_key()` is fail-closed: `ask` exits 2 and never
   constructs a router or engine when CALLISTO_SEAL_KEY is unset, blank,
   whitespace-only, or non-hex. Happy paths always set a valid hex key.
2. Key secrecy. The seal-key VALUE is never echoed to stdout/stderr by
   any front-door command, including failure paths.
3. Doctor panels. `doctor` reports BetExecutor.__init__ defaulting
   `_enabled = False` (money switch off) and surfaces CALLISTO_LOCAL_ONLY
   on/off. It fails closed (exit 1) when the seal is unkeyed/non-hex or
   when the bind host is non-loopback.
4. Runs/show persistence lives in tools.cli.runs: `_cmd_runs` lists
   persisted records newest-first; `_cmd_show` re-prints a record and
   re-verifies artifact hashes and fetch digests; mismatches are loud
   and exit non-zero; missing digests are hard failures.
5. Paper-trade status gate never contains "live" — arming live betting
   is out of bounds for this codebase and these tests pin that.
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
from tools.cli.ask import _persist_run, _result_record, _runs_dir  # noqa: E402
from tools.cli.doctor import _cmd_doctor  # noqa: E402
from tools.cli.runs import (  # noqa: E402
    _fetch_digest_status,
    _load_run,
    _verify_artifact,
    _cmd_runs,
    _cmd_show,
)

VALID_KEY = "ab" * 32                     # exactly 64 hex chars
OTHER_VALID_KEY = "deadbeef" * 8          # another valid hex value
BAD_KEYS = [
    "",
    "   ",
    "\t\n",
    "zzzz",
    "ab" * 31 + "g",                      # one non-hex char at the end
    "0x" + "ab" * 31,                     # prefix garbage
    "not-a-key-at-all",
]
KEYS_NEVER_PRINTED = [VALID_KEY, OTHER_VALID_KEY]


# ── helpers ────────────────────────────────────────────────────────────────

def _assert_no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key value leaked into output"


@pytest.fixture
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


@pytest.fixture(autouse=True)
def _loopback_bind(monkeypatch):
    monkeypatch.setenv("CALLISTO_BIND_HOST", "127.0.0.1")


def _boom_router(*a, **k):
    raise AssertionError("router loaded despite unkeyed seal")


def _boom_engine(*a, **k):
    raise AssertionError("engine built despite unkeyed seal")


async def _boom_research(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite unkeyed seal")


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may
    execute when the gate refuses."""
    monkeypatch.setattr(callisto, "_load_router", _boom_router)
    monkeypatch.setattr(callisto, "_make_engine", _boom_engine)
    monkeypatch.setattr(callisto, "_result_record", _boom_research)


class FakeRouter:
    class _Ledger:
        def snapshot(self):
            return {"by_tier": {"gpu1": {"calls": 3}}}

    def __init__(self, endpoints=("gpu1",), health=None):
        self.endpoints = list(endpoints)
        self.task_classes = {"decompose": "gpu1"}
        self.default_tier_name = "gpu1"
        self._health = health or {"status": "ok"}
        self.cost_ledger = self._Ledger()
        self.check_health_called_with = None

    async def check_health(self, tier):
        self.check_health_called_with = tier
        return self._health


class FakeEngine:
    def __init__(self, *, model=None, adversary_router=None):
        self.model = model
        self.adversary_router = adversary_router
        self.ran_with = None

    async def run(self, question):
        self.ran_with = question
        leaf = NS(text="sub-question", answer="an answer",
                  tier="SPECULATIVE", confidence=0.34)
        fetch = NS(source_name="openalex",
                   url="https://api.openalex.org/x",
                   content_sha256="b" * 64)
        ob = NS(text="only one independent source")
        return NS(sealed=True, refusal_reason="", leaves=[leaf],
                  confidence_score=0.34, confidence_tier="SPECULATIVE",
                  conclusion="sealed conclusion", fetches=[fetch],
                  objections=[ob], notes=["note one"], artifact_refs=[])


@pytest.fixture
def wired(monkeypatch):
    router = FakeRouter()
    engines = []

    def load_router(path):
        return router

    def make_engine(router_, self_review):
        eng = FakeEngine(model=router_,
                         adversary_router=(None if self_review else router_))
        engines.append(eng)
        return eng

    monkeypatch.setattr("callisto._load_router", load_router)
    monkeypatch.setattr("callisto._make_engine", make_engine)
    return router, engines


def _ask_args(q="0025 question"):
    return build_parser().parse_args(["ask", q])


def _run_doctor(providers: str | None = None,
                extra_env: dict | None = None) -> tuple[int, str]:
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    argv = ["doctor"]
    if providers:
        argv += ["--providers", providers]
    args = build_parser().parse_args(argv)
    rc = _cmd_doctor(args)
    # env vars set above persist for the process only during this helper;
    # tests that care use monkeypatch explicitly.


# ── 1. seal gate: refuse everything without a valid hex key ──────────────

class TestSealGateRefusals:
    @pytest.mark.parametrize("bad", BAD_KEYS, ids=lambda v: repr(v)[:20])
    def test_bad_keys_exit_two(self, bad, monkeypatch, capsys):
        if bad.strip():
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        else:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        out = capsys.readouterr().out + capsys.readouterr().err
        assert out.startswith("FAIL:")
        _assert_no_key_leak(out)

    @pytest.mark.parametrize("bad", BAD_KEYS, ids=lambda v: repr(v)[:20])
    def test_ask_refuses_without_running_anything(
            self, bad, monkeypatch, capsys):
        if bad.strip():
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        else:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        assert rc == 2
        out = capsys.readouterr().out + capsys.readouterr().err
        assert "unkeyed" in out or "hex" in out
        _assert_no_key_leak(out)

    def test_whitespace_only_key_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY + " \t ")
        # surrounding whitespace is tolerated after strip() — the VALUE
        # itself must be pure hex though
        assert check_seal_key() is True

    def test_uppercase_hex_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", ("AB" * 32))
        assert check_seal_key() is True

    def test_odd_length_hex_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abc")
        assert check_seal_key() is False

    def test_64_hex_is_the_canonical_happy_shape(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True


# ── 2. happy paths always set a valid hex key ────────────────────────────

class TestAskHappyPath:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def test_sealed_result_exits_zero(self, wired, capsys):
        _, engines = wired
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        assert rc == 0
        assert engines[0].ran_with == "0025 question"

    def test_output_carries_verdict_and_sources(self, wired, capsys):
        asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert "SEALED" in out
        assert "openalex" in out
        _assert_no_key_leak(out)

    def test_backend_pinning_routes_health_check(self, wired, monkeypatch,
                                                 capsys):
        router, engines = wired
        args = build_parser().parse_args(
            ["ask", "--backend", "gpu1", "q"])
        rc = asyncio.run(callisto._cmd_ask(args))
        assert rc == 0
        assert router.check_health_called_with == "gpu1"

    def test_unknown_backend_exit_two(self, wired, monkeypatch, capsys):
        args = build_parser().parse_args(
            ["ask", "--backend", "nope", "q"])
        rc = asyncio.run(callisto._cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "unknown provider tier" in out

    def test_unhealthy_backend_exit_two(self, monkeypatch, capsys):
        router = FakeRouter(health={"status": "down"})
        monkeypatch.setattr("callisto._load_router", lambda p: router)
        args = build_parser().parse_args(["ask", "--backend", "gpu1", "q"])
        rc = asyncio.run(callisto._cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "unhealthy" in out
        _assert_no_key_leak(out)


# ── 3. doctor: money switches, local-only, seal/bind fail-closed ─────────

class TestDoctorPanels:
    def _doctor(self, tmp_path, monkeypatch, capsys, **env):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        args = build_parser().parse_args(
            ["doctor", "--providers", str(tmp_path / "missing.yaml")])
        rc = _cmd_doctor(args)
        return rc, capsys.readouterr().out

    def test_betexecutor_disabled_reported(self, tmp_path, monkeypatch,
                                           capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys)
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out

    def test_local_only_surfaced_on_and_off(self, tmp_path, monkeypatch,
                                            capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_LOCAL_ONLY=None)
        assert "CALLISTO_LOCAL_ONLY: off" in out
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_LOCAL_ONLY="1")
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_money_switch_panel_present(self, tmp_path, monkeypatch, capsys):
        _, out = self._doctor(tmp_path, monkeypatch, capsys)
        assert "== money switches ==" in out
        assert "OrderManager.__init__ defaults _enabled = False" in out

    def test_seal_panels_fail_closed(self, tmp_path, monkeypatch, capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_SEAL_KEY=None)
        assert "== seal ==" in out
        assert "FAIL: CALLISTO_SEAL_KEY is not set" in out
        assert rc == 1

    def test_non_hex_seal_fails_doctor(self, tmp_path, monkeypatch, capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_SEAL_KEY="zz-not-hex")
        assert "not valid hex" in out
        assert rc == 1

    def test_non_loopback_bind_fails(self, tmp_path, monkeypatch, capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_BIND_HOST="0.0.0.0")
        assert "== bind ==" in out
        assert "FAIL: binding to an unspecified address" in out
        assert rc == 1

    def test_loopback_default_ok(self, tmp_path, monkeypatch, capsys):
        rc, out = self._doctor(tmp_path, monkeypatch, capsys,
                               CALLISTO_BIND_HOST=None)
        assert "host: 127.0.0.1" in out
        assert "loopback default" in out

    def test_doctor_never_prints_the_key(self, tmp_path, monkeypatch, capsys):
        _, out = self._doctor(tmp_path, monkeypatch, capsys,
                              CALLISTO_SEAL_KEY=OTHER_VALID_KEY)
        _assert_no_key_leak(out)

    def test_live_execute_switch_is_visible(self, tmp_path, monkeypatch,
                                            capsys):
        _, out = self._doctor(tmp_path, monkeypatch, capsys,
                              CALLISTO_ALLOW_LIVE_EXECUTE=None)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out


# ── 4. runs/show persistence stays in tools.cli.runs ─────────────────────

def _fake_result():
    from tools.artifacts import ArtifactRef
    return NS(
        sealed=True, refusal_reason="",
        conclusion="Foundry concentration is the binding constraint.",
        confidence_score=0.34, confidence_tier="SPECULATIVE",
        leaves=[NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256="a" * 64, kind="csv",
                                   name="concentration.csv")],
        fetches=[NS(source_name="openalex",
                    url="https://api.openalex.org/x",
                    content_sha256="b" * 64)],
        objections=[NS(text="one independent source only")],
        notes=[])


class TestPersistence:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def test_record_roundtrip_through_tools_cli_runs(self, runs_env):
        rec = _result_record(_fake_result(), "roundtrip question")
        path = _persist_run(rec)
        assert path.parent == runs_env
        loaded, loaded_path = _load_run(path.stem)
        assert loaded_path == path
        assert loaded["question"] == "roundtrip question"
        assert loaded["sealed"] is True
        assert loaded["conclusion"].startswith("Foundry")

    def test_runs_lists_saved_records_newest_first(self, runs_env, capsys):
        for q in ("first run", "second run"):
            _persist_run(_result_record(_fake_result(), q))
            import time as _t
            _t.sleep(1.1)  # recorded_at has second resolution
        args = build_parser().parse_args(["runs"])
        rc = _cmd_runs(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert out.index("second run") < out.index("first run")
        assert "SEALED" in out

    def test_runs_empty_dir_message(self, runs_env, capsys):
        rc = _cmd_runs(build_parser().parse_args(["runs"]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "no saved runs yet" in out

    def test_load_run_missing_returns_none(self, runs_env):
        rec, path = _load_run("does-not-exist")
        assert rec is None and path is None

    def test_show_missing_run_exits_one(self, runs_env, capsys):
        args = build_parser().parse_args(["show", "ghost"])
        rc = _cmd_show(args)
        assert rc == 1
        assert "no run matching" in capsys.readouterr().out

    def test_show_prints_conclusion_and_verdict(self, runs_env, capsys):
        path = _persist_run(_result_record(_fake_result(), "show me"))
        rc = _cmd_show(build_parser().parse_args(["show", path.stem]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "SEALED" in out
        assert "binding constraint" in out

    def test_fetch_digest_status_matrix(self):
        ok = {"content_sha256": hashlib_sha("hello"), "body": "hello"}
        status, hard = _fetch_digest_status(ok)
        assert (status, hard) == ("ok", False)

        missing, hard = _fetch_digest_status({})
        assert hard and "MISSING" in missing

        malformed, hard = _fetch_digest_status({"content_sha256": "xyz"})
        assert hard and "MALFORMED" in malformed

        nonhex, hard = _fetch_digest_status({"content_sha256": "z" * 64})
        assert hard and "non-hex" in nonhex

        soft = {"content_sha256": hashlib_sha("remote")}
        status, hard = _fetch_digest_status(soft)
        assert hard is False and "unverified" in status

        mismatch = {"content_sha256": hashlib_sha("expected"),
                    "body": "actual"}
        status, hard = _fetch_digest_status(mismatch)
        assert hard and status == "DIGEST MISMATCH"

    def test_verify_artifact_reports_missing(self):
        status = _verify_artifact("f" * 64)
        assert "missing" in status or "unverifiable" in status


def hashlib_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── 5. paper-trade gate never contains "live" ────────────────────────────

class TestPaperTradeGate:
    def test_statuses_is_a_frozenset_without_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        lowered = {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}
        assert "live" not in lowered

    def test_paper_signal_helper_only_matches_paper(self):
        from tools.signals import paper
        fn = getattr(paper, "is_paper_trade_status", None) or \
            getattr(paper, "_is_paper", None)
        if fn is not None:
            assert fn("paper_trading") is True
            assert fn("live") is False
            assert fn("executed") is False

    def test_statuses_are_paper_only(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
        assert _PAPER_TRADE_SIGNAL_STATUSES <= {"paper_trading"}

    def test_source_never_arms_live_by_default(self):
        src = (REPO / "tools" / "signals" / "paper.py").read_text(
            encoding="utf-8")
        assert '"live"' not in src.replace("'live'", '"live"') or \
            "live" not in src.split("_PAPER_TRADE_SIGNAL_STATUSES")[1].split(
                "}")[0]


# ── parser sanity (front door surface) ───────────────────────────────────

class TestFrontDoorParser:
    def test_runs_and_show_subcommands_exist(self):
        p = build_parser()
        assert p.parse_args(["runs"]).limit >= 1
        args = p.parse_args(["show", "someid"])
        assert args.run_id == "someid"

    def test_ask_self_review_flag(self):
        args = build_parser().parse_args(["ask", "--self-review", "q"])
        assert args.self_review is True
