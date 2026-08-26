"""Characterization tests: the callisto research-appliance front door.

These tests pin the safety contract of `callisto ask` / `callisto doctor`
and the API's public health surface as they exist today. They are
characterization tests: they describe the *current* fail-closed behavior so
any future change that silently weakens it shows up as a red test.

Contract under test
-------------------
1. `ask` refuses to run ANY research when CALLISTO_SEAL_KEY is unset,
   blank, whitespace-only, or non-hex. Refusal means non-zero exit code
   AND no router/engine construction — research must never start.
2. With a valid 64-hex-char key the gate opens and the pipeline runs
   (stubbed here at the _load_router/_make_engine seams).
3. The seal-key VALUE is secret: neither `check_seal_key`, `ask`, nor
   `doctor` ever prints it, even on failure paths.
4. `doctor` prints its three safety panels — == seal ==, == bind ==,
   == money switches == — and fails closed on unkeyed seals and
   non-loopback binds.
5. `/health` in api.py stays PUBLIC (no auth dependency), while the
   deeper health endpoints stay behind require_admin_or_loopback.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import callisto  # noqa: E402
from callisto import build_parser, check_seal_key  # noqa: E402

VALID_KEY = "ab" * 32          # exactly 64 hex chars
KEYS_NEVER_PRINTED = [
    VALID_KEY,
    "deadbeef" * 8,            # other valid hex the env might hold
]


# ── helpers ────────────────────────────────────────────────────────────────

def _args(q="char question", backend=None, self_review=False):
    return argparse.Namespace(
        providers=callisto._default_providers_path(),
        backend=backend, question=q, self_review=self_review)


def _boom_engine(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("engine built despite unkeyed seal")


async def _boom_research(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("research started despite unkeyed seal")


def _wire_boom(monkeypatch):
    """Make every post-gate seam explode: nothing past check_seal_key()
    may execute when the gate refuses."""
    monkeypatch.setattr(callisto, "_load_router",
                        lambda p: (_ for _ in ()).throw(
                            AssertionError("router loaded despite bad seal")))
    monkeypatch.setattr(callisto, "_make_engine", _boom_engine)
    monkeypatch.setattr(callisto, "_result_record", _boom_research)


@pytest.fixture
def runs_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


def _assert_no_key_leak(out: str) -> None:
    for key in KEYS_NEVER_PRINTED:
        assert key not in out, f"seal key value leaked into output: {key[:8]}…"


# ── 1. the seal gate itself ────────────────────────────────────────────────

class TestSealGate:
    def test_missing_key_refused(self, monkeypatch, capsys):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "unkeyed" in out.lower()

    def test_blank_key_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        assert check_seal_key() is False

    def test_whitespace_only_key_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", " \t \n ")
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "not set" in out          # stripped blank == unset, honestly named

    @pytest.mark.parametrize("bad", [
        "zz" * 32,                     # right length, not hex
        "not-hex-at-all",
        "ab" * 31 + "zz",              # hex prefix, junk tail
        "0x" + "a" * 64,               # hex-literal prefix is not hex digits
    ])
    def test_nonhex_keys_refused(self, bad, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        assert check_seal_key() is False
        out = capsys.readouterr().out
        assert "not valid hex" in out
        _assert_no_key_leak(out)

    def test_valid_64_hex_key_accepted(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert check_seal_key() is True

    def test_odd_length_hex_refused(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "abc")
        assert check_seal_key() is False

    def test_surrounding_whitespace_on_valid_key_tolerated(
            self, monkeypatch, capsys):
        """The gate strips surrounding whitespace before validating."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", f"  {VALID_KEY}\n")
        assert check_seal_key() is True


# ── 2. ask fails closed before any research starts ────────────────────────

class TestAskFailsClosed:
    @pytest.fixture(autouse=True)
    def _isolated_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_RUNS_DIR", str(tmp_path / "runs"))
        return tmp_path / "runs"

    @pytest.mark.parametrize("key_env", [None, "", "   ", "nothex"])
    def test_ask_never_starts_research_without_key(
            self, key_env, monkeypatch, capsys):
        if key_env is None:
            monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        else:
            monkeypatch.setenv("CALLISTO_SEAL_KEY", key_env)
        _wire_boom(monkeypatch)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert rc != 0
        assert "FAIL" in out
        # No partial run record may exist either.
        assert list(callisto._runs_dir().glob("*.json")) == []

    def test_ask_exit_code_is_two_for_unkeyed(self, monkeypatch, capsys):
        """Pin the exact code so callers can distinguish refusal from crash."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        _wire_boom(monkeypatch)
        assert asyncio.run(callisto._cmd_ask(_args())) == 2

    def test_unkeyed_refusal_does_not_print_the_attempted_key(
            self, monkeypatch, capsys):
        leaked = "f00d" * 16
        monkeypatch.setenv("CALLISTO_SEAL_KEY", leaked + "zz")  # invalid hex
        _wire_boom(monkeypatch)
        asyncio.run(callisto._cmd_ask(_args()))
        _assert_no_key_leak(capsys.readouterr().out)


# ── 3. happy path requires the keyed gate, then reaches the engine ────────

def _fake_pipeline(monkeypatch, reached, sealed=True):
    from types import SimpleNamespace as NS

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
            return NS(sealed=sealed, refusal_reason="" if sealed else "stub",
                      conclusion="c" if sealed else "",
                      confidence_score=0.5, confidence_tier="SPECULATIVE",
                      leaves=[], fetches=[], objections=[],
                      notes=[], artifact_refs=[])

    router = _Router()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda r, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result, q: {"recorded_at":
                                           "2026-08-26T00:00:00+00:00",
                                           "question": q})
    return router


class TestAskHappyPathIsKeyed:
    def test_valid_key_opens_the_gate_and_runs_research(
            self, monkeypatch, capsys, runs_isolated):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached)
        args = _args("the question")
        rc = asyncio.run(callisto._cmd_ask(args))
        out = capsys.readouterr().out
        assert reached.get("question") == "the question"
        assert rc == 0
        assert "SEALED" in out
        _assert_no_key_leak(out)

    def test_run_record_persisted_under_isolated_dir(
            self, monkeypatch, capsys, runs_isolated):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached)
        asyncio.run(callisto._cmd_ask(_args("persist me")))
        saved = sorted(p.name for p in runs_isolated.glob("*.json"))
        assert len(saved) == 1
        # run id = timestamp + a stable hash of the question
        assert re.match(r"^\d{8}T\d{6}[+\-]\d{4}_\d{4}\.json$", saved[0])

    def test_refused_result_still_exits_nonzero_but_ran(
            self, monkeypatch, capsys, runs_isolated):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        reached = {}
        _fake_pipeline(monkeypatch, reached, sealed=False)
        rc = asyncio.run(callisto._cmd_ask(_args()))
        out = capsys.readouterr().out
        assert reached.get("question") == "char question"  # research ran
        assert rc != 0
        assert "REFUSED" in out

    def test_backend_pin_unknown_tier_refused_after_gate(
            self, monkeypatch, capsys, runs_isolated):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        _fake_pipeline(monkeypatch, {})
        args = _args(backend="does-not-exist")
        rc = asyncio.run(callisto._cmd_ask(args))
        out = capsys.readouterr().out
        assert rc == 2
        assert "unknown provider tier 'does-not-exist'" in out


# ── 4. doctor prints seal/bind/money and never the key ────────────────────

def _run_doctor(providers=None, extra_env=None, monkeypatch=None, capsys=None):
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
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


class TestDoctorPanels:
    def test_keyed_doctor_ok_and_panels_present(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            providers=str(REPO / "config" / "providers.yaml"),
            extra_env={"CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "== seal ==" in out
        assert "== bind ==" in out
        assert "== money switches ==" in out
        assert "HMAC-SHA256" in out
        assert rc == 0
        assert "doctor: OK" in out
        _assert_no_key_leak(out)

    def test_unkeyed_doctor_fails_closed(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "FAIL" in out
        assert "unkeyed" in out
        assert "PROBLEMS FOUND" in out

    def test_nonhex_doctor_fails_closed(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_SEAL_KEY": "banana"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "not valid hex" in out
        _assert_no_key_leak(out)

    def test_wildcard_bind_flagged(self, monkeypatch, capsys):
        rc, out = _run_doctor(
            extra_env={"CALLISTO_BIND_HOST": "0.0.0.0"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert rc != 0
        assert "FAIL" in out
        assert "0.0.0.0" in out           # the dangerous value is named…
        _assert_no_key_leak(out)          # …but never the seal key

    def test_loopback_default_reported_ok(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_BIND_HOST": None,
                       "CALLISTO_SEAL_KEY": VALID_KEY},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "host: 127.0.0.1" in out
        assert "loopback default" in out

    def test_money_switches_reported(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_LOCAL_ONLY": "1"},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "OrderManager.__init__ defaults _enabled = False" in out
        assert "BetExecutor.__init__ assigns _enabled = False" in out
        assert "CALLISTO_LOCAL_ONLY: on" in out
        assert "CALLISTO_ALLOW_LIVE_EXECUTE:" in out

    def test_live_execute_switch_visibility(self, monkeypatch, capsys):
        _, out = _run_doctor(
            extra_env={"CALLISTO_ALLOW_LIVE_EXECUTE": None},
            monkeypatch=monkeypatch, capsys=capsys)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out

    def test_doctor_never_prints_seal_key_value_anywhere(
            self, monkeypatch, capsys):
        """Even in the OK path where the key is validated, only its
        presence is reported — never its value."""
        secret = "1234abcd" * 8
        monkeypatch.setenv("CALLISTO_SEAL_KEY", secret)
        _, out = _run_doctor(monkeypatch=monkeypatch, capsys=capsys)
        assert secret not in out
        assert "seal key is set" in out


# ── 5. /health stays public in api.py ──────────────────────────────────────

class TestHealthEndpointSurface:
    """Static characterization of api.py's route table: the base /health
    endpoint must carry NO auth dependency (sentinel/watchdog poll it),
    while deeper health surfaces keep require_admin_or_loopback."""

    @staticmethod
    @pytest.fixture(scope="class")
    def api_source():
        return (REPO / "api.py").read_text(encoding="utf-8")

    _DECOR_RE = re.compile(r'@app\.(get|post|put|delete)\("([^"]+)"'
                           r'(\s*,\s*dependencies=\[([^\]]*)\])?\s*\)')
    _AUTH_MARKERS = ("require_admin_or_loopback", "require_admin",
                     "verify_token", "Depends(auth")

    def _routes(self, src):
        return [(m.group(2), m.group(4) or "") for m in self._DECOR_RE.finditer(src)]

    def test_health_route_exists_and_is_public(self, api_source):
        routes = dict(self._routes(api_source))
        assert "/health" in routes, "/health route missing from api.py"
        deps = routes["/health"]
        for marker in self._AUTH_MARKERS:
            assert marker not in deps, \
                f"/health gained auth dependency ({marker}) — sentinel breaks"

    def test_health_decorator_line_has_no_dependencies(self, api_source):
        line = next(ln for ln in api_source.splitlines()
                    if '@app.get("/health")' in ln)
        assert "dependencies" not in line
        assert "require_" not in line

    def test_deep_health_endpoints_remain_gated(self, api_source):
        gated = {path: deps for path, deps in self._routes(api_source)
                 if path.startswith("/health/") and "detailed" in path
                 or path in ("/health/deep", "/health/integrity/history")}
        assert "/health/detailed" in gated
        for path, deps in gated.items():
            assert "require_admin_or_loopback" in deps, \
                f"{path} lost its auth dependency"

    def test_health_handler_does_not_require_seal_key(self, api_source):
        """The handler body must not gate on CALLISTO_SEAL_KEY."""
        m = re.search(r'@app\.get\("/health"\)\nasync def health_check\(\).*?'
                      r'(?=\n@app\.|\Z)', api_source, re.S)
        assert m, "health_check handler not found"
        assert "CALLISTO_SEAL_KEY" not in m.group(0)
