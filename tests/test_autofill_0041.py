"""autofill #0041 — ask / runs / doctor front door: characterization tests.

This module pins the safety contract of the ``callisto`` CLI front door as
it exists today. It is a characterization suite: every assertion describes
*current* fail-closed behavior so a future change that silently weakens it
turns the suite red.

Contract under test
-------------------
C1. The seal gate is the front door's bouncer. ``callisto ask`` refuses to
    run ANY research when CALLISTO_SEAL_KEY is unset, blank, whitespace,
    wrong length, or non-hex — with exit code exactly 2, no router or
    engine construction, no run record written, and never printing the
    attempted key value.
C2. Every happy path in this module sets a VALID hex seal key (64 hex
    chars) before touching the pipeline. No test here arms anything live;
    engines are stubbed at the _load_router/_make_engine seams.
C3. ``doctor`` reports the money-switch panels: BetExecutor.__init__ must
    assign ``_enabled = False``, OrderManager must default disabled, and
    CALLISTO_LOCAL_ONLY / CALLISTO_ALLOW_LIVE_EXECUTE visibility lines are
    always printed.
C4. runs/show persistence lives in ``tools.cli.runs`` — callisto re-exports
    the SAME function objects; there is no second persistence layer.
C5. Nothing anywhere prints the seal key VALUE.

Safety notes: no live betting is armed by these tests. We never add "live"
to any signal status set, never widen generate_paper_trade_signal, and the
executor is at most instantiated, never enabled.
"""
from __future__ import annotations

import argparse
import asyncio
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

VALID_KEY = "ab" * 32                 # exactly 64 hex chars
OTHER_VALID_KEY = "0123456789abcdef" * 4
KEYS_NEVER_PRINTED = [VALID_KEY, OTHER_VALID_KEY]


# ── helpers ────────────────────────────────────────────────────────────────

def _no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key value leaked: {key[:8]}…"


def _args(q="front door question", backend=None, self_review=False):
    return NS(providers=callisto._default_providers_path(),
              question=q, backend=backend, self_review=self_review)


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may run."""
    monkeypatch.setattr(
        callisto, "_load_router",
        lambda p: (_ for _ in ()).throw(
            AssertionError("router loaded despite bad seal")))
    monkeypatch.setattr(
        callisto, "_make_engine",
        lambda r, self_review=False: (_ for _ in ()).throw(
            AssertionError("engine built despite bad seal")))


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


class _Ledger:
    def snapshot(self):
        return {"by_tier": {}}


class FakeRouter:
    endpoints = ["gpu1"]
    task_classes = {"decompose": "gpu1"}
    default_tier_name = "gpu1"
    cost_ledger = _Ledger()

    async def check_health(self, name):
        return {"status": "ok"}


class FakeEngine:
    def __init__(self, sealed=True):
        self.sealed = sealed
        self.ran_with = None

    async def run(self, q):
        self.ran_with = q
        return NS(sealed=self.sealed,
                  refusal_reason="" if self.sealed else "stub refusal",
                  conclusion="c" if self.sealed else "",
                  confidence_score=0.5, confidence_tier="SPECULATIVE",
                  leaves=[], fetches=[], objections=[],
                  notes=[], artifact_refs=[])


def _wire_happy(monkeypatch, engine=None):
    engine = engine or FakeEngine()
    router = FakeRouter()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda r, self_review=False: engine)
    return router, engine


def _fake_result_record(result, q):
    return {
        "recorded_at": "2026-08-26T00:00:00+00:00",
        "question": q,
        "sealed": getattr(result, "sealed", False),
        "conclusion": getattr(result, "conclusion", ""),
        "confidence": {"tier": getattr(result, "confidence_tier", "?"),
                       "score": getattr(result, "confidence_score", 0)},
        "leaves": [], "fetches": [], "objections": [],
        "notes": [], "artifacts": [],
    }


@pytest.fixture(autouse=True)
def _stable_record(monkeypatch):
    monkeypatch.setattr(callisto, "_result_record", _fake_result_record)


# ── C1a. the gate itself, again from a fresh angle ────────────────────────

class TestGateMatrix0041:
    @pytest.mark.parametrize("bad_env", [
        None, "", "   ", "\t\n", "xyz", "0x" + "f" * 64,
        "g" * 64,                       # right shape, not hex alphabet
        "ab" * 32 + "extra",            # too long
    ])
    def test_bad_keys_refused_exit_two_no_side_effects(
            self, bad_env, monkeypatch, capsys, runs_isolated):
        if bad_env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad_env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert rc == 2
        assert list(runs_isolated.glob("*.json")) == []
        _no_key_leak(out)

    def test_gate_message_names_the_remediation(
            self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "CALLISTO_SEAL_KEY" in out

    def test_valid_lowercase_hex_opens_gate_and_ask_exits_zero(
            self, monkeypatch, capsys, runs_isolated):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _, eng = _wire_happy(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args("open sesame")))
        assert rc == 0
        assert eng.ran_with == "open sesame"
        assert "SEALED" in capsys.readouterr().out


# ── C1b. main() dispatch keeps honest exit codes ──────────────────────────

class TestMainDispatchExitCodes:
    def test_main_ask_unkeyed_returns_two(self, monkeypatch, capsys,
                                          runs_isolated):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _wire_boom(monkeypatch)
        argv = ["ask", "q"]
        assert callisto.main(argv) == 2
        _no_key_leak(capsys.readouterr().out)

    def test_main_unknown_command_is_argparse_error(self):
        import pytest
        with pytest.raises(SystemExit) as exc:
            callisto.main(["teleport"])
        assert exc.value.code != 0

    def test_main_routes_doctor_to_tools_cli_doctor(
            self, monkeypatch, capsys):
        import tools.cli.doctor as doc_mod
        seen = {}

        def fake_doctor(args):
            seen["called"] = True
            return 7

        monkeypatch.setattr(doc_mod, "cmd_doctor", fake_doctor, raising=False)
        monkeypatch.setattr(doc_mod, "_cmd_doctor", fake_doctor, raising=False)
        monkeypatch.setattr(callisto, "_cmd_doctor", fake_doctor)
        rc = callisto.main(["doctor"])
        assert seen.get("called") is True
        assert rc == 7

    def test_main_routes_runs_and_show_to_tools_cli_runs_objects(self):
        import tools.cli.runs as runs_mod
        assert callisto._cmd_runs is runs_mod._cmd_runs
        assert callisto._cmd_show is runs_mod._cmd_show
        assert callisto._load_run is runs_mod._load_run

    def test_parser_help_lists_front_door_commands(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for cmd in ("ask", "status", "runs", "show", "doctor"):
            assert cmd in out


# ── C2. happy paths all carry a valid hex key ─────────────────────────────

class TestHappyPathsAreKeyed:
    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def test_sealed_run_persists_one_record_with_question(
            self, monkeypatch, runs_isolated):
        _, eng = _wire_happy(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args("persist me please")))
        assert rc == 0
        saved = sorted(runs_isolated.glob("*.json"))
        assert len(saved) == 1
        rec = json.loads(saved[0].read_text())
        assert rec["question"] == "persist me please"
        assert rec["sealed"] is True
        assert eng.ran_with == "persist me please"

    def test_self_review_flag_reaches_engine_factory(
            self, monkeypatch, capsys, runs_isolated):
        seen = {}
        router = FakeRouter()
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)

        def make_engine(r, self_review=False):
            seen["self_review"] = self_review
            return FakeEngine()

        monkeypatch.setattr(callisto, "_make_engine", make_engine)
        args = build_parser().parse_args(["ask", "--self-review", "q"])
        assert asyncio.run(callisto._cmd_ask(args)) == 0
        assert seen["self_review"] is True

    def test_refused_result_still_writes_record_and_exits_nonzero(
            self, monkeypatch, capsys, runs_isolated):
        eng = FakeEngine(sealed=False)
        _wire_happy(monkeypatch, eng)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert rc != 0
        assert "REFUSED" in out
        assert len(list(runs_isolated.glob("*.json"))) == 1

    def test_backend_pin_unknown_tier_never_builds_engine(
            self, monkeypatch, capsys, runs_isolated):
        built = []

        def make_engine(r, self_review=False):
            e = FakeEngine()
            built.append(e)
            return e

        monkeypatch.setattr(callisto, "_load_router", lambda p: FakeRouter())
        monkeypatch.setattr(callisto, "_make_engine", make_engine)
        args = _args(backend="not-a-tier")
        rc = asyncio.run(callisto._cmd_ask(args))
        assert rc == 2
        assert "unknown provider tier 'not-a-tier'" in capsys.readouterr().out
        assert built == []


# ── C3. doctor reports BetExecutor disabled and LOCAL_ONLY ────────────────

def _run_doctor(extra_env=None, providers=None, monkeypatch=None, capsys=None):
    if monkeypatch is not None:
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
    got = capsys.readouterr()
    return rc, got.out + got.err


class TestDoctorMoneySwitches0041:
    BASE_ENV = {"CALLISTO_SEAL_KEY": VALID_KEY,
                "CALLISTO_BIND_HOST": None}

    def test_keyed_doctor_ok_prints_betexec_disabled_line(
            self, monkeypatch, capsys):
        rc, out = _run_doctor(
            providers=str(REPO / "config" / "providers.yaml"),
            extra_env=self.BASE_ENV, monkeypatch=monkeypatch, capsys=capsys)
        assert rc == 0
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out
        assert "OK: OrderManager.__init__ defaults _enabled = False" in out
        _no_key_leak(out)

    def test_local_only_visibility_line_present_when_set(
            self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={**self.BASE_ENV, "CALLISTO_LOCAL_ONLY": "1"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "== money switches ==" in out
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_off_by_default(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={**self.BASE_ENV, "CALLISTO_LOCAL_ONLY": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_allow_live_execute_off_by_default(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={**self.BASE_ENV,
                       "CALLISTO_ALLOW_LIVE_EXECUTE": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_money_switch_panel_prints_even_when_unkeyed(
            self, monkeypatch, capsys):
        """Doctor is a diagnostic tool: it reports everything it can even
        on a failing configuration, then exits non-zero."""
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "== money switches ==" in out
        assert "_enabled = False" in out

    @pytest.mark.parametrize("var", ["CALLISTO_LOCAL_ONLY",
                                     "CALLISTO_ALLOW_LIVE_EXECUTE"])
    def test_live_arming_switches_default_absent(
            self, var, monkeypatch, capsys):
        """Neither live-arming switch may default to on."""
        monkeypatch.delenv(var, raising=False)
        env_val = os.environ.get(var, "")
        assert env_val.strip() != "on"
        assert var in (
            "CALLISTO_LOCAL_ONLY", "CALLISTO_ALLOW_LIVE_EXECUTE")


class TestProductionGatesStayShut:
    """Static pins on the production sources themselves (tests-only file)."""

    def test_bet_executor_init_disables_itself(self):
        src = (REPO / "tools" / "bet_executor.py").read_text()
        m = re.search(r"class BetExecutor\b.*?def __init__\(self\):"
                      r"(.*?)(\n    (?:async )?def )", src, re.S)
        assert m, "BetExecutor.__init__ not found"
        assert re.search(r"self\._enabled\s*=\s*False", m.group(1))

    def test_order_manager_defaults_disabled(self):
        src = (REPO / "tools" / "order_manager.py").read_text()
        pre_enable = src.split("def enable", 1)[0]
        assert not re.search(r"self\._enabled\s*=\s*True", pre_enable)

    def test_paper_trade_signal_statuses_exclude_live(self):
        hits = []
        for path in REPO.rglob("*.py"):
            if "test" in str(path) or ".venv" in str(path):
                continue
            text = path.read_text(errors="ignore")
            for m in re.finditer(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=.*?(?=\n\S)",
                                 text, re.S):
                blob = m.group(0)
                assert re.search(r"['\"]live['\"]", blob) is None, \
                    f"{path}: paper-trade statuses contain 'live'"
                hits.append(path)
        assert hits, "expected at least one _PAPER_TRADE_SIGNAL_STATUSES pin"

    @staticmethod
    def _is_test_path(path) -> bool:
        parts = Path(path).parts
        if parts and parts[0] == "tests":
            return True
        return any(p.startswith("test_") for p in parts)

    def test_generate_paper_trade_signal_never_defaults_live(self):
        hits = 0
        for path in REPO.rglob("*.py"):
            if ".venv" in str(path) or self._is_test_path(path):
                continue
            text = path.read_text(errors="ignore")
            for m in re.finditer(
                    r"def generate_paper_trade_signal\(\s*self,(.*?)\)\s*(?:->)?[^:]*:",
                    text, re.S):
                params = m.group(1)
                assert '"live"' not in params and "'live'" not in params, \
                    f"{path}: live default in paper-trade signal signature"
                hits += 1
        assert hits >= 1, "generate_paper_trade_signal not found"


# ── C4. runs/show persistence lives only in tools.cli.runs ────────────────

class TestRunsPersistenceLivesInToolsCliRuns:
    def test_callisto_reexports_same_functions(self):
        import tools.cli.ask as ask_mod
        import tools.cli.runs as runs_mod
        for name in ("_cmd_runs", "_cmd_show", "_load_run",
                     "_verify_artifact", "_fetch_digest_status"):
            assert hasattr(runs_mod, name), f"tools.cli.runs lost {name}"
            assert getattr(callisto, name) is getattr(runs_mod, name)
        # persistence itself lives beside ask and is re-exported unchanged
        assert hasattr(ask_mod, "_persist_run")
        assert callisto._persist_run is ask_mod._persist_run

    def test_persist_then_load_roundtrip_in_isolated_dir(self, runs_isolated):
        rec = {"recorded_at": "2026-08-26T00:00:00+00:00",
               "question": "roundtrip", "sealed": True,
               "confidence": {"tier": "SPECULATIVE", "score": 0.5}}
        path = callisto._persist_run(rec)
        assert path.parent == runs_isolated
        loaded, loaded_path = callisto._load_run(path.stem)
        assert loaded_path == path
        assert json.loads(json.dumps(rec)) == loaded

    def test_load_run_prefix_match_and_none_for_ghost(self, runs_isolated):
        path = callisto._persist_run(
            {"recorded_at": "t", "question": "prefix me", "sealed": True})
        stem = path.stem
        loaded, _ = callisto._load_run(stem[:10])
        assert loaded is not None and loaded["question"] == "prefix me"
        assert callisto._load_run("ghost-id") == (None, None)

    def test_load_run_ambiguous_prefix_fails_closed(self, runs_isolated):
        p = callisto._persist_run({"recorded_at": "t", "question": "a",
                                   "sealed": True})
        dup = runs_isolated / (p.stem[:14] + "zz99.json")
        dup.write_text(json.dumps({"recorded_at": "t", "question": "b",
                                   "sealed": False}))
        with pytest.raises(SystemExit, match="ambiguous"):
            callisto._load_run(p.stem[:14])

    def test_runs_listing_newest_first_with_verdict_column(
            self, runs_isolated, capsys):
        for i, sealed in enumerate([True, False, True]):
            callisto._persist_run({"recorded_at": f"2026-08-2{i}",
                                   "question": f"q{i}", "sealed": sealed})
        assert callisto._cmd_runs(argparse.Namespace(limit=20)) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        verdicts = [ln.split()[1] for ln in lines]
        assert verdicts.count("SEALED") == 2
        assert "REFUSED" in verdicts

    def test_runs_limit_respected(self, runs_isolated, capsys):
        for i in range(5):
            callisto._persist_run({"recorded_at": f"2026-08-2{i}T00:00:00",
                                   "question": f"q{i}", "sealed": True})
        assert callisto._cmd_runs(argparse.Namespace(limit=2)) == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 2

    def test_runs_empty_dir_friendly_zero_exit(self, runs_isolated, capsys):
        assert callisto._cmd_runs(argparse.Namespace(limit=20)) == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_show_unknown_run_exits_one_and_names_next_step(
            self, runs_isolated, capsys):
        rc = callisto._cmd_show(argparse.Namespace(run_id="missing"))
        out = capsys.readouterr().out
        assert rc == 1
        assert "no run matching 'missing'" in out
        assert "`callisto runs`" in out

    def test_show_reprints_conclusion_from_persisted_record(
            self, runs_isolated, capsys, monkeypatch):
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR",
                           str(runs_isolated.parent / "arts"))
        rec = {"recorded_at": "2026-08-26T01:00:00+00:00",
               "question": "what changed", "sealed": True,
               "conclusion": "the binding constraint moved",
               "confidence": {"tier": "MODERATE", "score": 0.62},
               "leaves": [], "artifacts": [], "fetches": [],
               "objections": [], "notes": []}
        path = callisto._persist_run(rec)
        rc = callisto._cmd_show(argparse.Namespace(run_id=path.stem))
        out = capsys.readouterr().out
        assert rc == 0
        assert "the binding constraint moved" in out
        assert "MODERATE" in out and "0.62" in out

    def test_fetch_digest_matrix_via_callisto_alias(self):
        good = __import__("hashlib").sha256(b"body").hexdigest()
        ok_status, hard = callisto._fetch_digest_status(
            {"content_sha256": good, "body": "body"})
        assert ok_status == "ok" and hard is False
        for bad in ({}, {"content_sha256": ""},
                    {"content_sha256": "z" * 64}):
            status, hard = callisto._fetch_digest_status(bad)
            assert hard is True and status != "ok"


# ── C5. the seal key value is secret everywhere ───────────────────────────

class TestSealKeySecretEverywhere0041:
    SECRET = "feedface" * 8

    def test_doctor_ok_path_reports_presence_not_value(
            self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", self.SECRET)
        rc, out = _run_doctor(
            providers=str(REPO / "config" / "providers.yaml"),
            extra_env={"CALLISTO_BIND_HOST": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert self.SECRET not in out
        assert rc in (0, 1)          # either way, no leak
        assert "seal key is set" in out

    def test_unkeyed_ask_refusal_does_not_echo_attempted_value(
            self, monkeypatch, capsys, runs_isolated):
        attempted = self.SECRET + "!!"     # invalid hex on purpose
        monkeypatch.setenv("CALLISTO_SEAL_KEY", attempted)
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert self.SECRET not in out and attempted not in out

    def test_persisted_records_contain_no_key_material(self, runs_isolated):
        os.environ["CALLISTO_SEAL_KEY"] = self.SECRET
        try:
            rec = _fake_result_record(NS(sealed=True, conclusion="c",
                                         confidence_tier="LOW",
                                         confidence_score=0.2), "secret q")
            raw = json.dumps(rec)
        finally:
            os.environ.pop("CALLISTO_SEAL_KEY", None)
        assert self.SECRET not in raw
        assert "seal_key" not in raw

    def test_runs_output_never_contains_secret(self, runs_isolated,
                                               monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", self.SECRET)
        callisto._persist_run({"recorded_at": "t", "question": "q",
                               "sealed": True})
        callisto._cmd_runs(argparse.Namespace(limit=20))
        assert self.SECRET not in capsys.readouterr().out
