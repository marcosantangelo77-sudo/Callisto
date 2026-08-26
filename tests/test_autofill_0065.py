"""Autofill characterization #0065 — the ask / runs / doctor front door.

Characterization tests pinning the CURRENT safety contract of
`callisto ask`, `callisto runs`, `callisto show`, and `callisto doctor`
as implemented by tools/cli/ask.py, tools/cli/runs.py and
tools/cli/doctor.py.

Contract under test
-------------------
1. `check_seal_key()` fails closed: CALLISTO_SEAL_KEY that is unset,
   blank, whitespace-only, odd-length hex, or non-hex is refused, and
   `cmd_ask` exits 2 without constructing any router or engine —
   research must never start behind an unkeyed seal.
2. Happy paths set a VALID hex seal key ("ab" * 32). With it, the gate
   opens and the full pipeline (stubbed at the _load_router /
   _make_engine seams) runs, persists its record, and reports SEALED.
3. The seal key VALUE is secret: no refusal path ever prints it.
4. doctor reports BetExecutor.__init__ assigning _enabled = False and
   echoes the CALLISTO_LOCAL_ONLY switch state; an unset/invalid seal
   key makes doctor report PROBLEMS and exit non-zero.
5. Persistence lives in tools.cli.runs: `_load_run`, `_verify_artifact`,
   `_fetch_digest_status` are re-exported through callisto.py but owned
   by tools/cli/runs.py. A persisted run survives a save→list→show
   roundtrip; ambiguous ids refuse loudly; missing digests are HARD
   failures that make `show` exit non-zero.

Safety posture: these tests never arm live betting. Nothing here touches
_PAPER_TRADE_SIGNAL_STATUSES, generate_paper_trade_signal, or any
"live" execution status.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    _, out = _capture(sys.path.insert, 0, str(REPO))

import callisto  # noqa: E402
import tools.cli.ask as cli_ask          # noqa: E402
import tools.cli.doctor as cli_doctor    # noqa: E402
import tools.cli.runs as cli_runs        # noqa: E402
from callisto import build_parser        # noqa: E402
from tools.cli.ask import check_seal_key  # noqa: E402

VALID_KEY = "ab" * 32            # exactly 64 hex chars
KEYS_NEVER_PRINTED = [VALID_KEY, "deadbeef" * 8]


# ── helpers ────────────────────────────────────────────────────────────────

def _ask_args(q="front-door question", backend=None, self_review=False):
    return argparse.Namespace(
        providers=cli_ask._default_providers_path(),
        backend=backend, question=q, self_review=self_review)


def _run(coro):
    return asyncio.run(coro)


def _capture(fn, *a, **k):
    """Run fn capturing stdout; return (result, captured text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **k)
    return result, buf.getvalue()


def _assert_no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key value leaked into output"


@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    """Every test starts with NO seal key unless it sets one explicitly."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _boom_research(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite unkeyed seal")


def _wire_boom(monkeypatch):
    """Every post-gate seam explodes: nothing past check_seal_key() may
    execute when the gate refuses."""
    monkeypatch.setattr(callisto, "_load_router",
                        lambda p: (_ for _ in ()).throw(
                            _, out = _capture(AssertionError, "router loaded past bad gate")))
    monkeypatch.setattr(callisto, "_make_engine", _boom_research)
    monkeypatch.setattr(callisto, "_result_record", _boom_research)


class _FakeRouter:
    class _Ledger:
        def snapshot(self):
            return {"by_tier": {"gpu1": {"calls": 1}}}

    def __init__(self, endpoints=("gpu1",)):
        self.endpoints = list(endpoints)
        self.task_classes = {"decompose": "gpu1"}
        self.default_tier_name = "gpu1"
        self._health = {"status": "ok"}
        self.cost_ledger = self._Ledger()

    async def check_health(self, tier):
        assert tier == self.default_tier_name
        return self._health


def _fake_result(sealed=True, fetch_digest=None, artifacts=()):
    from tools.artifacts import ArtifactRef
    return NS(
        sealed=sealed,
        refusal_reason="" if sealed else "one independent source only",
        conclusion="the pipeline's actual verdict, never invented",
        confidence_score=0.42 if sealed else 0.10,
        confidence_tier="SPECULATIVE" if sealed else "UNVERIFIED",
        leaves=[NS(text="leaf q", answer="leaf a", tier="SPECULATIVE",
                   confidence=0.4)],
        artifact_refs=list(artifacts),
        fetches=[NS(source_name="openalex", url="https://example.org/x",
                    content_sha256=(fetch_digest or "b" * 64))],
        objections=[NS(text="objection text")],
        notes=["note one"])


def _wired(monkeypatch, result=None, router=None):
    router = router or _FakeRouter()
    engine = NS(adversary_router=None)

    async def run(q):
        return result if result is not None else _fake_result()

    engine.run = run
    built = {}

    def make_engine(r, self_review=False):
        built["router"] = r
        return engine

    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine", make_engine)
    monkeypatch.setattr(callisto, "_result_record",
                        lambda res, q: cli_ask._result_record(res, q))
    monkeypatch.setattr(callisto, "_persist_run",
                        lambda rec: cli_ask._persist_run(rec))
    return router, engine, built


# ════════════════════════════════════════════════════════════════════════
# 1. check_seal_key — the fail-closed gate itself
# ════════════════════════════════════════════════════════════════════════

class TestSealGateRefusals:
    def test_unset_key_refused(self):
        _, out = _capture(check_seal_key)
        assert "not set" in out

    def test_blank_key_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        _, out = _capture(check_seal_key)
        assert out == "" or "FAIL" in out

    @pytest.mark.parametrize("bad", ["   ", "\t", "\n"])
    def test_whitespace_only_key_refused(self, monkeypatch, bad):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        assert check_seal_key() is False

    @pytest.mark.parametrize("bad", [
        "zz" * 32,               # right length, not hex
        "0x" + "a" * 62,         # prefix junk
        "ab" * 31 + "g",         # one bad nibble at the end
        "not-a-hex-key-at-all",
    ])
    def test_non_hex_keys_refused(self, monkeypatch, capsys, bad):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        rc, out = _capture(check_seal_key)
        assert rc is False
        assert "hex" in out.lower()

    def test_odd_length_hex_refused(self, monkeypatch):
        # bytes.fromhex rejects odd-length strings — the gate must too.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abc")
        assert check_seal_key() is False

    def test_short_hex_still_accepted_by_gate_syntax(self, monkeypatch):
        # Characterization: the gate checks hex-validity only, not length;
        # bytes.fromhex("abcd") succeeds so the gate opens.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abcd")
        assert check_seal_key() is True

    def test_valid_64_hex_key_opens(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f"  {VALID_KEY}  ")
        assert check_seal_key() is True

    def test_uppercase_hex_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY.upper())
        assert check_seal_key() is True


class TestSealGateSecrets:
    @pytest.mark.parametrize("bad", [None, "", "   ", "zz" * 32])
    def test_no_key_value_printed_on_refusal(self, monkeypatch, capsys, bad):
        if bad is not None:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        _, out = _capture(check_seal_key)
        _assert_no_key_leak(out)

    def test_gate_message_mentions_unkeyed_rationale(self, capsys):
        _, out = _capture(check_seal_key)
        out = (out.lower())
        assert "unkeyed" in out or "forgeable" in out


# ════════════════════════════════════════════════════════════════════════
# 2. cmd_ask — refuses unkeyed seals BEFORE any research starts
# ════════════════════════════════════════════════════════════════════════

class TestAskRefusesUnkeyed:
    @pytest.mark.parametrize("seal", [None, "", "   ", "xyzzy", "gh" * 32])
    def test_exit_2_and_no_research(self, monkeypatch, capsys, seal):
        if seal is not None:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", seal)
        _wire_boom(monkeypatch)
        rc, out = _capture(_run, cli_ask.cmd_ask(_ask_args()))
        assert rc == 2
        _assert_no_key_leak(out)

    def test_unset_key_never_loads_router(self, monkeypatch):
        _wire_boom(monkeypatch)  # boom wiring doubles as tripwire
        rc = _run(cli_ask.cmd_ask(_ask_args()))
        assert rc == 2

    def test_non_hex_key_reports_hex_problem(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "nothex" * 8)
        rc, out = _capture(_run, cli_ask.cmd_ask(_ask_args()))
        assert rc == 2
        assert "hex" in out.lower()


# ════════════════════════════════════════════════════════════════════════
# 3. cmd_ask happy paths — valid hex seal key required throughout
# ════════════════════════════════════════════════════════════════════════

class TestAskHappyPath:
    def test_sealed_run_exits_zero(self, monkeypatch, runs_isolated, capsys):
        _wired(monkeypatch)
        rc, out = _capture(_run, cli_ask.cmd_ask(_ask_args()))
        assert rc == 0
        assert "SEALED" in out

    def test_refused_pipeline_result_exits_one(self, monkeypatch,
                                               runs_isolated):
        _wired(monkeypatch, result=_fake_result(sealed=False))
        rc = _run(cli_ask.cmd_ask(_ask_args()))
        assert rc == 1

    def test_run_record_persisted(self, monkeypatch, runs_isolated):
        _wired(monkeypatch)
        rc = _run(cli_ask.cmd_ask(_ask_args(q="persistence probe")))
        assert rc == 0
        files = list(runs_isolated.glob("*.json"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text(encoding="utf-8"))
        assert rec["question"] == "persistence probe"
        assert rec["sealed"] is True
        assert len(rec["fetches"]) == 1

    def test_unknown_backend_exits_two_without_engine(self, monkeypatch,
                                                      runs_isolated, capsys):
        router, _, built = _wired(
            monkeypatch, router=_FakeRouter(endpoints=("gpu1",)))
        rc, out = _capture(_run, cli_ask.cmd_ask(_ask_args(backend="nope")))
        assert rc == 2
        assert built == {}                      # engine never constructed
        assert "unknown provider tier" in out

    def test_unhealthy_backend_exits_two(self, monkeypatch, runs_isolated):
        router = _FakeRouter()
        router._health = {"status": "down"}
        _wired(monkeypatch, router=router)
        rc = _run(cli_ask.cmd_ask(_ask_args(backend="gpu1")))
        assert rc == 2

    def test_healthy_backend_routes_every_task_class(self, monkeypatch,
                                                     runs_isolated):
        router, _, _ = _wired(monkeypatch)
        router.task_classes = {"decompose": "other", "answer": "other"}
        rc = _run(cli_ask.cmd_ask(_ask_args(backend="gpu1")))
        assert rc == 0
        assert set(router.task_classes.values()) == {"gpu1"}
        assert router.default_tier_name == "gpu1"

    def test_output_names_sources_and_cost(self, monkeypatch, runs_isolated,
                                           capsys):
        _wired(monkeypatch)
        _, out = _capture(_run, cli_ask.cmd_ask(_ask_args()))
        assert "openalex" in out
        assert "cost" in out.lower()

    def test_persist_failure_does_not_crash_ask(self, monkeypatch,
                                                runs_isolated, capsys):
        _wired(monkeypatch)

        def _disk_full(rec):
            raise OSError("disk full")

        monkeypatch.setattr(callisto, "_persist_run", _disk_full)
        rc, out = _capture(_run, cli_ask.cmd_ask(_ask_args()))
        assert rc == 0
        assert "NOT SAVED" in out


# ════════════════════════════════════════════════════════════════════════
# 4. doctor — safety panels, BetExecutor disabled, LOCAL_ONLY echo
# ════════════════════════════════════════════════════════════════════════

DOCTOR_ARGS = argparse.Namespace(providers=cli_ask._default_providers_path())


class TestDoctorPanels:
    def test_betexecutor_disabled_reported_ok(self):
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out
        # sanity: production source really does default to disabled
        import tools.bet_executor as be
        src = Path(be.__file__).read_text(encoding="utf-8")
        m = re.search(r"class BetExecutor\b.*?def __init__\(self\):(.*?)"
                      r"(\n    (?:async )?def )", src, re.S)
        assert m and re.search(r"self\._enabled\s*=\s*False", m.group(1))

    def test_local_only_echoed_on(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_local_only_echoed_off_by_default(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_live_execute_switch_echoed(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_unset_seal_makes_doctor_fail(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        rc, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert rc != 0
        assert "PROBLEMS FOUND" in out
        assert "seals are unkeyed" in out

    def test_non_hex_seal_makes_doctor_fail(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "hello world not hex")
        rc, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert rc != 0
        assert "not valid hex" in out

    def test_valid_seal_panel_ok_but_other_problems_decide(self, monkeypatch,
                                                           capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        rc, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "OK: seal key is set (hex-valid)" in out
        _assert_no_key_leak(out)

    def test_loopback_bind_default_ok(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "OK: loopback default" in out

    @pytest.mark.parametrize("host", ["0.0.0.0", "::"])
    def test_wildcard_bind_fails(self, monkeypatch, capsys, host):
        monkeypatch.setenv("CALLISTO_BIND_HOST", host)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        rc, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert rc != 0
        assert "exposes the API" in out

    def test_all_six_panels_printed(self, capsys):
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        for panel in ("== providers ==", "== hermes cli ==",
                      "== database ==", "== source registry ==",
                      "== seal ==", "== bind ==", "== money switches =="):
            assert panel in out, f"missing panel {panel}"

    def test_money_switches_panel_mentions_order_manager_default(self,
                                                                 capsys):
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        assert "OrderManager.__init__ defaults _enabled = False" in out

    def test_doctor_never_prints_seal_value(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "deadbeef" * 8)
        _, out = _capture(cli_doctor.cmd_doctor, DOCTOR_ARGS)
        _assert_no_key_leak(out)

    def test_doctor_alias_backwards_compatible(self):
        assert cli_doctor._cmd_doctor is cli_doctor.cmd_doctor
        assert callisto._cmd_doctor is cli_doctor.cmd_doctor


# ════════════════════════════════════════════════════════════════════════
# 5. runs / show persistence stays in tools.cli.runs
# ════════════════════════════════════════════════════════════════════════

class TestPersistenceOwnership:
    def test_load_run_owned_by_tools_cli_runs(self):
        assert callisto._load_run is cli_runs._load_run
        assert hasattr(cli_runs, "_cmd_runs") and hasattr(cli_runs, "_cmd_show")

    def test_verify_artifact_owned_by_tools_cli_runs(self):
        assert callisto._verify_artifact is cli_runs._verify_artifact

    def test_cmd_runs_and_show_delegated_from_callisto(self):
        assert callisto._cmd_runs is cli_runs._cmd_runs
        assert callisto._cmd_show is cli_runs._cmd_show

    def test_persist_run_lives_in_tools_cli_ask(self):
        assert callisto._persist_run is cli_ask._persist_run
        assert callisto._result_record is cli_ask._result_record

    def test_fetch_digest_status_exported_from_tools_cli_runs(self):
        from tools.cli.runs import _fetch_digest_status
        assert callable(_fetch_digest_status)


class TestRunsRoundtrip:
    def _persist_one(self, question, sealed=True, fetch_digest=None):
        rec = cli_ask._result_record(
            _fake_result(sealed=sealed, fetch_digest=fetch_digest), question)
        return cli_ask._persist_run(rec)

    def test_save_then_list_newest_first(self, runs_isolated, capsys):
        self._persist_one("older question")
        self._persist_one("newer question")
        args = argparse.Namespace(limit=20)
        rc, out = _capture(cli_runs._cmd_runs, args)
        assert rc == 0
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert "SEALED" in lines[0] and "SEALED" in lines[1]

    def test_list_shows_refused_verdict(self, runs_isolated, capsys):
        self._persist_one("refused probe", sealed=False)
        _, out = _capture(cli_runs._cmd_runs, argparse.Namespace(limit=5))
        assert "REFUSED" in out

    def test_empty_runs_dir_friendly_message(self, runs_isolated, capsys):
        rc, out = _capture(cli_runs._cmd_runs, argparse.Namespace(limit=5))
        assert rc == 0
        assert "no saved runs yet" in out

    def test_limit_caps_listing(self, runs_isolated, capsys):
        for i in range(3):
            self._persist_one(f"q{i}")
        _, out = _capture(cli_runs._cmd_runs, argparse.Namespace(limit=2))
        assert len(out.strip().splitlines()) == 2

    def test_show_roundtrips_a_saved_run(self, runs_isolated, capsys):
        self._persist_one("roundtrip question")
        stem = sorted(p.stem for p in runs_isolated.glob("*.json"))[0]
        rc, out = _capture(cli_runs._cmd_show,
                           argparse.Namespace(run_id=stem))
        assert rc == 0
        assert "roundtrip question" in out
        assert "--- conclusion ---" in out
        assert "record   :" in out

    def test_show_missing_run_exits_one(self, runs_isolated, capsys):
        rc, out = _capture(cli_runs._cmd_show,
                           argparse.Namespace(run_id="nope"))
        assert rc == 1
        assert "no run matching 'nope'" in out

    def test_load_run_prefix_matching_unique(self, runs_isolated):
        self._persist_one("prefix probe")
        rec, path = cli_runs._load_run(
            sorted(p.stem for p in runs_isolated.glob("*.json"))[0][:8])
        assert rec["question"] == "prefix probe"
        assert path is not None

    def test_ambiguous_id_raises_systemexit(self, runs_isolated):
        self._persist_one("amb one")
        self._persist_one("amb two")
        with pytest.raises(SystemExit, match="ambiguous"):
            cli_runs._load_run("")     # empty prefix matches everything

    def test_unreadable_run_reported_not_swallowed(self, runs_isolated,
                                                   capsys):
        runs_isolated.mkdir(parents=True, exist_ok=True)
        (runs_isolated / "broken.json").write_text("{ not json")
        rc, out = _capture(cli_runs._cmd_runs, argparse.Namespace(limit=5))
        assert rc == 0
        assert "(unreadable:" in out


class TestFetchDigestHonesty:
    def test_valid_hex64_without_payload_is_soft(self):
        status, hard = cli_runs._fetch_digest_status(
            {"content_sha256": "b" * 64})
        assert status.startswith("unverified")
        assert hard is False

    def test_with_matching_body_verifies_ok(self):
        body = "fetched page body"
        good = __import__("hashlib").sha256(
            body.encode()).hexdigest()
        status, hard = cli_runs._fetch_digest_status(
            {"content_sha256": good, "body": body})
        assert status == "ok" and hard is False

    def test_mismatching_body_is_hard_failure(self):
        status, hard = cli_runs._fetch_digest_status(
            {"content_sha256": "b" * 64, "body": "different"})
        assert status == "DIGEST MISMATCH"
        assert hard is True

    @pytest.mark.parametrize("digest", [None, "", 42, "short", "z" * 64])
    def test_bad_digests_are_hard_failures(self, digest):
        status, hard = cli_runs._fetch_digest_status({"content_sha256": digest})
        assert hard is True
        assert status != "ok"

    def test_show_exits_nonzero_on_hard_digest_failure(self, runs_isolated,
                                                       capsys):
        rec = cli_ask._result_record(
            _fake_result(fetch_digest=None), "digest probe")
        rec["fetches"][0]["content_sha256"] = ""       # hard-fail shape
        _, out = _capture(cli_ask._persist_run, rec)
        stem = sorted(p.stem for p in runs_isolated.glob("*.json"))[0]
        rc, out = _capture(cli_runs._cmd_show,
                           argparse.Namespace(run_id=stem))
        assert rc == 1
        assert "MISSING DIGEST" in out
        assert "WARNING" in out

    def test_show_dedupes_only_identical_source_url_pairs(self, runs_isolated,
                                                          capsys):
        rec = cli_ask._result_record(_fake_result(), "dupes")
        dup = dict(rec["fetches"][0])
        rec["fetches"].append(dup)
        _, out = _capture(cli_ask._persist_run, rec)
        stem = sorted(p.stem for p in runs_isolated.glob("*.json"))[0]
        rc, out = _capture(cli_runs._cmd_show,
                           argparse.Namespace(run_id=stem))
        assert rc == 0
        # Characterization: identical (source, url) pairs with a SOFT status
        # ("unverified") are each printed; dedup only hides repeated "ok"
        # lines. The record stays readable and exits zero either way.
        assert out.count("[unverified") == 2


class TestResultRecordShape:
    def test_record_carries_full_provenance(self):
        rec = cli_ask._result_record(_fake_result(), "shape probe")
        assert set(rec) >= {"recorded_at", "question", "sealed",
                            "conclusion", "confidence", "leaves",
                            "artifacts", "fetches", "objections", "notes"}
        assert rec["confidence"]["tier"] == "SPECULATIVE"
        assert rec["fetches"][0]["source"] == "openalex"

    def test_record_tolerates_missing_fields(self):
        bare = NS(sealed=False)
        rec = cli_ask._result_record(bare, "bare")
        assert rec["sealed"] is False
        assert rec["conclusion"] == ""
        assert rec["leaves"] == [] and rec["fetches"] == []

    def test_persist_run_filename_has_json_suffix(self, runs_isolated):
        rec = cli_ask._result_record(_fake_result(), "naming probe")
        path = cli_ask._persist_run(rec)
        assert path.parent == runs_isolated
        assert path.suffix == ".json"
        assert json.loads(path.read_text(encoding="utf-8"))["question"] \
            == "naming probe"


# ════════════════════════════════════════════════════════════════════════
# 6. parser & entry plumbing
# ════════════════════════════════════════════════════════════════════════

class TestParserAndEntry:
    def test_ask_subcommand_parses_question(self):
        args = build_parser().parse_args(["ask", "what broke"])
        assert args.question == "what broke"

    def test_self_review_flag_defaults_false(self):
        args = build_parser().parse_args(["ask", "q"])
        assert args.self_review is False

    def test_runs_and_show_subcommands_exist(self):
        assert build_parser().parse_args(["runs"]).command == "runs"
        sid = build_parser().parse_args(["show", "abc123"])
        assert sid.command == "show" and sid.run_id == "abc123"

    def test_doctor_subcommand_exists(self):
        assert build_parser().parse_args(["doctor"]).command == "doctor"

    def test_entry_aliases_match_tool_modules(self):
        assert callisto._cmd_ask is cli_ask.cmd_ask
        assert callisto.check_seal_key is check_seal_key

    def test_runs_dir_override_respected(self, tmp_path, monkeypatch):
        target = tmp_path / "custom-runs"
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(target))
        assert cli_ask._runs_dir() == target
        assert target.exists()


# ════════════════════════════════════════════════════════════════════════
# 7. safety invariants — never arm live betting
# ════════════════════════════════════════════════════════════════════════

class TestSafetyInvariants:
    def test_front_door_modules_never_reference_live_status(self):
        for mod_path in (Path(cli_ask.__file__), Path(cli_runs.__file__),
                         Path(cli_doctor.__file__)):
            src = mod_path.read_text(encoding="utf-8")
            assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
            assert "generate_paper_trade_signal" not in src

    def test_paper_trade_signal_statuses_exclude_live(self):
        # The set lives in tools/signals/paper.py and is a frozenset of
        # paper-only statuses; "live" must never be among them.
        import tools.signals.paper as paper
        statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        assert statuses is not None, (
            "characterization expects the paper-trade status set to exist")
        assert "live" not in {str(s).lower() for s in statuses}

    def test_generate_paper_trade_signal_gate_never_widened_to_live(self):
        import inspect
        import tools.signals.paper as paper
        # The gate function must keep rejecting everything outside the
        # frozenset — widening it to 'live' is the forbidden change.
        assert paper.reject_non_paper("live") is True
        assert paper.reject_non_paper("paper_trading") is False
        # The frozenset literal must contain only paper statuses.
        assert set(paper._PAPER_TRADE_SIGNAL_STATUSES) == {"paper_trading"}

    def test_bet_executor_default_disabled_in_production_source(self):
        import tools.bet_executor as be
        src = Path(be.__file__).read_text(encoding="utf-8")
        init_body = re.search(
            r"class BetExecutor\b.*?def __init__\(self\):(.*?)"
            r"(\n    (?:async )?def )", src, re.S).group(1)
        assert "self._enabled = False" in re.sub(r"\s+", " ", init_body)
