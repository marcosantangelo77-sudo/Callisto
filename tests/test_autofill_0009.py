"""autofill #0009 — characterization: ask / runs / doctor front door.

A second, independent pin over the callisto CLI surface so that any drift
in the safety contract shows up red even if the original characterization
modules are edited or removed. Everything here describes CURRENT behavior:

1. The seal gate is fail-closed: `callisto ask` refuses to run ANY research
   when CALLISTO_SEAL_KEY is unset, blank, whitespace-only, or non-hex,
   exiting with code 2 BEFORE constructing a router or engine. Happy paths
   in this module always set a valid 64-hex-char key.
2. `callisto doctor` reports the money-switch panel: OrderManager and
   BetExecutor must both default _enabled = False, CALLISTO_LOCAL_ONLY is
   surfaced as on/off, and CALLISTO_ALLOW_LIVE_EXECUTE visibility is pinned.
3. `callisto runs` / `callisto show` persistence lives in tools.cli.runs:
   newest-first listing, friendly empty state, prefix lookup with ambiguity
   refusal, artifact re-hash verification, and fetch digest honesty.
4. The seal-key VALUE is secret on every path — refusals name the problem,
   never print the attempted key.

Tests-only module: no production gate is touched. Nothing here arms live
betting; "live" never appears in any paper-trade signal status this module
asserts about.
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
from tools.cli.ask import check_seal_key as ask_check_seal_key  # noqa: E402
from tools.cli.ask import _persist_run  # noqa: E402
from tools.cli.runs import (  # noqa: E402
    _cmd_runs,
    _cmd_show,
    _fetch_digest_status,
    _load_run,
    _verify_artifact,
)

VALID_KEY = "ab" * 32                    # exactly 64 hex chars
OTHER_VALID_KEY = "0f" * 32              # distinct valid hex for leak checks


# ── shared fixtures & helpers ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    """Every test starts with no seal key unless it sets one explicitly."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    yield


@pytest.fixture
def runs_env(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    return d


@pytest.fixture
def valid_key(monkeypatch) -> str:
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    return VALID_KEY


def assert_no_leak(out: str) -> None:
    """The seal key value must never appear in printed output."""
    for candidate in (VALID_KEY, OTHER_VALID_KEY, "f00d" * 16):
        assert candidate not in out, (
            f"seal key value leaked: {candidate[:8]}…")


def boom_wiring(monkeypatch):
    """Post-gate seams that explode: nothing may run past the seal gate."""
    def _fail(name):
        def f(*a, **k):  # pragma: no cover - must never execute
            raise AssertionError(f"{name} ran despite unkeyed seal")
        return f
    monkeypatch.setattr(callisto, "_load_router", _fail("_load_router"))
    monkeypatch.setattr(callisto, "_make_engine", _fail("_make_engine"))
    monkeypatch.setattr(callisto, "_result_record", _fail("_result_record"))


class _Ledger:
    def snapshot(self):
        return {"by_tier": {"gpu1": {"calls": 1}}}


class FakeRouter:
    endpoints = ["gpu1"]
    task_classes = {"decompose": "gpu1"}
    default_tier_name = "gpu1"
    cost_ledger = _Ledger()

    async def check_health(self, tier):
        return {"status": "ok"}


def make_fake_result(sealed=True):
    from tools.artifacts import ArtifactRef
    return NS(
        sealed=sealed,
        refusal_reason="" if sealed else "insufficient corroboration",
        conclusion=("Sealed finding." if sealed else ""),
        confidence_score=0.42,
        confidence_tier="SPECULATIVE",
        leaves=[NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=[ArtifactRef(sha256="c" * 64, kind="csv",
                                   name="data.csv")],
        fetches=[NS(source_name="openalex", url="https://api.openalex.org/x",
                    content_sha256="d" * 64)],
        objections=[NS(text="single source")],
        notes=["note one"],
    )


def wire_pipeline(monkeypatch, result=None, reached=None):
    router = FakeRouter()
    engine = NS(adversary_router=None)

    async def run(q):
        if reached is not None:
            reached["question"] = q
        return result if result is not None else make_fake_result()

    engine.run = run
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda r, self_review=False: engine)
    return router, engine


def ask_args(q="char question"):
    return argparse.Namespace(
        providers=callisto._default_providers_path(), backend=None,
        question=q, self_review=False)


def run_doctor(monkeypatch, capsys, extra_env=None,
               providers=None) -> tuple[int, str]:
    for k, v in (extra_env or {}).items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    argv = ["doctor"]
    if providers:
        argv += ["--providers", providers]
    rc = callisto._cmd_doctor(build_parser().parse_args(argv))
    got = capsys.readouterr()
    return rc, got.out + got.err


# ── 1. the seal gate itself ────────────────────────────────────────────────

class TestSealGateRefusals:
    """check_seal_key is fail-closed on everything but valid hex."""

    @pytest.mark.parametrize("bad", [
        None, "", "   ", "\t\n", "zz" * 32, "banana", "ab" * 31 + "!!",
        "0x" + "a" * 64, "abc", "-1",
    ])
    def test_every_bad_shape_refused(self, bad, capsys):
        if bad is not None:
            os.environ["CALLISTO_SEAL_KEY"] = bad
            try:
                bytes.fromhex(bad)
                pytest.skip("shape unexpectedly valid hex")
            except ValueError:
                pass
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert out.startswith("FAIL:")

    def test_missing_names_unkeyed(self, capsys):
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "not set" in out
        assert "unkeyed" in out

    def test_nonhex_says_not_valid_hex_and_never_echoes_value(self, capsys):
        attempted = "deadbeef" * 8
        os.environ["CALLISTO_SEAL_KEY"] = attempted + "xyz"
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "not valid hex" in out
        assert attempted not in out

    def test_valid_hex_accepted(self, valid_key, capsys):
        assert check_seal_key() is True
        assert capsys.readouterr().out == ""

    def test_whitespace_stripped_before_validation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f" {VALID_KEY}\t")
        assert check_seal_key() is True

    def test_entry_module_alias_is_same_function(self):
        """tools.cli.ask.check_seal_key and callisto's export agree."""
        assert ask_check_seal_key is check_seal_key


# ── 2. ask fails closed with exit 2 before any research ───────────────────

class TestAskExitTwoOnUnkeyed:
    @pytest.fixture(autouse=True)
    def _boom(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
        boom_wiring(monkeypatch)

    @pytest.mark.parametrize("key_env", [None, "", " ", "nothex", "0x1234"])
    def test_exit_two_and_no_side_effects(self, key_env, monkeypatch, capsys):
        if key_env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", key_env)
        rc = asyncio.run(callisto._cmd_ask(ask_args()))
        out = capsys.readouterr().out
        assert rc == 2
        assert "FAIL" in out
        assert list(callisto._runs_dir().glob("*")) == []

    def test_attempted_invalid_key_not_printed(self, monkeypatch, capsys):
        leaked_candidate = "f00d" * 16
        monkeypatch.setenv("CALLISTO_SEAL_KEY", leaked_candidate + "zz")
        asyncio.run(callisto._cmd_ask(ask_args()))
        assert_no_leak(capsys.readouterr().out)

    def test_refusal_precedes_backend_pinning(self, monkeypatch, capsys):
        """Even an unknown --backend cannot matter: the seal gate fires first."""
        args = argparse.Namespace(
            providers=callisto._default_providers_path(),
            backend="nonexistent-tier", question="q", self_review=False)
        rc = asyncio.run(callisto._cmd_ask(args))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier" not in out


# ── 3. happy paths: keyed gate opens, research runs ───────────────────────

class TestAskHappyPathKeyed:
    def test_sealed_run_exits_zero_and_persists(
            self, valid_key, runs_env, monkeypatch, capsys):
        reached = {}
        wire_pipeline(monkeypatch, reached=reached)
        rc = asyncio.run(callisto._cmd_ask(ask_args("the real question")))
        out = capsys.readouterr().out
        assert rc == 0
        assert reached["question"] == "the real question"
        assert "SEALED" in out
        saved = list(runs_env.glob("*.json"))
        assert len(saved) == 1
        rec = json.loads(saved[0].read_text())
        assert rec["sealed"] is True
        assert rec["question"] == "the real question"
        assert rec["confidence"]["tier"] == "SPECULATIVE"
        assert_no_leak(out)

    def test_run_id_filename_shape(self, valid_key, runs_env, monkeypatch):
        wire_pipeline(monkeypatch)
        asyncio.run(callisto._cmd_ask(ask_args()))
        name = next(runs_env.glob("*.json")).name
        assert re.match(r"^\d{8}T\d{6}[+\-]\d{4}_\d{4}\.json$", name)

    def test_refused_result_ran_but_exits_nonzero(
            self, valid_key, runs_env, monkeypatch, capsys):
        reached = {}
        wire_pipeline(monkeypatch,
                      result=make_fake_result(sealed=False), reached=reached)
        rc = asyncio.run(callisto._cmd_ask(ask_args()))
        out = capsys.readouterr().out
        assert reached["question"] == "char question"     # pipeline ran
        assert rc != 0
        assert "REFUSED" in out
        assert len(list(runs_env.glob("*.json"))) == 1    # still persisted

    def test_unknown_backend_after_gate_exits_two(
            self, valid_key, runs_env, monkeypatch, capsys):
        wire_pipeline(monkeypatch)
        args = argparse.Namespace(
            providers=callisto._default_providers_path(),
            backend="ghost", question="q", self_review=False)
        rc = asyncio.run(callisto._cmd_ask(args))
        assert rc == 2
        assert "unknown provider tier 'ghost'" in capsys.readouterr().out

    def test_self_review_flag_propagates_to_engine(
            self, valid_key, runs_env, monkeypatch):
        seen = {}
        router = FakeRouter()

        def make_engine(r, self_review=False):
            seen["self_review"] = self_review

            async def run(q):
                return make_fake_result()
            return NS(run=run, adversary_router=None)

        monkeypatch.setattr(callisto, "_load_router", lambda p: router)
        monkeypatch.setattr(callisto, "_make_engine", make_engine)
        args = argparse.Namespace(
            providers=callisto._default_providers_path(), backend=None,
            question="q", self_review=True)
        assert asyncio.run(callisto._cmd_ask(args)) == 0
        assert seen["self_review"] is True

    def test_persistence_goes_through_tools_cli_ask_module(
            self, valid_key, runs_env, monkeypatch):
        """The run record is written into the dir resolved by
        tools.cli.ask._runs_dir — the same store `runs`/`show` read."""
        import tools.cli.ask as ask_mod
        assert ask_mod._runs_dir() == runs_env.resolve() or \
            str(ask_mod._runs_dir()) == str(runs_env)
        wire_pipeline(monkeypatch)
        asyncio.run(callisto._cmd_ask(ask_args()))
        assert len(list(runs_env.glob("*.json"))) == 1


# ── 4. doctor: money switches panel ───────────────────────────────────────

class TestDoctorMoneySwitches:
    def test_panel_present_with_both_executors_disabled(
            self, monkeypatch, capsys, valid_key):
        rc, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_LOCAL_ONLY": None})
        assert "== money switches ==" in out
        assert "OK: OrderManager.__init__ defaults _enabled = False" in out
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out
        assert "FAIL: BetExecutor" not in out
        assert rc == 0

    def test_local_only_on_reported(self, monkeypatch, capsys, valid_key):
        _, out = run_doctor(
            monkeypatch, capsys, extra_env={"CALLISTO_LOCAL_ONLY": "1"})
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_unset_reports_off(self, monkeypatch, capsys,
                                          valid_key):
        _, out = run_doctor(
            monkeypatch, capsys, extra_env={"CALLISTO_LOCAL_ONLY": None})
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_allow_live_execute_off_by_default(self, monkeypatch, capsys,
                                               valid_key):
        _, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_ALLOW_LIVE_EXECUTE": None})
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_allow_live_execute_visibility_when_set(
            self, monkeypatch, capsys, valid_key):
        _, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_ALLOW_LIVE_EXECUTE": "yes"})
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: on" in out

    def test_production_sources_actually_default_disabled(self):
        """Direct source pin: the gates doctor checks really hold today."""
        om = Path(__import__("tools.order_manager",
                             fromlist=["x"]).__file__).read_text()
        be = Path(__import__("tools.bet_executor",
                             fromlist=["x"]).__file__).read_text()
        m_om = re.search(r"def __init__\(self[^)]*\):(.*?)(\n    def )",
                         om, re.S)
        assert m_om, "OrderManager.__init__ not found"
        assert re.search(r"self\._enabled\s*=\s*False", m_om.group(1))
        m = re.search(r"class BetExecutor\b.*?def __init__\(self\):(.*?)"
                      r"(\n    (?:async )?def )", be, re.S)
        assert m, "BetExecutor.__init__ not found"
        assert re.search(r"self\._enabled\s*=\s*False", m.group(1))


# ── 5. doctor: seal / bind panels stay fail-closed ────────────────────────

class TestDoctorPanels0009:
    def test_unkeyed_doctor_fails_with_rc_one(self, monkeypatch, capsys):
        rc, out = run_doctor(monkeypatch, capsys,
                             extra_env={"CALLISTO_SEAL_KEY": None})
        assert rc != 0
        assert "unkeyed" in out
        assert "PROBLEMS FOUND" in out
        assert_no_leak(out)

    def test_nonhex_doctor_fails(self, monkeypatch, capsys):
        rc, out = run_doctor(monkeypatch, capsys,
                             extra_env={"CALLISTO_SEAL_KEY": "hexish"})
        assert rc != 0
        assert "not valid hex" in out
        assert_no_leak(out)

    def test_keyed_doctor_ok_panels(self, monkeypatch, capsys):
        rc, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_SEAL_KEY": OTHER_VALID_KEY},
            providers=str(REPO / "config" / "providers.yaml"))
        assert rc == 0
        assert "doctor: OK" in out
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "HMAC-SHA256" in out
        assert OTHER_VALID_KEY not in out

    def test_wildcard_bind_fails_closed(self, monkeypatch, capsys):
        rc, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_BIND_HOST": "0.0.0.0"})
        assert rc != 0
        assert "0.0.0.0" in out
        assert_no_leak(out)

    def test_ipv6_any_bind_fails_closed(self, monkeypatch, capsys):
        rc, out = run_doctor(
            monkeypatch, capsys,
            extra_env={"CALLISTO_BIND_HOST": "::"})
        assert rc != 0

    def test_loopback_default_ok(self, monkeypatch, capsys, valid_key):
        _, out = run_doctor(
            monkeypatch, capsys, extra_env={"CALLISTO_BIND_HOST": None})
        assert "host: 127.0.0.1" in out
        assert "loopback default" in out

    def test_doctor_reports_money_switches_even_when_unkeyed(
            self, monkeypatch, capsys):
        """Diagnosis continues past the first failure — every panel prints."""
        _, out = run_doctor(monkeypatch, capsys,
                            extra_env={"CALLISTO_SEAL_KEY": None})
        assert "== money switches ==" in out
        assert "BetExecutor.__init__ assigns _enabled = False" in out


# ── 6. runs / show persistence (tools.cli.runs) ───────────────────────────

class TestRunsListing:
    def test_empty_state_friendly_zero_exit(self, runs_env, capsys):
        assert _cmd_runs(build_parser().parse_args(["runs"])) == 0
        assert "no saved runs yet" in capsys.readouterr().out

    def test_listing_newest_first_with_verdict_column(
            self, runs_env, capsys):
        for i in range(3):
            rec = callisto._result_record(make_fake_result(sealed=i % 2 == 0),
                                          f"question {i}")
            _persist_run(rec)
        assert _cmd_runs(build_parser().parse_args(["runs"])) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        ids = [ln.split()[0] for ln in lines]
        assert ids == sorted(ids, reverse=True)
        verdicts = [ln.split()[1] for ln in lines]
        assert set(verdicts) <= {"SEALED", "REFUSED"}

    def test_limit_flag_caps_rows(self, runs_env, capsys):
        for i in range(4):
            _persist_run(callisto._result_record(make_fake_result(), f"q{i}"))
        _cmd_runs(build_parser().parse_args(["runs", "--limit", "2"]))
        assert len(capsys.readouterr().out.strip().splitlines()) == 2

    def test_unreadable_record_reported_not_crashed(self, runs_env, capsys):
        runs_env.mkdir(parents=True, exist_ok=True)
        (runs_env / "broken.json").write_text("{not json")
        assert _cmd_runs(build_parser().parse_args(["runs"])) == 0
        assert "unreadable" in capsys.readouterr().out


class TestShowLookup:
    def test_unknown_id_exits_one(self, runs_env, capsys):
        assert _cmd_show(build_parser().parse_args(["show", "zzz"])) == 1
        assert "no run matching" in capsys.readouterr().out

    def test_prefix_lookup_resolves_unique(self, runs_env, capsys):
        path = _persist_run(callisto._result_record(make_fake_result(), "q"))
        stem = path.stem
        assert _cmd_show(build_parser().parse_args(["show", stem[:12]])) == 0
        assert "SEALED" in capsys.readouterr().out

    def test_ambiguous_prefix_raises_systemexit(self, runs_env):
        base = callisto._result_record(make_fake_result(), "dup-a")
        other = callisto._result_record(make_fake_result(), "dup-b")
        # force identical timestamps so only the hash suffix differs
        stamp = base["recorded_at"].replace(":", "").replace("-", "")
        (runs_env.mkdir(parents=True, exist_ok=True))
        (runs_env / f"{stamp}_0001.json").write_text(json.dumps(base))
        (runs_env / f"{stamp}_0100.json").write_text(json.dumps(other))
        with pytest.raises(SystemExit, match="ambiguous"):
            _load_run(stamp[:16])

    def test_missing_artifact_shown_honestly(self, runs_env, capsys):
        path = _persist_run(callisto._result_record(make_fake_result(), "q"))
        rc = _cmd_show(build_parser().parse_args(["show", path.stem]))
        out = capsys.readouterr().out
        assert rc == 0
        assert ("missing" in out) or ("unverifiable" in out)

    def test_verified_artifact_reprinted_with_ok(self, runs_env, tmp_path,
                                                 monkeypatch, capsys):
        payload = b"artifact-bytes-0009"
        digest = hashlib.sha256(payload).hexdigest()
        monkeypatch.setenv("CALLISTO_ARTIFACT_DIR", str(tmp_path / "arts"))
        from tools.artifacts import ArtifactStore
        ArtifactStore(root=tmp_path / "arts").put(payload, kind="csv",
                                                  name="data.csv")
        rec = callisto._result_record(make_fake_result(), "q")
        rec["artifacts"][0]["sha256"] = digest
        path = _persist_run(rec)
        rc = _cmd_show(build_parser().parse_args(["show", path.stem]))
        out = capsys.readouterr().out
        assert rc == 0
        assert "[ok" in out
        assert "Sealed finding." in out          # conclusion reprinted


class TestFetchDigestStatus:
    """Pin tools.cli.runs._fetch_digest_status honesty contract."""

    def test_missing_digest_hard_fails(self):
        status, hard = _fetch_digest_status({})
        assert hard is True
        assert "MISSING" in status

    def test_wrong_length_hard_fails(self):
        status, hard = _fetch_digest_status({"content_sha256": "ab" * 10})
        assert hard is True
        assert "MALFORMED" in status

    def test_non_hex_hard_fails(self):
        status, hard = _fetch_digest_status({"content_sha256": "z" * 64})
        assert hard is True
        assert "MALFORMED" in status

    def test_matching_body_verifies(self):
        body = b"remote payload"
        digest = hashlib.sha256(body).hexdigest()
        status, hard = _fetch_digest_status(
            {"content_sha256": digest, "body": "remote payload"})
        assert hard is False
        assert status == "ok"

    def test_mismatched_body_hard_fails(self):
        digest = hashlib.sha256(b"real").hexdigest()
        status, hard = _fetch_digest_status(
            {"content_sha256": digest, "body": "forged"})
        assert hard is True
        assert "MISMATCH" in status

    def test_syntax_only_digest_soft_unverified(self):
        status, hard = _fetch_digest_status({"content_sha256": "e" * 64})
        assert hard is False
        assert "unverified" in status


class TestPersistLoadRoundtrip:
    def test_roundtrip_preserves_full_record(self, runs_env):
        rec = callisto._result_record(make_fake_result(), "round trip")
        path = _persist_run(rec)
        loaded, loaded_path = _load_run(path.stem)
        assert loaded_path == path
        assert json.loads(json.dumps(rec)) == loaded

    def test_atomic_write_leaves_no_tmp_files(self, runs_env):
        for i in range(5):
            _persist_run(callisto._result_record(make_fake_result(),
                                                 f"atomic {i}"))
        assert list(runs_env.glob("*.tmp")) == []
        assert len(list(runs_env.glob("*.json"))) == 5

    def test_load_run_no_match_returns_none_none(self, runs_env):
        assert _load_run("does-not-exist") == (None, None)

    def test_verify_artifact_status_values(self):
        assert _verify_artifact("a" * 64).startswith(("missing",
                                                      "unverifiable"))


# ── 7. static safety pins ─────────────────────────────────────────────────

class TestStaticSafetyPins:
    def test_paper_trade_signal_statuses_exclude_live(self):
        """Red line: 'live' must never be an armed paper-trade status."""
        import tools.signals.paper as paper_mod
        src = Path(paper_mod.__file__).read_text(encoding="utf-8")
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\((.*?)\)",
            src, re.S)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES constant not found"
        statuses = re.findall(r"[\"']([^\"']+)[\"']", m.group(1))
        assert statuses, "signal statuses empty"
        assert "live" not in [s.strip().lower() for s in statuses]
        assert paper_mod.allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_generate_paper_trade_signal_method_exists(self):
        """The gate exists as a BacktestEngine method; we only check
        existence here, never invoke it."""
        from tools.backtest import BacktestEngine
        assert callable(getattr(BacktestEngine,
                                "generate_paper_trade_signal", None))

    def test_bet_executor_disabled_by_construction(self):
        from tools.bet_executor import BetExecutor
        ex = BetExecutor.__new__(BetExecutor)
        BetExecutor.__init__(ex)
        assert getattr(ex, "_enabled", True) is False

    def test_order_manager_disabled_by_construction(self):
        from tools.order_manager import OrderManager
        om = OrderManager.__new__(OrderManager)
        OrderManager.__init__(om)
        assert getattr(om, "_enabled", True) is False

    def test_runs_module_is_tools_cli_runs(self):
        """Persistence genuinely lives in tools.cli.runs, not elsewhere."""
        import tools.cli.runs as runs_mod
        assert runs_mod.__file__ is not None
        assert Path(runs_mod.__file__).name == "runs.py"
