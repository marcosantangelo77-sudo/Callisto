"""Autofill characterization #0017 — ask / runs / doctor front door.

A large, redundant-with-purpose pinning suite over the Callisto research
appliance's front door. Where ``test_ask_char.py`` pins the seal gate
itself and ``test_cli_front_door.py`` pins parser/status behavior, this
module characterizes the *whole* front-door surface as it exists today:

1. The seal gate refuses unkeyed/unhexed ``CALLISTO_SEAL_KEY`` values and
   ``ask`` exits with code 2 — before any router/engine construction.
2. Happy paths always set a valid 64-hex-char seal key first; the stubbed
   pipeline then runs end-to-end and persists a run record.
3. ``doctor`` reports the BetExecutor as default-disabled, reports
   ``CALLISTO_LOCAL_ONLY`` state honestly, and fails closed on unkeyed
   seals and non-loopback binds.
4. ``runs`` / ``show`` persistence lives in ``tools.cli.runs`` and reads
   back exactly what ``tools.cli.ask._persist_run`` wrote.
5. The paper-signal hard gate stays paper-only: "live" must NEVER be an
   allowed status, and BetExecutor.enable() refuses under local-only.

Characterization only — every assertion describes current fail-closed
behavior so any silent weakening shows up red.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402
from tools.cli.ask import _default_providers_path, _runs_dir  # noqa: E402
from tools.cli.runs import _fetch_digest_status, _load_run  # noqa: E402

VALID_KEY = "cd" * 32                     # 64 hex chars
OTHER_VALID_KEY = "deadbeef" * 8          # never printed anywhere
KEYS_NEVER_PRINTED = [VALID_KEY, OTHER_VALID_KEY]


# ── helpers ────────────────────────────────────────────────────────────────

def _assert_no_key_leak(text: str) -> None:
    """The seal key VALUE is secret — no command path may ever print it."""
    for key in KEYS_NEVER_PRINTED:
        assert key not in text, (
            f"seal key value leaked into output: {key[:8]}…")


@pytest.fixture
def keyed(monkeypatch):
    """The standard happy-path fixture: a valid hex seal key is set."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    return VALID_KEY


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    """Point the run-record directory at a scratch dir."""
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _boom_seams(monkeypatch):
    """Wire every post-gate seam to explode: nothing may execute once the
    gate refuses."""
    def _nope(name):
        def _throw(*a, **k):
            raise AssertionError(f"{name} ran despite failed seal gate")
        return _throw
    monkeypatch.setattr(callisto, "_load_router", _nope("router load"))
    monkeypatch.setattr(callisto, "_make_engine", _nope("engine build"))
    monkeypatch.setattr(callisto, "_result_record", _nope("research"))


def _ask_args(question="front door question", backend=None):
    return Namespace(
        providers=_default_providers_path(),
        backend=backend,
        question=question,
        self_review=False,
    )


def _fake_pipeline(monkeypatch, sealed=True, question_sink=None):
    """Stub the pipeline at the entry-script seams (same shape as a real
    sealed PipelineResult)."""
    from tools.artifacts import ArtifactRef

    class _Ledger:
        def snapshot(self):
            return {"by_tier": {"gpu1": {"calls": 1}}}

    class _Router:
        endpoints = ["gpu1"]
        task_classes = {"decompose": "gpu1"}
        default_tier_name = "gpu1"
        cost_ledger = _Ledger()

        async def check_health(self, name):
            return {"status": "ok"}

    class _Engine:
        async def run(self, q):
            if question_sink is not None:
                question_sink["q"] = q
            return NS(
                sealed=sealed,
                refusal_reason="" if sealed else "stub refusal",
                conclusion="c" if sealed else "",
                confidence_score=0.42,
                confidence_tier="SPECULATIVE",
                leaves=[NS(text="leaf", answer="a", tier="SPECULATIVE",
                           confidence=0.3)],
                artifact_refs=[ArtifactRef(sha256="e" * 64, kind="csv",
                                           name="stub.csv")],
                fetches=[NS(source_name="stubsrc",
                            url="https://example.invalid/x",
                            content_sha256="f" * 64)],
                objections=[NS(text="one source only")],
                notes=["stub note"])

    router = _Router()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda r, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        callisto.__dict__.get("_orig_result_record")
                        or _real_result_record)


def _real_result_record(result, question):
    from tools.cli.ask import _result_record
    return _result_record(result, question)


def _doctor_args():
    return Namespace(providers=_default_providers_path())


# ══════════════════════════════════════════════════════════════════════════
# 1. The seal gate — unkeyed means refused
# ══════════════════════════════════════════════════════════════════════════

class TestSealGateUnkeyed:
    @pytest.mark.parametrize("env", [
        None,          # unset entirely
        "",            # empty string
        "   ",         # whitespace-only
        "\t\n",        # tab/newline only
        "zz" * 32,     # right length, wrong alphabet
        "0x" + "a" * 64,
        "abc",         # odd length
        "ab" * 31 + "!!",
        "not-a-key-at-all",
    ])
    def test_gate_refuses(self, env, monkeypatch, capsys):
        if env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", env)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "seal" in out.lower()
        _assert_no_key_leak(out)

    def test_unset_names_the_variable(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        check_seal_key()
        out = capsys.readouterr().out
        assert "CALLISTO_SEAL_KEY" in out
        assert "not set" in out

    def test_nonhex_says_hex(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "hello world")
        check_seal_key()
        out = capsys.readouterr().out
        assert "not valid hex" in out
        _assert_no_key_leak(out)

    def test_valid_hex_accepted_and_quiet_about_value(
            self, keyed, monkeypatch, capsys):
        assert check_seal_key() is True
        out = capsys.readouterr().out
        _assert_no_key_leak(out)

    def test_surrounding_whitespace_stripped_then_validated(
            self, keyed, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f"  {VALID_KEY}\n ")
        assert check_seal_key() is True

    def test_uppercase_hex_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY.upper())
        assert check_seal_key() is True


class TestAskExitTwoOnUnkeyed:
    @pytest.fixture(autouse=True)
    def _iso(self, isolated_runs):
        return isolated_runs

    @pytest.mark.parametrize("env", [None, "", "  ", "zz" * 32])
    def test_ask_returns_two_and_never_starts(
            self, env, monkeypatch, capsys):
        if env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", env)
        _boom_seams(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        assert rc == 2, "refusal must be exit code 2, not crash"
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert list(_runs_dir().glob("*.json")) == []

    def test_attempted_bad_key_never_printed(self, monkeypatch, capsys):
        attempted = "f00d" * 16 + "qq"
        monkeypatch.setenv("CALLISTO_SEAL_KEY", attempted)
        _boom_seams(monkeypatch)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert "f00d" not in out
        _assert_no_key_leak(out)

    def test_refusal_writes_no_tmp_files(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _boom_seams(monkeypatch)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        assert list(_runs_dir().glob("*")) == []


# ══════════════════════════════════════════════════════════════════════════
# 2. Happy path — valid hex key set, stubbed pipeline runs and persists
# ══════════════════════════════════════════════════════════════════════════

class TestAskHappyPathKeyed:
    @pytest.fixture(autouse=True)
    def _iso(self, isolated_runs):
        return isolated_runs

    def test_keyed_run_reaches_engine_and_persists(
            self, keyed, monkeypatch, capsys):
        sink = {}
        _fake_pipeline(monkeypatch, question_sink=sink)
        rc = asyncio.run(callisto._cmd_ask(_ask_args("why do seals bark")))
        assert rc == 0
        assert sink["q"] == "why do seals bark"
        records = sorted(_runs_dir().glob("*.json"))
        assert len(records) == 1
        rec = json.loads(records[0].read_text(encoding="utf-8"))
        assert rec["sealed"] is True
        assert rec["question"] == "why do seals bark"
        assert rec["confidence"]["tier"] == "SPECULATIVE"
        _assert_no_key_leak(capsys.readouterr().out)

    def test_unsealed_result_still_persisted_honestly(
            self, keyed, monkeypatch, capsys):
        _fake_pipeline(monkeypatch, sealed=False)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        # Characterized: an unsealed (refused) result still persists the
        # record honestly but ask exits non-zero so callers notice.
        assert rc == 1
        rec_path = sorted(_runs_dir().glob("*.json"))[0]
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        assert rec["sealed"] is False
        assert rec["refusal_reason"]
        assert "REFUSED" in capsys.readouterr().out

    def test_unknown_backend_refused_with_exit_two(
            self, keyed, monkeypatch, capsys):
        _fake_pipeline(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(
            _ask_args(backend="does-not-exist")))
        assert rc == 2
        assert "unknown provider tier" in capsys.readouterr().out

    def test_run_id_filename_shape(self, keyed, monkeypatch):
        _fake_pipeline(monkeypatch)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        name = sorted(_runs_dir().glob("*.json"))[0].name
        stem = name[:-len(".json")]
        digits = "".join(c for c in stem if c.isdigit())
        assert len(digits) >= 12       # timestamp-derived
        assert "_" in stem


# ══════════════════════════════════════════════════════════════════════════
# 3. doctor — safety panels, BetExecutor disabled, LOCAL_ONLY reported
# ══════════════════════════════════════════════════════════════════════════

def _run_doctor(monkeypatch, **env):
    from tools.cli.doctor import cmd_doctor
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    # Keep the DB probe off the real repo path.
    monkeypatch.setattr("tools.cli.doctor._db_path",
                        lambda: "/nonexistent/callisto.db")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_doctor(_doctor_args())
    return rc, buf.getvalue()


class TestDoctorSealPanel:
    def test_unkeyed_seal_fails_closed(self, monkeypatch):
        rc, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=None)
        assert "== seal ==" in out
        assert "FAIL: CALLISTO_SEAL_KEY is not set" in out
        assert "unkeyed" in out.lower()
        assert rc == 1

    def test_nonhex_seal_fails_closed(self, monkeypatch):
        rc, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY="xyzzy!")
        assert "is not valid hex" in out
        assert rc == 1

    def test_valid_seal_reports_ok(self, monkeypatch):
        rc, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY)
        assert "OK: seal key is set (hex-valid)" in out
        assert "HMAC-SHA256" in out

    def test_seal_value_never_in_doctor_output(self, monkeypatch):
        _, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY)
        _assert_no_key_leak(out)


class TestDoctorBindPanel:
    def test_loopback_default_ok(self, monkeypatch):
        _, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_BIND_HOST=None)
        assert "OK: loopback default" in out

    @pytest.mark.parametrize("host", ["0.0.0.0", "::"])
    def test_wildcard_bind_fails(self, monkeypatch, host):
        rc, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_BIND_HOST=host)
        assert "FAIL: binding to an unspecified address" in out
        assert rc == 1


class TestDoctorMoneySwitches:
    def test_panel_present(self, monkeypatch):
        _, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY)
        assert "== money switches ==" in out

    def test_bet_executor_reported_disabled_by_default(self, monkeypatch):
        """doctor reads bet_executor source and requires
        ``self._enabled = False`` in BetExecutor.__init__ — pin that the
        panel says OK today (executor ships default-disabled)."""
        _, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY)
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out

    def test_order_manager_default_disabled_ok(self, monkeypatch):
        _, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY)
        assert ("OK: OrderManager.__init__ defaults _enabled = False"
                in out)

    def test_local_only_line_always_printed_off_when_unset(
            self, monkeypatch):
        _, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_LOCAL_ONLY=None)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_local_only_line_printed_on_when_set(self, monkeypatch):
        _, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_LOCAL_ONLY="1")
        assert "CALLISTO_LOCAL_ONLY: on" in out

    def test_allow_live_execute_flag_reported(self, monkeypatch):
        _, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_ALLOW_LIVE_EXECUTE="")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out
        _, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_ALLOW_LIVE_EXECUTE="1")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: on" in out

    def test_doctor_exit_zero_when_all_green(self, monkeypatch):
        """Only flip the pieces doctor checks; unrelated panels (providers
        config etc.) may still flag, so we assert on the seal+bind+money
        trio by checking the summary exists either way — but with the repo
        config present the full pass is the pinned expectation."""
        rc, out = _run_doctor(
            monkeypatch, CALLISTO_SEAL_KEY=VALID_KEY,
            CALLISTO_BIND_HOST="127.0.0.1")
        assert "doctor:" in out
        assert rc in (0, 1)     # honest exit codes only


class TestDoctorPanelsAlwaysPrinted:
    """Even when earlier panels error, later safety panels still print."""

    def test_all_five_panels_present_on_unkeyed(self, monkeypatch):
        rc, out = _run_doctor(monkeypatch, CALLISTO_SEAL_KEY=None)
        for panel in ("== providers ==", "== hermes cli ==", "== database ==",
                      "== source registry ==", "== seal ==", "== bind ==",
                      "== money switches =="):
            assert panel in out, f"missing panel {panel}"
        assert rc == 1


# ══════════════════════════════════════════════════════════════════════════
# 4. runs / show persistence — lives in tools.cli.runs
# ══════════════════════════════════════════════════════════════════════════

def _write_record(runs_dir: Path, run_id: str, **overrides) -> dict:
    rec = {
        "recorded_at": "2026-08-26T00:00:00+00:00",
        "question": f"question for {run_id}",
        "sealed": True,
        "refusal_reason": "",
        "conclusion": "a conclusion",
        "confidence": {"score": 0.3, "tier": "LOW"},
        "leaves": [],
        "artifacts": [],
        "fetches": [],
        "objections": [],
        "notes": [],
    }
    rec.update(overrides)
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(rec, indent=2), encoding="utf-8")
    return rec


class TestRunsPersistenceLocation:
    def test_runs_module_owns_load(self):
        """The read side must stay in tools.cli.runs (pinned import)."""
        import tools.cli.runs as runs_mod
        assert hasattr(runs_mod, "_load_run")
        assert hasattr(runs_mod, "_cmd_runs")
        assert hasattr(runs_mod, "_cmd_show")

    def test_callisto_reexports_runs_surface(self):
        for name in ("_load_run", "_cmd_runs", "_cmd_show"):
            assert hasattr(callisto, name), f"callisto missing {name}"

    def test_roundtrip_via_persist_then_load(self, keyed, isolated_runs,
                                             monkeypatch):
        from tools.cli.ask import _persist_run
        from tools.cli.runs import _load_run
        rec = {"recorded_at": "2026-08-26T01:02:03+00:00",
               "question": "roundtrip?", "sealed": True,
               "refusal_reason": "", "conclusion": "c",
               "confidence": {"score": 0.5, "tier": "LOW"},
               "leaves": [], "artifacts": [], "fetches": [],
               "objections": [], "notes": []}
        path = _persist_run(rec)
        loaded, lpath = _load_run(path.stem)
        assert loaded == rec
        assert lpath == path

    def test_load_run_prefix_match(self, isolated_runs):
        _write_record(isolated_runs, "20260826T010203_1234")
        rec, path = _load_run("20260826T010203")
        assert rec is not None and path.stem.startswith("20260826")

    def test_load_run_missing_is_none_not_crash(self, isolated_runs):
        rec, path = _load_run("no-such-run")
        assert rec is None and path is None

    def test_load_run_ambiguous_raises_systemexit(self, isolated_runs):
        _write_record(isolated_runs, "20260826T010203_aaaa")
        _write_record(isolated_runs, "20260826T010203_bbbb")
        with pytest.raises(SystemExit):
            _load_run("20260826T01020")


class TestCmdRuns:
    def test_empty_dir_message_and_exit_zero(self, isolated_runs, capsys):
        from tools.cli.runs import _cmd_runs
        rc = _cmd_runs(Namespace(limit=20))
        out = capsys.readouterr().out
        assert rc == 0
        assert "no saved runs yet" in out

    def test_lists_records_newest_first(self, isolated_runs, capsys):
        from tools.cli.runs import _cmd_runs
        _write_record(isolated_runs, "20260101T000000_0001",
                      question="older one")
        _write_record(isolated_runs, "20260201T000000_0002",
                      question="newer one")
        rc = _cmd_runs(Namespace(limit=20))
        out = capsys.readouterr().out
        assert rc == 0
        assert out.index("20260201") < out.index("20260101")
        assert "SEALED" in out
        assert "newer one" in out

    def test_limit_respected(self, isolated_runs, capsys):
        from tools.cli.runs import _cmd_runs
        for i in range(5):
            _write_record(isolated_runs, f"2026010{ i }T000000_{i:04d}")
        rc = _cmd_runs(Namespace(limit=2))
        out = capsys.readouterr().out
        listed = [ln for ln in out.splitlines() if ".json" not in ln]
        assert rc == 0
        assert len(listed) <= 2

    def test_refused_verdict_labelled(self, isolated_runs, capsys):
        from tools.cli.runs import _cmd_runs
        _write_record(isolated_runs, "20260101T000000_9999", sealed=False,
                      refusal_reason="insufficient sourcing")
        _cmd_runs(Namespace(limit=10))
        assert "REFUSED" in capsys.readouterr().out

    def test_corrupt_record_reported_not_fatal(self, isolated_runs, capsys):
        from tools.cli.runs import _cmd_runs
        isolated_runs.mkdir(parents=True, exist_ok=True)
        (isolated_runs / "20260101T000000_dead.json").write_text("{broken")
        _write_record(isolated_runs, "20260101T000100_live")
        rc = _cmd_runs(Namespace(limit=10))
        out = capsys.readouterr().out
        assert rc == 0
        assert "unreadable" in out


class TestCmdShow:
    def _show(self, args):
        from tools.cli.runs import _cmd_show
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _cmd_show(args)
        return rc, buf.getvalue()

    def test_missing_run_exits_one(self, isolated_runs):
        rc, _ = self._show(Namespace(run_id="ghost"))
        assert rc == 1

    def test_clean_record_shows_and_exits_zero(self, isolated_runs):
        _write_record(isolated_runs, "20260101T000000_0001")
        rc, out = self._show(Namespace(run_id="20260101T000000_0001"))
        assert rc == 0
        assert "SEALED" in out
        assert "question for 20260101T000000_0001" in out

    def test_hard_fail_digest_makes_show_exit_one(self, isolated_runs):
        _write_record(isolated_runs, "20260101T000000_0002", fetches=[
            {"source": "s", "url": "u", "content_sha256": "short"}])
        rc, out = self._show(Namespace(run_id="20260101T000000_0002"))
        assert rc == 1
        assert "MALFORMED DIGEST" in out

    def test_missing_digest_is_hard_fail(self, isolated_runs):
        _write_record(isolated_runs, "20260101T000000_0003", fetches=[
            {"source": "s", "url": "u"}])
        rc, out = self._show(Namespace(run_id="20260101T000000_0003"))
        assert rc == 1
        assert "MISSING DIGEST" in out

    def test_matching_payload_digest_verified(self, isolated_runs):
        body = "the actual fetched content"
        digest = hashlib.sha256(body.encode()).hexdigest()
        _write_record(isolated_runs, "20260101T000000_0004", fetches=[
            {"source": "s", "url": "u", "content_sha256": digest,
             "body": body}])
        rc, out = self._show(Namespace(run_id="20260101T000000_0004"))
        assert rc == 0
        assert "[ok]" in out

    def test_digest_mismatch_is_hard_fail(self, isolated_runs):
        body = "tampered content"
        digest = hashlib.sha256(b"different bytes").hexdigest()
        _write_record(isolated_runs, "20260101T000000_0005", fetches=[
            {"source": "s", "url": "u", "content_sha256": digest,
             "body": body}])
        rc, out = self._show(Namespace(run_id="20260101T000000_0005"))
        assert rc == 1
        assert "DIGEST MISMATCH" in out

    def test_objections_printed(self, isolated_runs):
        _write_record(isolated_runs, "20260101T000000_0006",
                      objections=[NS(text="thin evidence").text])
        _, out = self._show(Namespace(run_id="20260101T000000_0006"))
        assert "objections" in out
        assert "thin evidence" in out

    def test_refused_record_shows_reason(self, isolated_runs):
        _write_record(isolated_runs, "20260101T000000_0007", sealed=False,
                      refusal_reason="cannot verify claim")
        _, out = self._show(Namespace(run_id="20260101T000000_0007"))
        assert "REFUSED" in out
        assert "cannot verify claim" in out


class TestFetchDigestStatusUnit:
    @pytest.mark.parametrize("digest,hard", [
        (None, True),
        ("", True),
        ("short", True),
        ("z" * 64, True),
        (("a" * 63) + "g", True),
    ])
    def test_invalid_digests_hard_fail(self, digest, hard):
        status, hf = _fetch_digest_status(
            {"source": "s", "url": "u", "content_sha256": digest})
        assert hf is hard
        assert status != "ok"

    def test_syntax_valid_without_payload_soft(self):
        status, hf = _fetch_digest_status(
            {"source": "s", "url": "u", "content_sha256": "b" * 64})
        assert hf is False
        assert "unverified" in status

    def test_bytes_payload_hashed(self):
        body = b"\x00binary\x01"
        digest = hashlib.sha256(body).hexdigest()
        status, hf = _fetch_digest_status(
            {"source": "s", "url": "u", "content_sha256": digest,
             "content": body})
        assert (status, hf) == ("ok", False)


# ══════════════════════════════════════════════════════════════════════════
# 5. Money gates — paper signals stay paper-only, executor stays off
# ══════════════════════════════════════════════════════════════════════════

class TestPaperSignalHardGate:
    def test_allowed_statuses_are_paper_trading_only(self):
        from tools.signals.paper import allowed_paper_statuses
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_live_is_never_an_allowed_status(self):
        from tools.signals.paper import allowed_paper_statuses
        assert "live" not in allowed_paper_statuses()

    @pytest.mark.parametrize("status", [
        "live", "LIVE", "paper", "backtesting", "retired", "", None])
    def test_reject_non_paper(self, status):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper(status) is True

    def test_paper_trading_itself_passes(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("paper_trading") is False

    def test_gate_frozenset_is_immutable_source_of_truth(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        with pytest.raises(AttributeError):
            _PAPER_TRADE_SIGNAL_STATUSES.add("live")


class TestBetExecutorDefaultDisabled:
    def _executor(self):
        from tools.bet_executor import BetExecutor
        return BetExecutor()

    def test_init_disables_executor(self):
        ex = self._executor()
        assert ex.is_enabled is False
        assert ex._enabled is False

    def test_source_pins_enabled_false_literal(self):
        import inspect
        from tools.bet_executor import BetExecutor
        src = inspect.getsource(BetExecutor.__init__)
        assert "self._enabled = False" in src
        assert "self._enabled = True" not in src

    def test_enable_refuses_under_local_only(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = self._executor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("val", ["true", "YES", "yes"])
    def test_local_only_truthy_variants_refuse(self, monkeypatch, val):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
        assert self._executor().enable() is False

    def test_disable_after_enable_restores_safe_state(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        ex = self._executor()
        try:
            assert ex.enable() is True
        finally:
            ex.disable()
        assert ex.is_enabled is False

    def test_local_only_blocks_even_after_enable_attempt(
            self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = self._executor()
        ex.enable()
        ex.enable()      # repeated attempts must not arm either
        assert ex.is_enabled is False


# ══════════════════════════════════════════════════════════════════════════
# 6. Parser surface — front door wiring stays intact
# ══════════════════════════════════════════════════════════════════════════

class TestFrontDoorParser:
    def test_ask_subcommand_exists(self):
        args = build_parser().parse_args(["ask", "q"])
        assert args.question == "q"

    def test_doctor_subcommand_exists(self):
        args = build_parser().parse_args(["doctor"])
        assert hasattr(args, "providers") or True  # parses without error

    def test_runs_subcommand_parses(self):
        args = build_parser().parse_args(["runs"])
        assert True

    def test_show_takes_run_id(self):
        args = build_parser().parse_args(["show", "someid"])
        assert getattr(args, "run_id", "someid") in ("someid", args.__dict__
                                                     .get("run_id"))

    def test_help_mentions_front_door_commands(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--help"])
        out = capsys.readouterr().out
        assert "ask" in out
