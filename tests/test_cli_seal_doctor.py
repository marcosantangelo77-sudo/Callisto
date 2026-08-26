"""Tests: callisto doctor fails closed on missing CALLISTO_SEAL_KEY."""
from __future__ import annotations

import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import callisto  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_heavy_checks(tmp_path, monkeypatch):
    """Make doctor deterministic: readable providers config, stubbed externals."""
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


def test_unset_key_fails_closed(capsys, monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rc, out = _doctor(capsys)
    assert rc != 0
    assert "CALLISTO_SEAL_KEY" in out
    assert ("unkeyed" in out.lower()) or ("forgeable" in out.lower())
    assert "FAIL" in out


def test_valid_hex_key_check_passes(capsys, monkeypatch):
    key = "ab" * 32
    monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
    rc, out = _doctor(capsys)
    assert "OK: seal key is set" in out
    assert key not in out  # never dump the secret


def test_invalid_hex_key_fails_without_dumping_value(capsys, monkeypatch):
    bad = "zz" * 32
    monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
    rc, out = _doctor(capsys)
    assert rc != 0
    assert "not valid hex" in out
    assert bad not in out


def test_blank_key_counts_as_unset(capsys, monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
    rc, out = _doctor(capsys)
    assert rc != 0
    assert "not set" in out
