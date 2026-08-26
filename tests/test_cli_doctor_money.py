"""Tests: callisto doctor reports bind host and money switches safely."""
from __future__ import annotations

import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import callisto  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_heavy_checks(tmp_path, monkeypatch):
    """Make doctor deterministic: stubbed externals, as in seal doctor tests."""
    prov = tmp_path / "providers.json"
    prov.write_text('{"default_tier":"t","providers":{}}')
    import inference
    from tools.pipeline import hermes_cli

    def _load(_p):
        return {"default_tier": "t",
                "providers": {"x": {"backend": "b", "model": "m"}}}

    monkeypatch.setattr(inference, "load_providers_config", _load)
    monkeypatch.setattr(hermes_cli, "hermes_available", lambda: False)
    monkeypatch.setattr(callisto, "_db_path", lambda: str(tmp_path / "db.sqlite"))
    import tools.sources.registry as reg_mod

    class _Reg:
        def names(self):
            return ["a"]

    monkeypatch.setattr(reg_mod, "get_source_registry", lambda: _Reg())
    return str(prov)


def _doctor(capsys):
    rc = callisto._cmd_doctor(argparse.Namespace(providers="unused.json"))
    return rc, capsys.readouterr().out


def test_wildcard_bind_fails_closed(capsys, monkeypatch):
    monkeypatch.setenv("CALLISTO_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rc, out = _doctor(capsys)
    assert rc != 0
    assert "FAIL" in out
    assert "== bind ==" in out
    assert "0.0.0.0" in out


def test_default_bind_is_loopback_and_does_not_fail(capsys, monkeypatch):
    monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
    rc, out = _doctor(capsys)
    assert "== bind ==" in out
    assert "127.0.0.1" in out
    # the only FAIL (if any) must not come from the bind section
    bind_out = out.split("== bind ==", 1)[1].split("==", 1)[0]
    assert "FAIL" not in bind_out


def test_money_switches_reported(capsys, monkeypatch):
    for var in ("CALLISTO_LOCAL_ONLY", "CALLISTO_ALLOW_LIVE_EXECUTE",
                "CALLISTO_BIND_HOST", "CALLISTO_SEAL_KEY"):
        monkeypatch.delenv(var, raising=False)
    rc, out = _doctor(capsys)
    assert ("money switches" in out) or ("== money ==" in out)
    assert "CALLISTO_LOCAL_ONLY: off" in out
    assert "CALLISTO_ALLOW_LIVE_EXECUTE: off" in out


def test_bet_executor_init_default_disabled_is_ok(capsys, monkeypatch):
    """Doctor must recognize `        self._enabled = False` (class indent)."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
    monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
    rc, out = _doctor(capsys)
    money = out.split("== money switches ==", 1)[1]
    assert "OK: BetExecutor.__init__ assigns _enabled = False" in money
    assert "FAIL: BetExecutor.__init__" not in money


def test_seal_key_value_never_printed(capsys, monkeypatch):
    key = "deadbeef" * 8
    monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
    monkeypatch.delenv("CALLISTO_BIND_HOST", raising=False)
    rc, out = _doctor(capsys)
    assert key not in out


def test_ipv6_wildcard_bind_fails_closed(capsys, monkeypatch):
    monkeypatch.setenv("CALLISTO_BIND_HOST", "::")
    rc, out = _doctor(capsys)
    assert rc != 0
    bind_out = out.split("== bind ==", 1)[1].split("==", 1)[0]
    assert "FAIL" in bind_out
