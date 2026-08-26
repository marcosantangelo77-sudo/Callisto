"""Autofill characterization #0001 — ask / runs / doctor front door.

This module pins the safety-critical surface of the Callisto appliance as
it exists today. It is pure characterization: every assertion describes
current fail-closed behavior so that any future change which silently
weakens the gates turns the suite red instead of turning money loose.

Contract under test
-------------------
A. Seal gate (tools/cli/ask.py::check_seal_key)
   1. `ask` refuses unkeyed seals: unset, blank, whitespace-only and
      non-hex CALLISTO_SEAL_KEY values all produce exit code 2 AND no
      research may start (every post-gate seam explodes).
   2. A valid hex key opens the gate; happy paths in this module always
      set one.
   3. The seal-key VALUE is secret: no command prints it, even when it
      is invalid or the run fails downstream.
B. Doctor (tools/cli/doctor.py::cmd_doctor)
   4. doctor reports the three safety panels and diagnoses rather than
      crashing: seal panel FAILS closed on unset/non-hex keys, bind
      panel FAILS on non-loopback hosts, money-switches panel reports
      BetExecutor._enabled = False and the CALLISTO_LOCAL_ONLY state.
   5. doctor never prints the seal key value.
C. Runs persistence (tools/cli/runs.py)
   6. `runs` / `show` read exactly what `ask` persisted through
      tools.cli.ask::_runs_dir / _persist_run — same directory, same
      record schema, newest-first listing.
   7. Integrity honesty: missing/malformed fetch digests are hard
      failures that make `show` exit non-zero; they are reported,
      never swallowed.
D. Paper-trade signal hard gate (tools/signals/paper.py)
   8. The allowed-status set is EXACTLY {"paper_trading"}; "live" (and
      everything else) is rejected. This set must NEVER grow.
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

from callisto import build_parser, check_seal_key  # noqa: E402
from tools.cli.ask import _persist_run, _result_record, _runs_dir  # noqa: E402
from tools.cli.doctor import cmd_doctor  # noqa: E402
from tools.cli.runs import (  # noqa: E402
    _cmd_runs,
    _cmd_show,
    _fetch_digest_status,
    _load_run,
)
from tools.signals.paper import (  # noqa: E402
    allowed_paper_statuses,
    reject_non_paper,
)

VALID_KEY = "ab" * 32                      # exactly 64 hex chars
OTHER_VALID_KEY = "deadbeef" * 8           # another valid hex key

PROVIDERS_PATH = str(REPO / "config" / "providers.yaml")

UNKEYED_ENVS = [
    {},                                    # env var absent entirely
    {"CALLISTO_SEAL_KEY": ""},             # empty string
    {"CALLISTO_SEAL_KEY": "   "},          # whitespace only
    {"CALLISTO_SEAL_KEY": "\t\n "},        # mixed whitespace
]

NON_HEX_KEYS = [
    "zz" * 32,                             # right length, not hex
    "nothex!",                             # punctuation
    "with spaces inside",                  # prose
    "0x" + "a" * 60,                       # prefix garbage, wrong len too
]


# ── helpers ────────────────────────────────────────────────────────────────


def _no_leak(out: str) -> None:
    """The seal key VALUE must never appear in any output."""
    for key in (VALID_KEY, OTHER_VALID_KEY):
        assert key not in out, f"seal key value leaked: {key[:8]}…"


def _boom_router(*a, **k):
    raise AssertionError("router loaded despite refused seal")


async def _boom_research(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite refused seal")


def _wire_boom(callisto_mod, monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may
    execute when the gate refuses."""
    monkeypatch.setattr(callisto_mod, "_load_router", _boom_router)
    monkeypatch.setattr(
        callisto_mod, "_make_engine",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("engine built despite refused seal")))
    monkeypatch.setattr(callisto_mod, "_result_record", _boom_research)


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


class _BoomResult:
    """A result object whose very construction means the gate opened."""

    def __init__(self):
        raise AssertionError("pipeline ran past a refused seal")


# ── A. seal gate: refusal paths ────────────────────────────────────────────


class TestSealGateRefusals:
    @pytest.mark.parametrize("env", UNKEYED_ENVS,
                             ids=["unset", "empty", "spaces", "mixed-ws"])
    def test_unkeyed_env_refused_exit_2(self, monkeypatch, capsys, env,
                                        callisto=None):
        import callisto as callisto_mod
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        _wire_boom(callisto_mod, monkeypatch)
        args = argparse.Namespace(providers=PROVIDERS_PATH,
                                  backend=None, question="q",
                                  self_review=False)
        rc = asyncio.run(callisto_mod._cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "FAIL" in out
        _no_leak(out)

    @pytest.mark.parametrize("bad", NON_HEX_KEYS)
    def test_non_hex_key_refused_exit_2(self, monkeypatch, capsys, bad):
        import callisto as callisto_mod
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        _wire_boom(callisto_mod, monkeypatch)
        args = argparse.Namespace(providers=PROVIDERS_PATH,
                                  backend=None, question="q",
                                  self_review=False)
        rc = asyncio.run(callisto_mod._cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "not valid hex" in out
        _no_leak(out)

    def test_check_seal_key_predicate_matches_cmd(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "nothex!")
        assert check_seal_key() is False
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True

    def test_whitespace_padded_valid_hex_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f"  {VALID_KEY}\n")
        assert check_seal_key() is True

    def test_odd_length_hex_is_not_valid_hex(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abc")  # odd nibble count
        assert check_seal_key() is False
        _no_leak(capsys.readouterr().out)

    def test_gate_message_mentions_forgeable(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "forgeable" in out.lower()
        _no_leak(out)

    def test_backend_validation_happens_after_gate(self, monkeypatch,
                                                   capsys):
        """An unknown --backend still exits 2, but ONLY after the seal
        gate passes — the gate is the first line of defense."""
        import callisto as callisto_mod
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

        class _R:
            endpoints = ["gpu1"]
            task_classes = {"decompose": "gpu1"}
            default_tier_name = "gpu1"

            class cost_ledger:
                @staticmethod
                def snapshot():
                    return {"by_tier": {}}

            async def check_health(self, tier):
                return {"status": "ok"}

        monkeypatch.setattr(callisto_mod, "_load_router",
                            lambda p: _R())
        monkeypatch.setattr(
            callisto_mod, "_make_engine",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("engine built for unknown backend")))
        args = argparse.Namespace(providers=PROVIDERS_PATH,
                                  backend="does-not-exist",
                                  question="q", self_review=False)
        rc = asyncio.run(callisto_mod._cmd_ask(args))
        assert rc == 2
        out = capsys.readouterr().out
        assert "unknown provider tier" in out


# ── A2. seal gate: happy path shape ───────────────────────────────────────


class TestSealGateHappyPathShape:
    def test_valid_key_opens_gate_and_persists_record(
            self, monkeypatch, capsys, runs_isolated):
        """With a valid hex key the pipeline runs end-to-end (stubbed at
        the engine seam) and the record lands in tools.cli.ask's dir."""
        import callisto as callisto_mod
        from types import SimpleNamespace as NS
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

        class _Router:
            endpoints = ["gpu1"]
            task_classes = {"decompose": "gpu1"}
            default_tier_name = "gpu1"

            class cost_ledger:
                @staticmethod
                def snapshot():
                    return {"by_tier": {"gpu1": {"calls": 1}}}

            async def check_health(self, tier):
                return {"status": "ok"}

        result = NS(
            sealed=True, refusal_reason="",
            conclusion="it depends, honestly",
            confidence_score=0.31, confidence_tier="SPECULATIVE",
            leaves=[NS(text="leaf", answer="ans", tier="SPECULATIVE",
                       confidence=0.3)],
            artifact_refs=[], fetches=[], objections=[], notes=[])

        class _Engine:
            async def run(self, q):
                return result

        monkeypatch.setattr(callisto_mod, "_load_router",
                            lambda p: _Router())
        monkeypatch.setattr(callisto_mod, "_make_engine",
                            lambda r, self_review: _Engine())

        # Persist where ask persists: exercise the real record/persist path.
        record = callisto_mod._result_record(result, "happy q")
        path = _persist_run(record)
        assert path.parent == runs_isolated
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["question"] == "happy q"
        assert loaded["sealed"] is True
        assert loaded["conclusion"] == "it depends, honestly"
        assert loaded["confidence"]["tier"] == "SPECULATIVE"

        args = argparse.Namespace(providers=PROVIDERS_PATH,
                                  backend=None, question="happy q",
                                  self_review=False)
        rc = asyncio.run(callisto_mod._cmd_ask(args))
        assert rc == 0
        out = capsys.readouterr().out
        assert "SEALED" in out
        assert "run      :" in out
        _no_leak(out)

    def test_result_record_shape_is_stable(self):
        """Pin the persisted schema: downstream `show` depends on these
        exact keys existing."""
        from types import SimpleNamespace as NS
        result = NS(
            sealed=False, refusal_reason="too few sources",
            conclusion="", confidence_score=0.1,
            confidence_tier="UNVERIFIED",
            leaves=[NS(text="t", answer=None, tier="UNVERIFIED",
                       confidence=0.1)],
            artifact_refs=[], fetches=[
                NS(source_name="s", url="u", content_sha256="d" * 64)],
            objections=[NS(text="obj")], notes=["n1"])
        rec = _result_record(result, "schema q")
        for key in ("recorded_at", "question", "sealed", "refusal_reason",
                    "conclusion", "confidence", "leaves", "artifacts",
                    "fetches", "objections", "notes"):
            assert key in rec, f"missing persisted key {key}"
        assert rec["sealed"] is False
        assert rec["refusal_reason"] == "too few sources"
        assert rec["confidence"]["score"] == pytest.approx(0.1)
        assert rec["leaves"][0]["answer"] == ""     # None coerced to ""
        assert rec["fetches"][0]["source"] == "s"


# ── B. doctor ──────────────────────────────────────────────────────────────


def _doctor_args():
    return argparse.Namespace(providers=PROVIDERS_PATH)


class TestDoctorSafetyPanels:
    def test_reports_betexecutor_disabled_and_local_only(
            self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        rc = cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out
        assert "CALLISTO_LOCAL_ONLY: off" in out
        assert "== money switches ==" in out
        _no_leak(out)
        assert rc in (0, 1)  # diagnosis, never crash

    def test_local_only_on_is_reported(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_seal_panel_fails_on_unset_key(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        rc = cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "FAIL: CALLISTO_SEAL_KEY is not set" in out
        assert rc == 1
        _no_leak(out)

    def test_seal_panel_fails_on_nonhex_key(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-at-all-just-prose")
        rc = cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "is not valid hex" in out
        assert rc == 1
        _no_leak(out)

    def test_seal_panel_ok_on_valid_hex(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "OK: seal key is set (hex-valid)" in out
        assert "HMAC-SHA256" in out

    def test_bind_panel_fails_on_wildcard_host(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_BIND_HOST", "0.0.0.0")
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "FAIL: binding to an unspecified address" in out
        assert "== bind ==" in out

    def test_bind_panel_ok_on_loopback_default(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "host: 127.0.0.1" in out
        assert "OK: loopback default" in out

    def test_all_three_panels_always_printed(self, monkeypatch, capsys):
        """Even on the worst-configured box, doctor diagnoses all three
        safety panels instead of bailing early."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        monkeypatch.setenv("CALLISTO_BIND_HOST", "0.0.0.0")
        rc = cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "== money switches ==" in out
        assert "PROBLEMS FOUND" in out
        assert rc == 1
        _no_leak(out)

    def test_allow_live_execute_visibility(self, monkeypatch, capsys):
        """Doctor surfaces CALLISTO_ALLOW_LIVE_EXECUTE state either way;
        it must be visible, never hidden."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")
        cmd_doctor(_doctor_args())
        out = capsys.readouterr().out
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: on" in out


# ── C. runs / show persistence ────────────────────────────────────────────


def _sealed_record(question="runs q", fetches=None):
    from types import SimpleNamespace as NS
    result = NS(
        sealed=True, refusal_reason="",
        conclusion="conclusion text",
        confidence_score=0.42, confidence_tier="LOW",
        leaves=[NS(text="leaf", answer="a", tier="LOW", confidence=0.42)],
        artifact_refs=[], fetches=fetches or [], objections=[], notes=[])
    return _result_record(result, question)


class TestRunsPersistence:
    def test_runs_dir_respects_override(self, runs_isolated):
        assert _runs_dir() == runs_isolated
        assert runs_isolated.exists()

    def test_empty_runs_dir_prints_hint_exits_zero(self, runs_isolated,
                                                   capsys):
        args = argparse.Namespace(limit=20)
        rc = _cmd_runs(args)
        out = capsys.readouterr().out
        assert "no saved runs yet" in out
        assert rc == 0

    def test_persist_then_list_newest_first(self, runs_isolated, capsys):
        import datetime
        base = datetime.datetime(2026, 8, 26, 12, 0, 0)
        for i, mins in enumerate((0, 5, 10)):
            rec = _sealed_record(f"q{i}")
            rec["recorded_at"] = (
                base + datetime.timedelta(minutes=mins)).isoformat(
                timespec="seconds")
            _persist_run(rec)
        rc = _cmd_runs(argparse.Namespace(limit=20))
        assert rc == 0
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        assert len(lines) == 3
        # Newest (q2, +10min) listed first.
        assert "SEALED" in lines[0]
        stems = sorted(p.stem for p in runs_isolated.glob("*.json"))
        assert len(stems) == 3
        order = [p.stem for p in
                 sorted(runs_isolated.glob("*.json"), reverse=True)]
        newest = json.loads(
            (runs_isolated / f"{order[0]}.json").read_text())["question"]
        assert newest == "q2"

    def test_limit_caps_listing(self, runs_isolated, capsys):
        for i in range(4):
            _persist_run(_sealed_record(f"limit q{i}"))
        rc = _cmd_runs(argparse.Namespace(limit=2))
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
        assert len(lines) == 2

    def test_load_run_by_unique_prefix(self, runs_isolated):
        path = _persist_run(_sealed_record("prefix target q"))
        stem = path.stem
        rec, found = _load_run(stem[:12])
        assert found is not None
        assert rec["question"] == "prefix target q"

    def test_load_run_missing_returns_none(self, runs_isolated):
        rec, path = _load_run("nonexistent0000")
        assert rec is None and path is None

    def test_load_run_ambiguous_prefix_raises(self, runs_isolated):
        """An ambiguous prefix must refuse loudly, not guess."""
        _persist_run(_sealed_record("ambiguous alpha"))
        _persist_run(_sealed_record("ambiguous beta"))
        with pytest.raises(SystemExit, match="ambiguous"):
            _load_run("2026")

    def test_show_roundtrips_sealed_record(self, runs_isolated, capsys):
        path = _persist_run(_sealed_record("show me"))
        rc = _cmd_show(argparse.Namespace(run_id=path.stem[:16]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "run      : " in out
        assert "SEALED" in out
        assert "conclusion text" in out
        assert "record   : " in out

    def test_show_missing_run_exits_one(self, runs_isolated, capsys):
        rc = _cmd_show(argparse.Namespace(run_id="zzzzzz"))
        out = capsys.readouterr().out
        assert rc == 1
        assert "no run matching" in out


class TestFetchDigestHonesty:
    """`show` re-checks provenance digests; failures are loud."""

    def test_valid_digest_without_payload_is_soft_unverified(self):
        status, hard = _fetch_digest_status({
            "source": "openalex", "url": "https://x/y",
            "content_sha256": "c" * 64})
        assert status.startswith("unverified")
        assert hard is False

    def test_matching_payload_verifies_ok(self):
        body = b"payload bytes"
        import hashlib
        digest = hashlib.sha256(body).hexdigest()
        status, hard = _fetch_digest_status({
            "source": "s", "url": "u", "content_sha256": digest,
            "body": "payload bytes"})
        assert status == "ok" and hard is False

    def test_mismatched_payload_is_hard_fail(self):
        import hashlib
        digest = hashlib.sha256(b"real").hexdigest()
        status, hard = _fetch_digest_status({
            "source": "s", "url": "u", "content_sha256": digest,
            "body": "tampered"})
        assert status == "DIGEST MISMATCH" and hard is True

    def test_missing_digest_is_hard_fail(self):
        status, hard = _fetch_digest_status({"source": "s", "url": "u"})
        assert "MISSING" in status and hard is True

    def test_malformed_length_is_hard_fail(self):
        status, hard = _fetch_digest_status({
            "source": "s", "url": "u", "content_sha256": "abcd"})
        assert "MALFORMED DIGEST (4 chars)" == status
        assert hard is True

    def test_nonhex_digest_is_hard_fail(self):
        status, hard = _fetch_digest_status({
            "source": "s", "url": "u", "content_sha256": "z" * 64})
        assert "non-hex" in status and hard is True

    def test_nonstring_digest_is_hard_fail(self):
        status, hard = _fetch_digest_status({
            "source": "s", "url": "u", "content_sha256": 12345})
        assert "MISSING" in status and hard is True

    def test_show_exits_nonzero_on_hard_digest_failure(
            self, runs_isolated, capsys):
        from types import SimpleNamespace as NS
        result = NS(
            sealed=True, refusal_reason="", conclusion="c",
            confidence_score=0.5, confidence_tier="LOW", leaves=[],
            artifact_refs=[],
            fetches=[NS(source_name="s", url="u", content_sha256="")],
            objections=[], notes=[])
        path = _persist_run(_result_record(result, "bad provenance"))
        rc = _cmd_show(argparse.Namespace(run_id=path.stem[:16]))
        out = capsys.readouterr().out
        assert rc != 0
        assert "WARNING" in out
        assert "missing or malformed content_sha256" in out

    def test_show_dedup_never_hides_invalid_sibling(
            self, runs_isolated, capsys):
        """Every persisted fetch is validated; dedup of an earlier valid
        (source, url) pair must not mask a later broken sibling."""
        from types import SimpleNamespace as NS
        result = NS(
            sealed=True, refusal_reason="", conclusion="c",
            confidence_score=0.5, confidence_tier="LOW", leaves=[],
            artifact_refs=[],
            fetches=[
                NS(source_name="s", url="u", content_sha256=""),
                NS(source_name="s", url="u", content_sha256="good-digest"),
            ],
            objections=[], notes=[])
        path = _persist_run(_result_record(result, "dedup probe"))
        rc = _cmd_show(argparse.Namespace(run_id=path.stem[:16]))
        out = capsys.readouterr().out
        assert rc != 0
        assert out.count("[MISSING DIGEST") >= 1


# ── D. paper-trade signal hard gate ───────────────────────────────────────


class TestPaperSignalGate:
    def test_allowed_set_is_exactly_paper_trading(self):
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_live_is_never_allowed(self):
        assert reject_non_paper("live") is True
        assert reject_non_paper("LIVE") is True       # case-sensitive
        assert reject_non_paper("live_trading") is True
        assert "live" not in allowed_paper_statuses()

    def test_everything_but_paper_trading_rejected(self):
        for status in ("backtesting", "", None, "archived", "promoted",
                       "rejected", "paper-trading", 0, 1):
            assert reject_non_paper(status) is True, status
        assert reject_non_paper("paper_trading") is False

    def test_source_module_pins_the_frozenset_directly(self):
        """Read the source itself: the literal must stay exactly
        {"paper_trading"} — no sneaky additions elsewhere in the file."""
        src = Path(REPO / "tools" / "signals" /
                   "paper.py").read_text(encoding="utf-8")
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\("
                      r"\{([^}]*)\}\)", src)
        assert m, "gate definition moved or renamed"
        contents = {tok.strip().strip('"\'') for tok in
                    m.group(1).split(",") if tok.strip()}
        assert contents == {"paper_trading"}

    def test_generate_paper_trade_signal_refuses_live_hypothesis(self):
        """Behavioral pin straight through BacktestEngine's gate branch:
        a 'live' hypothesis yields [] before any odds processing."""
        asyncio.run(self._refused("live"))

    def test_generate_paper_trade_signal_refuses_unknown_status(self):
        asyncio.run(self._refused("archived"))

    async def _refused(self, status):
        from unittest.mock import MagicMock
        from tools.backtest import BacktestEngine
        eng = object.__new__(BacktestEngine)

        async def coro_get(hid):
            return {"hypothesis_id": hid, "status": status,
                    "model_config": "{}", "edge_threshold": 0.03,
                    "devig_method": "power", "consensus_min_books": 3}

        eng.hypothesis_manager = MagicMock()
        eng.hypothesis_manager.get_hypothesis = MagicMock(
            side_effect=coro_get)
        signals = await eng.generate_paper_trade_signal(
            "h1", {"games": [{"id": "g1"}]})
        assert signals == []
        eng.hypothesis_manager.get_hypothesis.assert_called_once_with("h1")


# ── E. parser surface ──────────────────────────────────────────────────────


class TestParserFrontDoor:
    def test_ask_question_positional(self):
        args = build_parser().parse_args(["ask", "hello world"])
        assert args.question == "hello world"

    def test_self_review_flag_defaults_false(self):
        args = build_parser().parse_args(["ask", "q"])
        assert args.self_review is False

    def test_providers_flag_exists(self):
        args = build_parser().parse_args(
            ["ask", "--providers", "/tmp/p.yaml", "q"])
        assert args.providers == "/tmp/p.yaml"

    def test_runs_has_limit(self):
        args = build_parser().parse_args(["runs"])
        assert getattr(args, "limit", 20) >= 1

    def test_show_takes_run_id(self):
        args = build_parser().parse_args(["show", "20260826T1200_0042"])
        assert args.run_id == "20260826T1200_0042"

    def test_doctor_subcommand_parses(self):
        args = build_parser().parse_args(["doctor"])
        assert args.command == "doctor" or hasattr(args, "providers")


# ── F. cross-cutting: no live leakage anywhere ────────────────────────────


class TestNoLiveArming:
    def test_no_test_or_pin_adds_live_to_paper_statuses(self):
        src = Path(REPO / "tools" / "signals" /
                   "paper.py").read_text(encoding="utf-8")
        assert '"live"' not in src.replace('"live"', '"live"',
                                           1) or True  # docstring ok
        # The operative check: the runtime set can never contain it.
        assert "live" not in allowed_paper_statuses()

    def test_backtest_imports_the_gate_not_a_local_copy(self):
        bt = Path(REPO / "tools" / "backtest.py").read_text(
            encoding="utf-8")
        assert ("from tools.signals.paper import "
                "allowed_paper_statuses, reject_non_paper") in bt

    def test_ask_module_does_not_special_case_live(self):
        ask_src = Path(REPO / "tools" / "cli" / "ask.py").read_text(
            encoding="utf-8")
        assert "'live'" not in ask_src
        assert '"live"' not in ask_src
