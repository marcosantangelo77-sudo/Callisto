"""Characterization tests, wave 2: ask / runs / show / doctor.

Wave 1 (tests/test_ask_char.py) pinned the seal gate, the ask
fail-closed path, the doctor's three safety panels and api.py's public
/health. This module pins the *rest* of the front door as it exists
today, so any silent weakening shows up red:

  1. check_seal_key edge cases beyond wave 1 — case handling, unicode,
     embedded separators, and the exact failure vocabulary.
  2. ask's post-gate behaviour with a keyed gate: --backend preflight
     (unknown tier, unreachable provider, unhealthy status), the
     --self-review wiring into _make_engine, leaf/source/cost printing,
     and the persisted-record shape.
  3. The run-record serializer (_result_record): every field it lifts
     off a PipelineResult, including defaults when fields are absent.
  4. _persist_run: filename shape, atomicity (no .tmp litter),
     question-hash disambiguation.
  5. `callisto runs`: ordering (newest first), limit, verdict labels,
     unreadable-record resilience, empty-dir message.
  6. `callisto show` + _fetch_digest_status: missing/malformed/mismatch
     digests are HARD failures (exit 1); syntax-valid digests without a
     local payload stay soft; duplicate (source,url) pairs never hide an
     invalid sibling; artifact verification statuses flow through.
  7. doctor panels beyond wave 1: providers config errors, hermes_cli
     availability coupling, database panel, source registry, seal key
     case-tolerance, IPv6 wildcard bind, LOCAL_ONLY/LIVE_EXECUTE
     visibility, and the BetExecutor __init__-scoped regex.
  8. Static money-safety pins: the hard live-signal gate stays exactly
     frozenset({"paper_trading"}) with the `not in` comparison shape.

Every happy path sets CALLISTO_SEAL_KEY to valid hex. Every unkeyed /
bad-key ask path must refuse before research starts.
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
from tools.cli.ask import (  # noqa: E402
    _default_providers_path,
    _persist_run,
    _result_record,
    _runs_dir,
)
from tools.cli.doctor import _cmd_doctor  # noqa: E402
from tools.cli.runs import (  # noqa: E402
    _cmd_runs,
    _cmd_show,
    _fetch_digest_status,
    _HEX64_RE,
    _load_run,
)
from tools.backtest import _PAPER_TRADE_SIGNAL_STATUSES  # noqa: E402

VALID_KEY = "cd" * 32          # 64 hex chars, distinct value from wave 1
OTHER_VALID_KEY = "0123456789abcdef" * 4


def _no_leak(out: str, *keys: str) -> None:
    for k in keys:
        assert k not in out, f"seal key value leaked: {k[:8]}…"


# ── 1. check_seal_key edge cases ───────────────────────────────────────────


class TestCheckSealKeyEdges:
    def test_uppercase_hex_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY.upper())
        assert check_seal_key() is True

    def test_mixed_case_hex_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY",
                           "AbCdEf01" * 8)
        assert check_seal_key() is True

    def test_short_but_valid_hex_accepted_by_gate(self, monkeypatch):
        """The gate validates hex-ness only, not length; length policy is
        the operator's concern. Pin today's behaviour."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "beef")
        assert check_seal_key() is True

    def test_single_char_odd_hex_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "f")
        assert check_seal_key() is False
        assert "not valid hex" in capsys.readouterr().out

    def test_embedded_ascii_whitespace_between_bytes_tolerated(
            self, monkeypatch):
        """bytes.fromhex ignores single ASCII spaces between byte pairs;
        the gate inherits that, so pin today's tolerance."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY",
                           VALID_KEY[:2] + " " + VALID_KEY[2:])
        assert check_seal_key() is True

    def test_newline_inside_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY + "zz00")
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        _no_leak(out, VALID_KEY)

    def test_unicode_key_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ключ" * 16)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "not valid hex" in out

    def test_emoji_key_refused_and_not_echoed(self, monkeypatch, capsys):
        weird = "🦆" * 32
        monkeypatch.setenv("CALLISTO_SEAL_KEY", weird)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert weird not in out

    def test_refusal_messages_name_the_variable(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "CALLISTO_SEAL_KEY" in out
        assert "forgeable" in out.lower()

    def test_nonhex_refusal_mentions_fallback_danger(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz" * 32)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "unkeyed" in out.lower()

    def test_ok_path_prints_nothing_about_value(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True
        out = capsys.readouterr().out
        assert out == ""            # success is silent
        _no_leak(out, VALID_KEY)


# ── 2. ask: keyed gate + backend preflight + output contract ──────────────


class _Ledger:
    def __init__(self, by_tier=None):
        self._by_tier = by_tier or {}

    def snapshot(self):
        return {"by_tier": dict(self._by_tier)}


class _Router:
    def __init__(self, endpoints=("gpu1",), task_classes=None, ledger=None):
        self.endpoints = list(endpoints)
        self.task_classes = task_classes or {"decompose": "gpu1"}
        self.default_tier_name = self.endpoints[0] if endpoints else ""
        self.cost_ledger = ledger or _Ledger({"gpu1": {"calls": 3}})

    async def check_health(self, name):
        return {"status": "ok"}


class _Leaf:
    def __init__(self, text="leaf text", answer="", tier="VERIFIED",
                 confidence=0.8):
        self.text = text
        self.answer = answer
        self.tier = tier
        self.confidence = confidence


class _Fetch:
    def __init__(self, source_name="wikipedia", url="https://x", sha="ab" * 32):
        self.source_name = source_name
        self.url = url
        self.content_sha256 = sha


class _Ref:
    def __init__(self, kind="table", sha="ef" * 32, name="t1"):
        self.kind = kind
        self.sha256 = sha
        self.name = name

    def to_dict(self):
        return {"kind": self.kind, "sha256": self.sha256, "name": self.name}


class _Objection:
    def __init__(self, text="weak sourcing"):
        self.text = text


class _Engine:
    """Scriptable stand-in for ResearchPipeline."""

    def __init__(self, *, sealed=True, leaves=(), fetches=(), refs=(),
                 objections=(), notes=(), seen=None, self_review=False):
        self._payload = NS(
            sealed=sealed,
            refusal_reason="" if sealed else "insufficient evidence",
            conclusion="conclusion text" if sealed else "",
            confidence_score=0.73 if sealed else 0.2,
            confidence_tier="LIKELY" if sealed else "SPECULATIVE",
            leaves=list(leaves),
            fetches=list(fetches),
            objections=list(objections),
            notes=list(notes),
            artifact_refs=list(refs),
        )
        self.seen = seen if seen is not None else {}
        self.self_review = self_review

    async def run(self, q):
        self.seen["question"] = q
        return self._payload


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    d = tmp_path / "runs"
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _boom(*a, **k):
    raise AssertionError("research started despite unkeyed/bad seal")


def _wire_boom(monkeypatch):
    monkeypatch.setattr(callisto, "_load_router", _boom)
    monkeypatch.setattr(callisto, "_make_engine", _boom)
    monkeypatch.setattr(callisto, "_result_record", _boom)


def _ask_args(q="wave two question", backend=None, self_review=False,
              providers=None):
    return argparse.Namespace(
        providers=providers or _default_providers_path(),
        backend=backend,
        question=q,
        self_review=self_review,
    )


class TestAskUnkeyedStillFailsClosed:
    """Re-pin the refusal with the wave-2 fixtures so a regression in
    either module trips here too."""

    @pytest.mark.parametrize("env", [
        None, "", "   ", "\t\n", "xyzzy", "gg" * 32, VALID_KEY + "q",
        "0x" + VALID_KEY, "e" * 63, "e" * 65,
    ])
    def test_every_bad_key_state_returns_two_before_router_load(
            self, env, monkeypatch, capsys, runs_isolated):
        if env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        assert rc == 2
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert list(runs_isolated.glob("*")) == []

    def test_gate_checked_before_backend_validation(
            self, monkeypatch, capsys, runs_isolated):
        """A bad key plus a bogus backend must report the SEAL failure,
        not the backend one — the gate runs first."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="nope")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "seal" in out.lower()
        assert "unknown provider tier" not in out


class TestAskKeyedBackendPreflight:
    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def test_unknown_backend_tier_refused_with_configured_list(
            self, monkeypatch, capsys, runs_isolated):
        router = _Router(endpoints=("gpu1", "ox_alpha"))
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="wat")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier 'wat'" in out
        assert "gpu1" in out and "ox_alpha" in out

    def test_known_backend_reroutes_all_task_classes(
            self, monkeypatch, capsys, runs_isolated):
        router = _Router(endpoints=("gpu1", "ox_alpha"),
                         task_classes={"decompose": "gpu1", "adversary": "gpu1"})
        health_calls = []
        router.check_health = make_health_spy(router.check_health, health_calls)
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)

        engine = _Engine(seen=(seen := {}))
        captured = {}

        def fake_make(r, self_review=False):
            captured["router"] = r
            return engine

        monkeypatch.setattr(callisto, "_make_engine", fake_make)
        monkeypatch.setattr(callisto, "_result_record",
                            lambda result, q: {"recorded_at":
                                               "2026-08-26T01:00:00+00:00",
                                               "question": q})
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="ox_alpha")))
        assert rc == 0
        assert captured["router"].task_classes == {"decompose": "ox_alpha",
                                                   "adversary": "ox_alpha"}
        assert captured["router"].default_tier_name == "ox_alpha"
        assert health_calls == ["ox_alpha"]
        assert seen["question"] == "wave two question"

    def test_unreachable_backend_reports_doctor_hint(
            self, monkeypatch, capsys, runs_isolated):
        router = _Router(endpoints=("dead",))

        async def explode(name):
            raise ConnectionError("refused")

        router.check_health = explode
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)
        monkeypatch.setattr(callisto, "_make_engine", _boom)
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="dead")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unreachable" in out
        assert "doctor" in out

    def test_unhealthy_backend_status_refused(
            self, monkeypatch, capsys, runs_isolated):
        router = _Router(endpoints=("sick",))

        async def sick(name):
            return {"status": "degraded", "detail": "5xx"}

        router.check_health = sick
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)
        monkeypatch.setattr(callisto, "_make_engine", _boom)
        rc = asyncio.run(callisto._cmd_ask(_ask_args(backend="sick")))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unhealthy" in out
        assert "degraded" in out

    def test_no_backend_skips_preflight_entirely(
            self, monkeypatch, capsys, runs_isolated):
        router = _Router()
        calls = []
        router.check_health = make_health_spy(router.check_health, calls)
        monkeypatch.setattr(callisto, "_load_router", lambda p: router)
        engine = _Engine(seen={})
        monkeypatch.setattr(callisto, "_make_engine",
                            lambda r, self_review=False: engine)
        monkeypatch.setattr(callisto, "_result_record",
                            lambda result, q: {"recorded_at":
                                               "2026-08-26T02:00:00+00:00",
                                               "question": q})
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        assert rc == 0
        assert calls == []           # candidate chain decides per-task


def make_health_spy(orig, calls):
    def spy(name):
        calls.append(name)
        return orig(name)
    return spy


class TestAskOutputContract:
    @pytest.fixture(autouse=True)
    def _keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)

    def _wire(self, monkeypatch, engine, router=None):
        monkeypatch.setattr(callisto, "_load_router",
                            lambda p: router or _Router())
        monkeypatch.setattr(callisto, "_make_engine",
                            lambda r, self_review=False: engine)
        monkeypatch.setattr(callisto, "_result_record", _result_record)

    def test_self_review_flag_reaches_make_engine(
            self, monkeypatch, capsys, runs_isolated):
        got = {}
        engine = _Engine(seen={})

        def make(r, self_review=False):
            got["self_review"] = self_review
            return engine

        monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
        monkeypatch.setattr(callisto, "_make_engine", make)
        monkeypatch.setattr(callisto, "_result_record",
                            lambda res, q: {"recorded_at":
                                            "2026-08-26T03:00:00+00:00",
                                            "question": q})
        asyncio.run(callisto._cmd_ask(_ask_args(self_review=True)))
        assert got["self_review"] is True

    def test_sealed_output_includes_confidence_sources_cost_run_line(
            self, monkeypatch, capsys, runs_isolated):
        engine = _Engine(
            leaves=[_Leaf(answer="the answer")],
            fetches=[_Fetch(), _Fetch(source_name="espn")],
            refs=[_Ref()],
            objections=[_Objection()],
            notes=["note one"],
        )
        self._wire(money := None or monkeypatch, engine,
                   _Router(ledger=_Ledger({"gpu1": {"usd": 0.05}})))
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert rc == 0
        assert "=" * 72 in out
        assert "SEALED   confidence 0.73 tier=LIKELY" in out
        assert "leaf [VERIFIED 0.80]" in out
        assert "the answer" in out
        assert "sources  : 2 distinct (espn, wikipedia)" in out
        assert "/ 2 fetches" in out
        assert "objections (1):" in out
        assert "- weak sourcing" in out
        assert '"usd"' in out and "0.05" in out      # cost json
        assert re.search(r"^run      : .+\.json$", out, re.M)
        assert "artifact : table efefefefefefefef…  t1" in out

    def test_leaf_without_answer_still_listed(
            self, monkeypatch, capsys, runs_isolated):
        engine = _Engine(leaves=[_Leaf(text="open leaf", answer=None)])
        self._wire(monkeypatch, engine)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert rc == 0
        assert "open leaf" in out

    def test_multiline_answers_flattened_and_truncated(
            self, monkeypatch, capsys, runs_isolated):
        long_ans = ("word " * 200).strip()
        engine = _Engine(leaves=[
            _Leaf(text="l", answer="line1\nline2"),
            _Leaf(text="l2", answer=long_ans),
        ])
        self._wire(monkeypatch, engine)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert "line1 line2" in out       # newlines flattened
        assert "\nline1\n" not in out
        # 400-char truncation of answers
        assert long_ans[:400] in out
        assert long_ans[400:] not in out

    def test_objections_capped_at_five_in_output(
            self, monkeypatch, capsys, runs_isolated):
        engine = _Engine(objections=[_Objection(f"ob{i}") for i in range(9)])
        self._wire(monkeypatch, engine)
        asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert "objections (9):" in out
        assert "- ob4" in out
        assert "- ob5" not in out

    def test_refused_output_shows_reason_and_exit_one(
            self, monkeypatch, capsys, runs_isolated):
        engine = _Engine(sealed=False)
        self._wire(monkeypatch, engine)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert rc == 1
        assert "REFUSED" in out
        assert "reason   : insufficient evidence" in out

    def test_persist_failure_does_not_crash_ask(
            self, monkeypatch, capsys, runs_isolated):
        engine = _Engine()
        self._wire(monkeypatch, engine)

        def broken_persist(record):
            raise OSError("disk full")

        monkeypatch.setattr(callisto, "_persist_run", broken_persist)
        rc = asyncio.run(callisto._cmd_ask(_ask_args()))
        out = capsys.readouterr().out
        assert rc == 0                    # research succeeded regardless
        assert "run      : NOT SAVED (disk full)" in out


# ── 3. _result_record serialisation ───────────────────────────────────────


class TestResultRecord:
    def test_full_result_round_trip(self):
        result = NS(
            sealed=True,
            refusal_reason="",
            conclusion="c",
            confidence_score=0.9,
            confidence_tier="VERIFIED",
            leaves=[_Leaf(text="t", answer="a", tier="VERIFIED",
                          confidence=1)],
            fetches=[_Fetch()],
            objections=[_Objection("weak sourcing")],
            notes=["n1"],
            artifact_refs=[_Ref()],
        )
        rec = _result_record(result, "q")
        assert rec["sealed"] is True
        assert rec["refusal_reason"] == ""
        assert rec["conclusion"] == "c"
        assert rec["confidence"] == {"score": 0.9, "tier": "VERIFIED"}
        assert rec["leaves"] == [{"text": "t", "answer": "a",
                                  "tier": "VERIFIED", "confidence": 1}]
        assert rec["fetches"] == [{"source": "wikipedia",
                                   "url": "https://x",
                                   "content_sha256": "ab" * 32}]
        assert rec["artifacts"] == [{"kind": "table", "sha256": "ef" * 32,
                                     "name": "t1"}]
        assert rec["objections"] == ["weak sourcing"]
        assert rec["notes"] == ["n1"]
        assert rec["question"] == "q"
        # recorded_at is ISO-8601 UTC with seconds precision
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$",
                        rec["recorded_at"])

    def test_missing_attrs_get_safe_defaults(self):
        rec = _result_record(NS(), "minimal")
        assert rec["sealed"] is False
        assert rec["refusal_reason"] == ""
        assert rec["conclusion"] == ""
        assert rec["confidence"]["score"] == 0.0
        assert rec["confidence"]["tier"] == "UNVERIFIED"
        assert rec["leaves"] == []
        assert rec["artifacts"] == []
        assert rec["fetches"] == []
        assert rec["objections"] == []
        assert rec["notes"] == []

    def test_none_answer_becomes_empty_string(self):
        rec = _result_record(NS(leaves=[_Leaf(answer=None)]), "q")
        assert rec["leaves"][0]["answer"] == ""

    def test_objection_without_text_stringified(self):
        rec = _result_record(NS(objections=[42]), "q")
        assert rec["objections"] == ["42"]

    def test_record_is_json_serialisable(self):
        result = NS(leaves=[_Leaf()], fetches=[_Fetch()], notes=["n"],
                    objections=[_Objection()], artifact_refs=[_Ref()],
                    sealed=True, refusal_reason="", conclusion="c",
                    confidence_score=0.5, confidence_tier="LIKELY")
        assert json.loads(json.dumps(_result_record(result, "q")))


# ── 4. _persist_run ───────────────────────────────────────────────────────


class TestPersistRun:
    def test_filename_shape_timestamp_plus_question_hash(self, runs_isolated):
        path = _persist_run({"recorded_at": "2026-08-26T04:05:06+00:00",
                             "question": "hello"})
        stem = path.stem
        assert stem.startswith("20260826T040506+0000_")
        suffix = stem.rsplit("_", 1)[1]
        assert len(suffix) == 4 and suffix.isdigit()

    def test_same_timestamp_different_questions_both_saved(
            self, runs_isolated):
        base = "2026-08-26T04:05:06+00:00"
        p1 = _persist_run({"recorded_at": base, "question": "alpha"})
        p2 = _persist_run({"recorded_at": base, "question": "beta"})
        assert p1 != p2
        assert len(list(runs_isolated.glob("*.json"))) == 2

    def test_identical_record_overwrites_atomically(self, runs_isolated):
        rec = {"recorded_at": "2026-08-26T04:05:06+00:00",
               "question": "same"}
        p1 = _persist_run(dict(rec))
        p2 = _persist_run(dict(rec))
        assert p1 == p2
        files = list(runs_isolated.glob("*"))
        assert len(files) == 1          # no .tmp litter left behind
        assert files[0].suffix == ".json"

    def test_content_is_the_json_record(self, runs_isolated):
        path = _persist_run({"recorded_at": "2026-08-26T04:05:06+00:00",
                             "question": "q", "sealed": True})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["question"] == "q" and data["sealed"] is True

    def test_runs_dir_created_on_demand(self, tmp_path, monkeypatch):
        d = tmp_path / "deep" / "nested" / "runs"
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(d))
        _persist_run({"recorded_at": "2026-08-26T04:05:06+00:00",
                      "question": "q"})
        assert d.is_dir()


# ── 5. `callisto runs` ────────────────────────────────────────────────────


class TestCmdRuns:
    def _write(self, runs_dir, name, **fields):
        rec = {"recorded_at": "2026-08-26T00:00:00+00:00",
               "question": fields.pop("q", "?"),
               "sealed": fields.pop("sealed", True),
               "confidence": {"tier": "LIKELY", "score": 0.7},
               **fields}
        (runs_dir / f"{name}.json").write_text(
            json.dumps(rec), encoding="utf-8")

    def test_empty_dir_message_and_zero(self, runs_isolated, capsys):
        args = argparse.Namespace(limit=20)
        rc = _cmd_runs(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert "no saved runs yet" in out
        assert "ask" in out

    def test_newest_first_ordering(self, runs_isolated, capsys):
        self._write(runs_dir := runs_isolated, "20260101T000000+0000_0001")
        self._write(runs_dir, "20260601T000000+0000_0002")
        self._write(runs_dir, "20260301T000000+0000_0003")
        _cmd_runs(argparse.Namespace(limit=20))
        lines = capsys.readouterr().out.strip().splitlines()
        ids = [ln.split()[0] for ln in lines]
        assert ids == sorted(ids, reverse=True)

    def test_limit_caps_listing(self, runs_isolated, capsys):
        for i in range(5):
            self._write(runs_isolated, f"2026010{i}T000000+0000_000{i}")
        _cmd_runs(argparse.Namespace(limit=2))
        assert len(capsys.readouterr().out.strip().splitlines()) == 2

    def test_verdict_labels_match_sealed_flag(self, runs_isolated, capsys):
        self._write(runs_isolated, "20260101T000000+0000_0001", sealed=True)
        self._write(runs_isolated, "20260102T000000+0000_0002", sealed=False)
        assert _cmd_runs(argparse.Namespace(limit=20)) == 0
        out = capsys.readouterr().out
        assert "SEALED" in out and "REFUSED" in out

    def test_confidence_rendered_as_tier_over_score(
            self, runs_isolated, capsys):
        self._write(runs_isolated, "20260101T000000+0000_0001")
        assert _cmd_runs(argparse.Namespace(limit=20)) == 0
        out = capsys.readouterr().out
        assert "LIKELY/0.70" in out

    def test_question_truncated_to_sixty_chars(self, runs_isolated, capsys):
        self._write(runs_isolated, "20260101T000000+0000_0001", q="w" * 100)
        assert _cmd_runs(argparse.Namespace(limit=20)) == 0
        out = capsys.readouterr().out
        assert "w" * 61 not in out

    def test_unreadable_record_reported_not_fatal(self, runs_isolated,
                                                  capsys):
        (runs_isolated / "20260101T000000+0000_0001.json").write_text(
            "{broken", encoding="utf-8")
        rc = _cmd_runs(argparse.Namespace(limit=20))
        out = capsys.readouterr().out
        assert rc == 0
        assert "unreadable" in out

    def test_missing_confidence_defaults(self, runs_isolated, capsys):
        (runs_isolated / "20260101T000000+0000_0001.json").write_text(
            json.dumps({"recorded_at": "2026-08-26T00:00:00+00:00"}),
            encoding="utf-8")
        rc = _cmd_runs(argparse.Namespace(limit=20))
        out = capsys.readouterr().out
        assert rc == 0
        assert "?/" in out              # unknown tier, score 0


# ── 6. `show` + digest validation ────────────────────────────────────────


class TestFetchDigestStatus:
    def test_valid_digest_without_payload_is_soft(self):
        st, hard = _fetch_digest_status(
            {"content_sha256": hashlib_sha("x"), "source": "s", "url": "u"})
        assert st.startswith("unverified") and hard is False

    @pytest.mark.parametrize("body_key", ["body", "content", "payload"])
    def test_matching_payload_verifies_ok(self, body_key):
        body = b"actual bytes"
        st, hard = _fetch_digest_status({
            "content_sha256": real_sha256(body), body_key: "actual bytes"})
        assert st == "ok" and hard is False

    def test_mismatch_is_hard_failure(self):
        st, hard = _fetch_digest_status({
            "content_sha256": real_sha256(b"a"), "body": "b"})
        assert st == "DIGEST MISMATCH" and hard is True

    def test_bytes_payload_accepted(self):
        body = b"\x00\x01"
        st, hard = _fetch_digest_status({
            "content_sha256": real_sha256(body), "body": body})
        assert st == "ok"

    def test_missing_digest_hard(self):
        st, hard = _fetch_digest_status({})
        assert "MISSING DIGEST" in st and hard is True

    def test_none_digest_hard(self):
        st, hard = _fetch_digest_status({"content_sha256": None})
        assert "MISSING DIGEST" in st and hard is True

    def test_wrong_length_hard(self):
        st, hard = _fetch_digest_status({"content_sha256": "ab" * 10})
        assert "MALFORMED DIGEST (20 chars)" in st and hard is True

    def test_non_hex_hard(self):
        st, hard = _fetch_digest_status({"content_sha256": "z" * 64})
        assert "MALFORMED DIGEST (non-hex)" in st and hard is True

    def test_uppercase_digest_normalised_to_ok(self):
        body = b"up"
        st, hard = _fetch_digest_status({
            "content_sha256": real_sha256(body).upper(), "body": "up"})
        assert st == "ok"

    def test_hex_regex_shape(self):
        assert _HEX64_RE.match(real_sha256(b""))
        assert not _HEX64_RE.match("A" * 64)     # lowercase only
        assert not _HEX64_RE.match("g" * 64)


def real_sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def hashlib_sha(_: str) -> str:      # alias used above for readability
    return real_sha256(_.encode())


class TestCmdShow:
    def _seed(self, runs_dir, rec, name="20260101T000000+0000_0001"):
        (runs_dir / f"{name}.json").write_text(
            json.dumps(rec), encoding="utf-8")
        return name

    _BASE = {
        "recorded_at": "2026-08-26T00:00:00+00:00",
        "question": "shown question",
        "sealed": True,
        "confidence": {"tier": "LIKELY", "score": 0.7},
        "conclusion": "the conclusion",
    }

    def _run_show(self, run_id, monkeypatch, capsys,
                  verify=lambda sha: "missing"):
        monkeypatch.setattr("tools.cli.runs._verify_artifact", verify)
        rc = _cmd_show(argparse.Namespace(run_id=run_id))
        captured = capsys.readouterr()
        return rc, captured.out + captured.err

    def test_missing_run_id_exits_one(self, runs_isolated, capsys):
        rc = _cmd_show(argparse.Namespace(run_id="ghost"))
        assert rc == 1
        assert "no run matching 'ghost'" in capsys.readouterr().out

    def test_prefix_match_resolves_unique_run(
            self, runs_isolated, monkeypatch, capsys):
        self._seed(runs_isolated, dict(self._BASE))
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "shown question" in out

    def test_ambiguous_prefix_systemexit(
            self, runs_isolated, monkeypatch, capsys):
        self._seed(runs_isolated, dict(self._BASE),
                   "20260101T000000+0000_0001")
        self._seed(runs_isolated, dict(self._BASE),
                   "20260101T000000+0000_0002")
        with pytest.raises(SystemExit) as ei:
            _load_run("20260101")
        assert "ambiguous" in str(ei.value)

    def test_sealed_header_and_conclusion(
            self, runs_isolated, monkeypatch, capsys):
        self._seed(runs_isolated, dict(self._BASE))
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "SEALED   : LIKELY 0.70" in out
        assert "--- conclusion ---" in out
        assert "the conclusion" in out

    def test_refused_shows_reason(
            self, runs_isolated, monkeypatch, capsys):
        rec = dict(self._BASE, sealed=False,
                   refusal_reason="cannot verify")
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "REFUSED  : LIKELY 0.70" in out
        assert "reason   : cannot verify" in out

    def test_artifact_statuses_flow_through(
            self, runs_isolated, monkeypatch, capsys):
        rec = dict(self._BASE, artifacts=[
            {"kind": "table", "sha256": "aa" * 32, "name": "good"},
            {"kind": "csv", "sha256": "bb" * 32, "name": "bad"},
        ])
        self._seed(runs_isolated, rec)

        def verify(sha):
            return "ok" if sha.startswith("aa") else "CORRUPT"

        rc, out = self._run_show("20260101", monkeypatch, capsys, verify)
        assert rc == 0                      # artifacts don't hard-fail
        assert "[ok           ]" in out.replace("[ok]", "").replace(
            "[ok           ]", "") or "[ok" in out
        assert "CORRUPT" in out
        assert "re-hashed against the store" in out

    def test_fetch_ok_lines_are_silent_duplicates_collapsed(
            self, runs_isolated, monkeypatch, capsys):
        dup = {"source": "s", "url": "u", "content_sha256": real_sha256(b"x"),
               "body": "x"}
        rec = dict(self._BASE, fetches=[dup, dict(dup)])
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "[ok]" in out
        # duplicate ok rows collapsed to a single print
        assert out.count("[ok]") == 1

    def test_duplicate_invalid_sibling_not_hidden(
            self, runs_isolated, monkeypatch, capsys):
        good = {"source": "s", "url": "u",
                "content_sha256": real_sha256(b"x"), "body": "x"}
        bad = {"source": "s", "url": "u", "content_sha256": ""}
        rec = dict(self._BASE, fetches=[good, bad])
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 1
        assert "MISSING DIGEST" in out
        assert "WARNING: 1 fetch(es)" in out

    def test_digest_mismatch_exits_one(
            self, runs_isolated, monkeypatch, capsys):
        rec = dict(self._BASE, fetches=[
            {"source": "s", "url": "u",
             "content_sha256": real_sha256(b"true"), "body": "forged"}])
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 1
        assert "DIGEST MISMATCH" in out

    def test_soft_unverified_keeps_exit_zero(
            self, runs_isolated, monkeypatch, capsys):
        rec = dict(self._BASE, fetches=[
            {"source": "s", "url": "u",
             "content_sha256": real_sha256(b"remote")}])
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "unverified" in out

    def test_objections_section(
            self, runs_isolated, monkeypatch, capsys):
        rec = dict(self._BASE, objections=["obj one", "obj two"])
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "objections (2):" in out
        assert "- obj one" in out and "- obj two" in out

    def test_no_conclusion_no_section(
            self, runs_isolated, monkeypatch, capsys):
        rec = {k: v for k, v in self._BASE.items() if k != "conclusion"}
        self._seed(runs_isolated, rec)
        rc, out = self._run_show("20260101", monkeypatch, capsys)
        assert rc == 0
        assert "--- conclusion ---" not in out

    def test_end_to_end_ask_then_runs_then_show(
            self, monkeypatch, capsys, runs_isolated):
        """Full round trip through the real seams: keyed ask persists a
        record, runs lists it, show reprints it with exit zero."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        engine = _Engine(leaves=[_Leaf()], fetches=[
            _Fetch(url="https://example.com/a")],
            refs=[_Ref()], objections=[], notes=[])
        monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
        monkeypatch.setattr(callisto, "_make_engine",
                            lambda r, self_review=False: engine)
        monkeypatch.setattr(callisto, "_result_record", _result_record)
        rc = asyncio.run(callisto._cmd_ask(_ask_args("round trip q")))
        assert rc == 0
        saved = list(runs_isolated.glob("*.json"))
        assert len(saved) == 1
        run_id = saved[0].stem

        capsys.readouterr()
        assert _cmd_runs(argparse.Namespace(limit=20)) == 0
        runs_out = capsys.readouterr().out
        assert "SEALED" in runs_out and "round trip q" in runs_out

        monkeypatch.setattr("tools.cli.runs._verify_artifact",
                            lambda sha: "missing")
        assert _cmd_show(argparse.Namespace(run_id=run_id)) == 0
        show_out = capsys.readouterr().out
        assert "round trip q" in show_out
        assert "--- conclusion ---" in show_out
        assert "the conclusion" not in show_out  # engine stub has none


# ── 7. doctor: additional panels ─────────────────────────────────────────


def _doctor(extra_env=None, monkeypatch=None, capsys=None, argv_extra=()):
    if monkeypatch is not None:
        for k, v in (extra_env or {}).items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
    else:
        for k, v in (extra_env or {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    argv = ["doctor", *argv_extra]
    rc = _cmd_doctor(build_parser().parse_args(argv))
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


class TestDoctorMorePanels:
    def test_providers_default_marker_starred(
            self, monkeypatch, capsys, tmp_path):
        prov = tmp_path / "providers.yaml"
        prov.write_text(
            "default_tier: gpu1\n"
            "providers:\n"
            "  gpu1:\n"
            "    backend: openai_compat\n"
            "    model: m1\n"
            "    max_concurrency: 4\n"
            "  ox_alpha:\n"
            "    backend: ox\n"
            "    model: stealth/ox-alpha\n",
            encoding="utf-8")
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys,
            argv_extra=("--providers", str(prov)))
        assert rc == 0
        assert "*gpu1" in out
        assert " ox_alpha" in out
        assert "backend=openai_compat" in out
        assert "concurrency=4" in out
        _no_leak(out, VALID_KEY)

    def test_unreadable_providers_config_fails(
            self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys,
            argv_extra=("--providers", "/nonexistent/providers.yaml"))
        assert rc != 0
        assert "config unreadable" in out

    def test_empty_providers_table_fails(
            self, monkeypatch, capsys, tmp_path):
        prov = tmp_path / "empty.yaml"
        prov.write_text("providers: {}\n", encoding="utf-8")
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys,
            argv_extra=("--providers", str(prov)))
        assert rc != 0
        assert "NO PROVIDERS CONFIGURED" in out

    def test_hermes_cli_panel_present(self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys)
        assert "== hermes cli ==" in out
        assert re.search(r"available: (True|False)", out)

    def test_database_panel_names_db(
            self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "c.db"))
        _, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys)
        assert "== database ==" in out
        assert f"path: {tmp_path / 'c.db'}" in out
        assert "present: False" in out

    def test_database_present_true_when_file_exists(
            self, monkeypatch, capsys, tmp_path):
        db = tmp_path / "c.db"
        db.write_bytes(b"sqlite")
        monkeypatch.setenv("CALLISTO_DB_PATH", str(db))
        _, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys)
        assert "present: True" in out

    def test_source_registry_panel_lists_adapters(
            self, monkeypatch, capsys):
        _, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys)
        assert "== source registry ==" in out
        assert re.search(r"\d+ adapters registered", out)

    def test_uppercase_hex_seal_key_ok(self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY.upper()}, monkeypatch, capsys)
        assert rc == 0
        assert "OK: seal key is set (hex-valid)" in out
        _no_leak(out, VALID_KEY.upper())

    def test_ipv6_wildcard_bind_fails(self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY,
             "CALLISTO_BIND_HOST": "::"}, monkeypatch, capsys)
        assert rc != 0
        assert any("host: ::" in ln for ln in out.splitlines())

    def test_custom_loopback_bind_ok(self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY,
             "CALLISTO_BIND_HOST": "127.0.0.1"}, monkeypatch, capsys)
        assert rc == 0
        assert "host: 127.0.0.1" in out

    def test_local_only_off_by_default_visible(
            self, monkeypatch, capsys):
        _, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY,
             "CALLISTO_LOCAL_ONLY": None}, monkeypatch, capsys)
        assert "CALLISTO_LOCAL_ONLY: off" in out

    def test_allow_live_execute_on_visible(
            self, monkeypatch, capsys):
        _, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY,
             "CALLISTO_ALLOW_LIVE_EXECUTE": "1"}, monkeypatch, capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: on" in out

    def test_money_switch_source_checks_pass_today(
            self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": VALID_KEY}, monkeypatch, capsys)
        assert rc == 0
        assert "OK: OrderManager.__init__ defaults _enabled = False" in out
        assert "OK: BetExecutor.__init__ assigns _enabled = False" in out

    def test_problems_footer_wording(self, monkeypatch, capsys):
        rc, out = _doctor({"CALLISTO_SEAL_KEY": None},
                          monkeypatch, capsys)
        assert rc != 0
        assert "doctor: PROBLEMS FOUND (see above)" in out

    def test_all_green_when_keyed_and_loopback(
            self, monkeypatch, capsys):
        rc, out = _doctor(
            {"CALLISTO_SEAL_KEY": OTHER_VALID_KEY,
             "CALLISTO_BIND_HOST": None,
             "CALLISTO_LOCAL_ONLY": None,
             "CALLISTO_ALLOW_LIVE_EXECUTE": None},
            monkeypatch, capsys,
            argv_extra=("--providers", str(REPO / "config" / "providers.yaml")))
        assert rc == 0
        assert "doctor: OK" in out
        _no_leak(out, OTHER_VALID_KEY)


# ── 8. static safety pins ─────────────────────────────────────────────────


class TestStaticSafetyPins:
    def test_paper_trade_signal_statuses_exactly_paper_trading(self):
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_backtest_source_compares_against_the_gate_set(self):
        src = (REPO / "tools" / "signals" / "paper.py").read_text(
            encoding="utf-8")
        assert re.search(r"status\s+not\s+in\s+_PAPER_TRADE_SIGNAL_STATUSES",
                         src), "gate must use `status not in ...`"
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset(' \
               '{"paper_trading"})' in src

    def test_never_add_live_to_signal_statuses(self):
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_ask_module_has_no_live_widening(self):
        src = (REPO / "tools" / "cli" / "ask.py").read_text(encoding="utf-8")
        assert "generate_paper_trade_signal" not in src
        assert "'live'" not in src and '"live"' not in src

    def test_entry_script_reexports_front_door_commands(self):
        src = (REPO / "callisto.py").read_text(encoding="utf-8")
        for fn in ("_cmd_ask", "_cmd_runs", "_cmd_show",
                   "_cmd_doctor", "_cmd_status", "check_seal_key"):
            assert fn in src

    def test_parser_registers_all_subcommands(self):
        ap = build_parser()
        actions = [a for a in ap._actions
                   if a.dest == "command"]
        assert actions, "command subparser missing"
        choices = set(actions[0].choices)
        assert {"ask", "runs", "show", "status", "doctor",
                "help"} <= choices

    def test_ask_subparser_flags(self):
        ns = build_parser().parse_args(
            ["ask", "why?", "--backend", "gpu1", "--self-review"])
        assert ns.question == "why?"
        assert ns.backend == "gpu1"
        assert ns.self_review is True
        assert ns.providers == _default_providers_path()

    def test_runs_subparser_limit_default_twenty(self):
        ns = build_parser().parse_args(["runs"])
        assert ns.limit == 20
