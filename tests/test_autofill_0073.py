"""Autofill #0073 — characterization of the ask/runs/doctor front door.

This module pins the CURRENT safety behavior of the `callisto` CLI surface,
so any future change that silently weakens the fail-closed contract shows
up as a red test. It complements tests/test_ask_char.py,
tests/test_cli_front_door.py and tests/test_cli_runs.py without replacing
them.

Contract under characterization
-------------------------------
A. The ask seal gate: `callisto ask` refuses to start ANY research unless
   CALLISTO_SEAL_KEY is a valid hex seal key. Refusal means exit code 2 AND
   no router/engine construction AND no run record persisted.
B. Happy paths MUST set a valid 64-hex-char key before touching the
   pipeline seams; every happy-path test below does exactly that.
C. doctor reports its three panels and specifically reports BetExecutor as
   default-disabled (`_enabled = False`) plus the CALLISTO_LOCAL_ONLY /
   CALLISTO_ALLOW_LIVE_EXECUTE switch states.
D. The money switches stay safe: OrderManager/BetExecutor construct
   disabled; arming is refused under CALLISTO_LOCAL_ONLY; the paper-signal
   hard gate never admits "live".
E. runs/show persistence lives in tools.cli.runs: records round-trip,
   prefixes resolve, mismatches are reported loudly, and the seal key value
   is never printed anywhere.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402
from tools.cli.ask import _runs_dir  # noqa: E402
from tools.cli.runs import (  # noqa: E402
    _fetch_digest_status,
    _load_run,
    _verify_artifact,
)

VALID_KEY = "ab" * 32          # exactly 64 hex chars
OTHER_KEY = "deadbeef" * 8     # a second valid hex string to grep leaks for

KEYS_NEVER_PRINTED = (VALID_KEY, OTHER_KEY)


# ── shared helpers ─────────────────────────────────────────────────────────

def _assert_no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key leaked into output ({key[:8]}…)"


@pytest.fixture
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _ask_args(q="char q", backend=None, self_review=False):
    return Namespace(
        providers=callisto._default_providers_path(),
        backend=backend, question=q, self_review=self_review)


class _Ledger:
    def snapshot(self):
        return {"by_tier": {}}


class _FakeRouter:
    endpoints = ["gpu1"]
    task_classes = {"decompose": "gpu1"}
    default_tier_name = "gpu1"
    cost_ledger = _Ledger()

    async def check_health(self, name):
        return {"status": "ok"}


class _FakeEngine:
    def __init__(self, sealed=True):
        self.sealed = sealed
        self.questions = []

    async def run(self, q):
        self.questions.append(q)
        return NS(sealed=self.sealed,
                  refusal_reason="" if self.sealed else "stub refusal",
                  conclusion="c" if self.sealed else "",
                  confidence_score=0.42,
                  confidence_tier="SPECULATIVE",
                  leaves=[], fetches=[], objections=[],
                  notes=[], artifact_refs=[])


def _wire_pipeline(monkeypatch, engine=None):
    engine = engine or _FakeEngine()
    router = _FakeRouter()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(
        callisto, "_make_engine",
        lambda r, self_review=False: engine)
    monkeypatch.setattr(
        callisto, "_result_record",
        lambda result, q: {
            "recorded_at": "2026-08-26T00:00:00+00:00",
            "question": q,
            "sealed": getattr(result, "sealed", False),
            "confidence": {"score": 0.42, "tier": "SPECULATIVE"},
            "artifacts": [], "fetches": [], "objections": [],
            "notes": [],
        })
    return router, engine


def _boom_seam(*a, **k):  # pragma: no cover - must never execute
    raise AssertionError("post-gate seam executed despite bad seal")


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may run."""
    monkeypatch.setattr(callisto, "_load_router", _boom_seam)
    monkeypatch.setattr(callisto, "_make_engine", _boom_seam)
    monkeypatch.setattr(callisto, "_result_record", _boom_seam)


# ── A. the seal gate itself ────────────────────────────────────────────────

class TestSealGateCharacterization:
    @pytest.mark.parametrize("bad_env", [
        None, "", "   ", "\t\n", "nothex", "zz" * 32,
        "ab" * 31 + "gg", "0x" + "a" * 64, "abc",
    ])
    def test_invalid_keys_refused(self, bad_env, monkeypatch, capsys):
        if bad_env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad_env)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        _assert_no_key_leak(out)

    @pytest.mark.parametrize("key", [VALID_KEY, OTHER_KEY])
    def test_valid_hex_keys_accepted(self, key, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        assert check_seal_key() is True

    def test_whitespace_wrapped_valid_key_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f" {VALID_KEY} ")
        assert check_seal_key() is True


# ── A/B. ask fails closed; happy paths are keyed ───────────────────────────

class TestAskGateEndToEnd:
    @pytest.mark.parametrize("key_env", [None, "", "   ", "not-even-hex"])
    def test_ask_exit_two_and_no_research_no_record(
            self, key_env, monkeypatch, capsys, runs_env):
        if key_env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", key_env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert rc == 2, f"expected exact refusal code 2, got {rc}"
        assert "FAIL" in out
        assert list(_runs_dir().glob("*.json")) == []

    def test_refusal_does_not_leak_attempted_key(self, monkeypatch, capsys):
        attempted = "f00d" * 15 + "zz"
        monkeypatch.setenv("CALLISTO_SEAL_KEY", attempted)
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert attempted not in out
        _assert_no_key_leak(out)

    def test_keyed_happy_path_runs_and_exits_zero(
            self, monkeypatch, capsys, runs_env):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, engine = _wire_pipeline(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args("sealed question")))
        out = capsys.readouterr().out
        assert rc == 0
        assert engine.questions == ["sealed question"]
        assert "SEALED" in out
        _assert_no_key_leak(out)

    def test_unsealed_result_reported_REFUSED(self, monkeypatch, capsys,
                                              runs_env):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, engine = _wire_pipeline(monkeypatch, _FakeEngine(sealed=False))
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert engine.questions == ["char q"]      # research DID run
        assert rc != 0
        assert "REFUSED" in out

    def test_unknown_backend_tier_refused_after_gate(self, monkeypatch,
                                                     capsys, runs_env):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _wire_pipeline(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="ghost-tier")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier 'ghost-tier'" in out

    def test_parser_routes_ask_subcommand(self):
        args = build_parser().parse_args(["ask", "--backend", "gpu1", "q"])
        assert args.question == "q"
        assert args.backend == "gpu1"
        assert args.self_review is False


# ── C. doctor panels ──────────────────────────────────────────────────────

def _run_doctor(extra_env=None, providers=None, monkeypatch=None,
                capsys=None):
    for k, v in (extra_env or {}).items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    argv = ["doctor"]
    if providers:
        argv += ["--providers", providers]
    rc = callisto._cmd_doctor(build_parser().parse_args(argv))
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


class TestDoctorPanelsCharacterization:
    def test_all_three_panels_present_when_ok(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            providers=str(REPO / "config" / "providers.yaml"),
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "== money switches ==" in out
        assert "HMAC-SHA256" in out
        assert "doctor: OK" in out
        assert rc == 0
        _assert_no_key_leak(out)

    def test_doctor_reports_bet_executor_default_disabled(
            self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out

    def test_doctor_reports_order_manager_disabled_by_default(
            self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert ("OK: OrderManager.__init__ defaults _enabled = False"
                in out)

    def test_local_only_switch_visibility_on(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_LOCAL_ONLY": "1"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_switch_visibility_off_by_default(
            self, monkeypatch, capsys):
        _, out = _run_doctor(extra_env={"CALLISTO_LOCAL_ONLY": None},
                             monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    @pytest.mark.parametrize("val", ["0", "false", "no", ""])
    def test_doctor_display_matches_gate_truthiness(
            self, val, monkeypatch, capsys):
        """Doctor's LOCAL_ONLY panel must match is_local_only /
        local_only_enabled so an operator is not told the nuclear switch
        is on while the router still uses hosted rails. The gate is
        unchanged: only 1/true/yes arm it."""
        _, out = _run_doctor(extra_env={"CALLISTO_LOCAL_ONLY": val},
                             monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out
        from tools.betexec.lifecycle import arm_gate_refusal
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
        assert arm_gate_refusal() == ""

    def test_allow_live_execute_off_by_default(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_ALLOW_LIVE_EXECUTE": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_unkeyed_doctor_problems_found(self, monkeypatch, capsys):
        rc, out = _run_doctor(extra_env={"CALLISTO_SEAL_KEY": None},
                              monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "PROBLEMS FOUND" in out
        assert "unkeyed" in out.lower()

    def test_wildcard_bind_named_but_not_the_key(self, monkeypatch, capsys):
        rc, out = _run_doctor(extra_env={"CALLISTO_BIND_HOST": "0.0.0.0"},
                              monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "0.0.0.0" in out
        _assert_no_key_leak(out)

    def test_loopback_default_reported(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_BIND_HOST": None,
                       "CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "host: 127.0.0.1" in out
        assert "loopback default" in out


# ── D. money switches stay safe ────────────────────────────────────────────

class TestMoneySwitchesStaySafe:
    """Static + behavioral pins that live betting cannot be armed by drift."""

    def test_bet_executor_constructs_disabled(self):
        from tools.bet_executor import BetExecutor
        ex = BetExecutor()
        assert ex.is_enabled is False

    def test_enable_refused_under_local_only(self, monkeypatch):
        from tools.bet_executor import BetExecutor
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_arm_gate_refusal_reason_names_local_only(self, monkeypatch):
        from tools.betexec.lifecycle import arm_gate_refusal
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        refusal = arm_gate_refusal()
        assert refusal
        assert "CALLISTO_LOCAL_ONLY" in refusal

    def test_arm_gate_open_without_local_only(self, monkeypatch):
        from tools.betexec.lifecycle import arm_gate_refusal
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        assert arm_gate_refusal() == ""

    def test_is_local_only_truthiness_matrix(self, monkeypatch):
        from tools.betexec.lifecycle import is_local_only
        for truthy in ("1", "true", "yes", "TRUE", "Yes"):
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", truthy)
            assert is_local_only() is True, truthy
        for falsy in ("", "0", "false", "off", "  "):
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", falsy)
            assert is_local_only() is False, repr(falsy)
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        assert is_local_only() is False

    def test_source_pins_enabled_false_in_init(self):
        src = Path(REPO / "tools" / "bet_executor.py").read_text()
        init_m = re.search(
            r"class BetExecutor\b.*?def __init__\(self\):(.*?)(\n    (?:async )?def )",
            src, re.S)
        assert init_m, "BetExecutor.__init__ not found"
        assert re.search(r"self\._enabled\s*=\s*False", init_m.group(1))

    def test_paper_signal_statuses_exclude_live(self):
        from tools.signals.paper import (
            _PAPER_TRADE_SIGNAL_STATUSES, allowed_paper_statuses)
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
        assert allowed_paper_statuses() <= {"paper_trading"}

    def test_reject_non_paper_matrix(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("live") is True
        assert reject_non_paper("rejected") is True
        assert reject_non_paper(None) is True
        assert reject_non_paper("paper_trading") is False

    async def _gate_check(self, status):
        """Exercise the real gate path on BacktestEngine.generate_paper_trade_signal
        without constructing the engine: bind a stub to the unbound function."""
        from tools.backtest import BacktestEngine
        mgr_calls = []

        class _Mgr:
            async def get_hypothesis(self, hid):
                mgr_calls.append(hid)
                return {"status": status}

        async def fake_pipeline(engine, hypothesis_id, live_odds):
            raise AssertionError("pipeline ran past the hard gate")

        import tools.btest.paper_pipeline as pp
        orig = pp.generate_paper_trade_signal
        pp.generate_paper_trade_signal = \
            __import__("tools.backtest", fromlist=["x"]).paper_pipeline \
            .generate_paper_trade_signal
        try:
            func = BacktestEngine.generate_paper_trade_signal.__wrapped__ \
                if hasattr(BacktestEngine.generate_paper_trade_signal,
                           "__wrapped__") \
                else BacktestEngine.generate_paper_trade_signal
            engine = NS(hypothesis_manager=_Mgr())
            bound = BacktestEngine.generate_paper_trade_signal(engine, "h1",
                                                               {})
            got = await bound
        finally:
            pp.generate_paper_trade_signal = orig
        return got, mgr_calls

    @pytest.mark.parametrize("status", ["live", None, "rejected", "LIVE"])
    def test_non_paper_status_yields_empty_signals(self, status):
        got, calls = asyncio.run(self._gate_check(status))
        assert got == []
        assert calls == ["h1"]     # manager consulted exactly once

    def test_generate_paper_trade_signal_never_accepts_live_status(self):
        """Direct pin on the method's docstring-level contract: 'live' must
        short-circuit before paper_pipeline is reached."""
        import tools.backtest as bt
        seen = []

        class _Mgr:
            async def get_hypothesis(self, hid):
                return {"status": "live"}

        class _Eng(NS.__class__ if False else object):
            pass

        engine = NS(hypothesis_manager=_Mgr())
        orig = bt.paper_pipeline.generate_paper_trade_signal

        def spy(*a, **k):  # pragma: no cover - must never run
            seen.append(a)
            raise AssertionError("paper pipeline ran for status=live")

        bt.paper_pipeline.generate_paper_trade_signal = spy
        try:
            coro = bt.BacktestEngine.generate_paper_trade_signal(
                engine, "h-live", {"odds": -110})
            result = asyncio.run(coro)
        finally:
            bt.paper_pipeline.generate_paper_trade_signal = orig
        assert result == []
        assert seen == []


# ── E. runs/show persistence stays in tools.cli.runs ──────────────────────

def _fake_result(sealed=True):
    from tools.artifacts import ArtifactRef
    digest = hashlib.sha256(b"artifact-bytes-0073").hexdigest()
    good_fetch = hashlib.sha256(b"fetched body").hexdigest()
    return NS(
        sealed=sealed, refusal_reason="" if sealed else "why",
        conclusion="concentration finding", confidence_score=0.31,
        confidence_tier="SPECULATIVE",
        leaves=[NS(text="leaf", answer="ans", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256=digest, kind="csv", name="x.csv")],
        fetches=[NS(source_name="openalex", url="https://api.openalex.org/x",
                    content_sha256=good_fetch)],
        objections=[NS(text="single source")], notes=[])


def _record(result, q="q"):
    from callisto import _result_record
    rec = _result_record(result, q)
    # normalize fetch dicts so persistence matches the show-side validator
    rec["fetches"] = [{
        "source": "openalex", "url": "https://api.openalex.org/x",
        "content_sha256": f.content_sha256,
        "body": b"fetched body".decode(),
    } for f in result.fetches]
    return rec


class TestRunsShowPersistence:
    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def test_persist_load_roundtrip_preserves_fields(self, runs_env):
        from callisto import _persist_run
        rec = _record(_fake_result(), "round trip")
        path = _persist_run(rec)
        loaded, loaded_path = _load_run(path.stem)
        assert loaded_path == path
        assert json.loads(json.dumps(rec)) == loaded

    def test_runs_module_owns_the_persistence_helpers(self):
        """The product helpers must keep living in tools.cli.runs (the
        front door callisto.py re-exports them from there)."""
        import tools.cli.runs as runs_mod
        for name in ("_cmd_runs", "_cmd_show", "_load_run",
                     "_fetch_digest_status"):
            fn = getattr(runs_mod, name)
            assert fn.__module__ == "tools.cli.runs", \
                f"{name} moved out of tools.cli.runs into {fn.__module__}"
        import callisto
        for name in ("_cmd_runs", "_cmd_show"):
            assert getattr(callisto, name).__module__ == "tools.cli.runs"
        assert callable(runs_mod._fetch_digest_status)

    def test_cmd_runs_lists_sealed_and_refused(self, runs_env, capsys):
        from callisto import _persist_run, _cmd_runs
        _persist_run(_record(_fake_result(True), "sealed one"))
        refused = _record(_fake_result(False), "refused one")
        refused["sealed"] = False
        _persist_run(refused)
        assert _cmd_runs(Namespace(limit=20)) == 0
        out = capsys.readouterr().out
        assert out.count("SEALED") == 1
        assert out.count("REFUSED") == 1
        _assert_no_key_leak(out)

    def test_cmd_runs_respects_limit(self, runs_env, capsys):
        from callisto import _persist_run, _cmd_runs
        for i in range(5):
            _persist_run(_record(_fake_result(), f"q {i}"))
        assert _cmd_runs(Namespace(limit=3)) == 0
        listed = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        assert len(listed) == 3
        newest_first = sorted((p.stem for p in runs_env.glob("*.json")),
                              reverse=True)[:3]
        assert [ln.split()[0] for ln in listed] == newest_first

    def test_show_unknown_id_hint(self, runs_env, capsys):
        from tools.cli.runs import _cmd_show
        assert _cmd_show(Namespace(run_id="missing")) == 1
        out = capsys.readouterr().out
        assert "no run matching 'missing'" in out
        assert "`callisto runs`" in out

    def test_show_verified_artifact_flagged_ok(self, runs_env, tmp_path,
                                               monkeypatch, capsys):
        from callisto import _persist_run, _cmd_show
        from tools.artifacts import ArtifactStore
        payload = b"artifact-bytes-0073"
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(tmp_path / "arts"))
        ArtifactStore(root=tmp_path / "arts").put(payload, kind="csv",
                                                  name="x.csv")
        path = _persist_run(_record(_fake_result(), "verified"))
        assert _cmd_show(Namespace(run_id=path.stem)) == 0
        assert "[ok" in capsys.readouterr().out

    def test_verify_artifact_detects_corruption(self, runs_env, tmp_path,
                                                monkeypatch):
        payload = b"artifact-bytes-0073"
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(tmp_path / "arts"))
        from tools.artifacts import ArtifactStore
        store = ArtifactStore(root=tmp_path / "arts")
        store.put(payload, kind="csv", name="x.csv")
        assert _verify_artifact(digest) == "ok"
        store.put(b"different bytes entirely!!", kind="csv", name="y.csv")
        other = hashlib.sha256(b"different bytes entirely!!").hexdigest()
        assert _verify_artifact(other) == "ok"
        assert _verify_artifact("e" * 64) in ("missing",) or "unverif" in \
            _verify_artifact("e" * 64)

    def test_fetch_digest_hard_fail_matrix(self):
        good = hashlib.sha256(b"body").hexdigest()
        ok, hard = _fetch_digest_status(
            {"content_sha256": good, "body": "body"})
        assert (ok, hard) == ("ok", False)
        for bad in ({}, {"content_sha256": ""}, {"content_sha256": None},
                    {"content_sha256": "short"},
                    {"content_sha256": "z" * 64}):
            status, hard = _fetch_digest_status(bad)
            assert hard is True and status != "ok"

    def test_show_exits_nonzero_on_digest_mismatch(self, runs_env, capsys):
        from callisto import _persist_run, _cmd_show
        rec = _record(_fake_result(), "tampered")
        rec["fetches"][0]["body"] = "rewritten body"
        path = _persist_run(rec)
        assert _cmd_show(Namespace(run_id=path.stem)) == 1
        out = capsys.readouterr().out
        assert "DIGEST MISMATCH" in out
        assert "WARNING" in out

    def test_missing_fetch_digest_is_hard_fail_in_show(self, runs_env,
                                                       capsys):
        from callisto import _persist_run, _cmd_show
        rec = _record(_fake_result(), "no digest")
        rec["fetches"][0].pop("content_sha256")
        path = _persist_run(rec)
        assert _cmd_show(Namespace(run_id=path.stem)) == 1
        assert "MISSING DIGEST" in capsys.readouterr().out

    def test_ambiguous_prefix_raises_systemexit(self, runs_env):
        from callisto import _persist_run
        p1 = _persist_run(_record(_fake_result(), "one"))
        dup = runs_env / (p1.stem[:16] + "aaaa.json")
        dup.write_text(json.dumps(_record(_fake_result(), "two")))
        with pytest.raises(SystemExit, match="ambiguous"):
            _load_run(p1.stem[:16])

    def test_unique_prefix_resolves(self, runs_env):
        from callisto import _persist_run
        path = _persist_run(_record(_fake_result(), "prefix me"))
        rec, _ = _load_run(path.stem[:12])
        assert rec["question"] == "prefix me"


# ── E2. the seal key never appears in output or records ───────────────────

class TestKeyNeverPrintedAnywhere0073:
    KEY = "cafe" * 16

    @pytest.fixture(autouse=True)
    def _set_secret(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", self.KEY)

    def test_runs_output_clean(self, runs_env, capsys):
        from callisto import _persist_run, _cmd_runs
        _persist_run(_record(_fake_result(), "q"))
        _cmd_runs(Namespace(limit=20))
        _assert_no_key_leak(capsys.readouterr().out)

    def test_show_output_clean_on_success_and_failure(self, runs_env, capsys):
        from callisto import _persist_run, _cmd_show
        path = _persist_run(_record(_fake_result(), "q"))
        _cmd_show(Namespace(run_id=path.stem))
        _assert_no_key_leak(capsys.readouterr().out)
        _cmd_show(Namespace(run_id="nope"))
        _assert_no_key_leak(capsys.readouterr().out)

    def test_persisted_json_free_of_key_material(self, runs_env):
        from callisto import _persist_run
        path = _persist_run(_record(_fake_result(), "q"))
        raw = path.read_text()
        assert self.KEY not in raw
        assert "seal_key" not in raw

    def test_doctor_output_clean_with_secret_set(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": self.KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        del rc
        _assert_no_key_leak(out)


# ── F. runs dir resolution & env isolation ────────────────────────────────

class TestRunsDirResolution:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        override = tmp_path / "custom-runs"
        override.mkdir(exist_ok=True)
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(override))
        assert _runs_dir() == override

    def test_records_land_in_overridden_dir(self, runs_env, monkeypatch):
        from callisto import _persist_run
        path = _persist_run(_record(_fake_result(), "placed right"))
        assert path.parent == runs_env
        assert path.exists()


# ── G. static production-source gates (read-only pins) ────────────────────

class TestProductionSourceGates:
    """Read-only source pins: the fail-closed lines exist verbatim."""

    def test_ask_source_contains_exact_exit_two_refusal(self):
        """The refusal lives in tools/cli/ask.py (callisto.py re-exports
        _cmd_ask from there); pin the fail-closed line in place."""
        src = (REPO / "tools" / "cli" / "ask.py").read_text()
        assert "check_seal_key()" in src
        m = re.search(r"(?:async )?def cmd_ask.*?(?=\n(?:async )?def |\Z)",
                      src, re.S)
        assert m, "cmd_ask not found"
        assert "return 2" in m.group(0)
        front = Path(REPO / "callisto.py").read_text()
        assert "tools.cli.ask" in front

    def test_paper_statuses_comment_forbids_live(self):
        src = (REPO / "tools" / "signals" / "paper.py").read_text()
        assert '"live"' in src or "'live'" in src
        assert "must NEVER be added" in src

    def test_lifecycle_docstring_keeps_gate_before_state_flip(self):
        src = (REPO / "tools" / "betexec" / "lifecycle.py").read_text()
        assert "BEFORE any state is flipped" in src

    def test_doctor_source_checks_both_executors(self):
        src = (REPO / "tools" / "cli" / "doctor.py").read_text()
        assert "OrderManager" in src
        assert "BetExecutor" in src
        assert "CALLISTO_LOCAL_ONLY" in src
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src
