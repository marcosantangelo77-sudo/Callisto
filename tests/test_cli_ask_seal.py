"""Tests: callisto ask refuses to run with unkeyed/forgeable seals."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import callisto  # noqa: E402

VALID_KEY = "ab" * 32


@pytest.fixture
def args():
    return argparse.Namespace(providers="unused", backend=None,
                              question="q", self_review=False)


def _run(args, monkeypatch, capsys):
    calls = []

    async def _no_research(*a, **k):  # pragma: no cover - must not run
        calls.append(a)
        raise AssertionError("research started despite bad seal key")

    monkeypatch.setattr(callisto, "_load_router", _no_research)
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    return rc, out


def test_ask_missing_key_fails_closed(monkeypatch, capsys, args):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rc, out = _run(args, monkeypatch, capsys)
    assert rc != 0
    assert "FAIL" in out and "unkeyed" in out


def test_ask_blank_key_fails_closed(monkeypatch, capsys, args):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
    rc, out = _run(args, monkeypatch, capsys)
    assert rc != 0
    assert "unkeyed" in out


def test_ask_nonhex_key_fails_closed(monkeypatch, capsys, args):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
    rc, out = _run(args, monkeypatch, capsys)
    assert rc != 0
    assert "not valid hex" in out


def test_ask_valid_hex_key_proceeds(monkeypatch, capsys, args):
    """With a valid hex key the gate passes; research is stubbed fast."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    reached = {}

    class _FakeResult:
        sealed = False
        confidence_score = 0.0
        confidence_tier = "UNVERIFIED"
        refusal_reason = "stub"
        leaves = []
        fetches = []
        objections = []
        notes = []
        artifact_refs = []

    class _Router:
        endpoints = {}
        task_classes = {}
        default_tier_name = None
        cost_ledger = None

        async def check_health(self, name):
            return {"status": "ok"}

    class _Ledger:
        def snapshot(self):
            return {"by_tier": {}}

    class _Engine:
        async def run(self, q):
            reached["ran"] = True
            return _FakeResult()

    router = _Router()
    router.cost_ledger = _Ledger()
    monkeypatch.setattr(callisto, "_load_router", lambda p: router)
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result, q: {"recorded_at": "2026-01-01T00:00"})
    monkeypatch.setattr(callisto, "_runs_dir", lambda: None)
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert reached.get("ran") is True
    assert rc != 0  # stub result refused, but research DID run
    assert "REFUSED" in out


def test_ask_local_only_refuses_hosted_backend_before_health(
        monkeypatch, capsys, args):
    """CALLISTO_LOCAL_ONLY must not health-check a hosted --backend."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []
    engines = []

    class _Router:
        endpoints = ["gpu1", "ox_alpha"]
        task_classes = {"decompose": "gpu1"}
        default_tier_name = "gpu1"
        cost_ledger = None

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    class _Engine:
        async def run(self, q):  # pragma: no cover - must not run
            raise AssertionError("engine ran despite hosted LOCAL_ONLY pin")

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(
        callisto, "_make_engine",
        lambda router, self_review=False: engines.append(_Engine()) or _Engine())
    args.backend = "ox_alpha"
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAIL" in out and "CALLISTO_LOCAL_ONLY" in out
    assert "ox_alpha" in out
    assert health_calls == []
    assert engines == []


def test_ask_local_only_allows_gpu1_pin(monkeypatch, capsys, args):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []

    class _Ledger:
        def snapshot(self):
            return {"by_tier": {}}

    class _Router:
        endpoints = ["gpu1", "ox_alpha"]
        task_classes = {"decompose": "gpu1"}
        default_tier_name = "gpu1"
        cost_ledger = _Ledger()

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    class _FakeResult:
        sealed = False
        confidence_score = 0.0
        confidence_tier = "UNVERIFIED"
        refusal_reason = "stub"
        leaves = []
        fetches = []
        objections = []
        notes = []
        artifact_refs = []

    class _Engine:
        async def run(self, q):
            return _FakeResult()

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result, q: {"recorded_at": "2026-01-01T00:00",
                                           "question": q})
    args.backend = "gpu1"
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert health_calls == ["gpu1"]
    assert rc != 0
    assert "REFUSED" in out
    assert "CALLISTO_LOCAL_ONLY forbids" not in out


def test_ask_local_only_refuses_misconfigured_local_url(
        monkeypatch, capsys, args):
    """llama_cpp_server pointed at OpenRouter is hosted — refuse the pin."""
    from tools.infrouter.config import EndpointConfig

    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []

    poison = EndpointConfig(
        name="poison",
        backend="llama_cpp_server",
        base_url="https://openrouter.ai/api/v1",
        model="stealth/ox-alpha",
    )
    gpu1 = EndpointConfig(
        name="gpu1",
        backend="llama_cpp_server",
        base_url="http://127.0.0.1:8080/v1",
        model="qwen36",
    )

    class _Router:
        endpoints = {"poison": poison, "gpu1": gpu1}
        task_classes = {"decompose": ["gpu1"]}
        default_tier_name = "gpu1"
        cost_ledger = None

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router, self_review=False: (_ for _ in ()).throw(
                            AssertionError("engine built")))
    args.backend = "poison"
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert rc == 2
    assert "CALLISTO_LOCAL_ONLY" in out
    assert health_calls == []


def test_ask_local_only_no_backend_refuses_hosted_only_pool(
        monkeypatch, capsys, args):
    """No --backend + LOCAL_ONLY + only hosted rails must FAIL before
    the engine, without probing check_health."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []
    engines = []

    class _Router:
        endpoints = ["openrouter_ox", "ox_alpha"]
        task_classes = {"decompose": "openrouter_ox"}
        default_tier_name = "openrouter_ox"
        cost_ledger = None

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(
        callisto, "_make_engine",
        lambda router, self_review=False: engines.append("built") or None)
    args.backend = None
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAIL" in out and "no local endpoint" in out
    assert health_calls == []
    assert engines == []


def test_ask_local_only_no_backend_with_gpu1_skips_health(
        monkeypatch, capsys, args):
    """LOCAL_ONLY without --backend still must not probe check_health
    when a local rail exists — candidate chain decides per task."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []

    class _Ledger:
        def snapshot(self):
            return {"by_tier": {}}

    class _Router:
        endpoints = ["gpu1", "ox_alpha"]
        task_classes = {"decompose": "gpu1"}
        default_tier_name = "gpu1"
        cost_ledger = _Ledger()

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    class _FakeResult:
        sealed = False
        confidence_score = 0.0
        confidence_tier = "UNVERIFIED"
        refusal_reason = "stub"
        leaves = []
        fetches = []
        objections = []
        notes = []
        artifact_refs = []

    class _Engine:
        async def run(self, q):
            return _FakeResult()

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router, self_review=False: _Engine())
    monkeypatch.setattr(callisto, "_result_record",
                        lambda result, q: {"recorded_at": "2026-01-01T00:00",
                                           "question": q})
    args.backend = None
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert health_calls == []
    assert rc != 0
    assert "REFUSED" in out
    assert "no local endpoint" not in out


def test_ask_local_only_hosted_only_dict_pool_refused(
        monkeypatch, capsys, args):
    from tools.infrouter.config import EndpointConfig

    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    health_calls = []

    hosted = EndpointConfig(
        name="openrouter_ox",
        backend="openai_compat",
        base_url="https://openrouter.ai/api/v1",
        model="stealth/ox-alpha",
    )

    class _Router:
        endpoints = {"openrouter_ox": hosted}
        task_classes = {"decompose": ["openrouter_ox"]}
        default_tier_name = "openrouter_ox"
        cost_ledger = None

        async def check_health(self, name):
            health_calls.append(name)
            return {"status": "ok"}

    monkeypatch.setattr(callisto, "_load_router", lambda p: _Router())
    monkeypatch.setattr(callisto, "_make_engine",
                        lambda router, self_review=False: (_ for _ in ()).throw(
                            AssertionError("engine built")))
    args.backend = None
    rc = asyncio.run(callisto._cmd_ask(args))
    out = capsys.readouterr().out
    assert rc == 2
    assert "no local endpoint" in out
    assert health_calls == []
