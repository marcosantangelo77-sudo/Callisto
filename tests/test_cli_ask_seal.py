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
