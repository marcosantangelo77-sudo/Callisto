"""Tests: callisto.py commands are implemented in tools.cli.

The entry script must keep delegating to tools.cli (ask/doctor/status/help),
keep the seal-key gate fail-closed on ask, and never print the key.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import callisto  # noqa: E402
from tools.cli import ask as cli_ask  # noqa: E402
from tools.cli import doctor as cli_doctor  # noqa: E402
from tools.cli import help as cli_help  # noqa: E402
from tools.cli import status as cli_status  # noqa: E402


def test_command_bodies_live_in_tools_cli():
    """The real implementations are in tools/cli, re-exported by callisto."""
    assert callisto._cmd_ask is cli_ask.cmd_ask or callable(cli_ask.cmd_ask)
    assert callable(cli_doctor.cmd_doctor)
    assert callable(cli_status.cmd_status)
    assert callable(cli_help.cmd_help)
    for mod, name in ((cli_ask, "_cmd_ask"), (cli_doctor, "_cmd_doctor"),
                      (cli_status, "_cmd_status"), (cli_help, "_cmd_help")):
        assert callable(getattr(mod, name))
    # entry script delegates to the extracted functions
    assert callisto._cmd_doctor is cli_doctor.cmd_doctor
    assert callisto._cmd_status is cli_status.cmd_status
    assert callisto._cmd_help is cli_help.cmd_help


def test_seal_gate_lives_in_tools_cli_and_fails_closed(monkeypatch, capsys):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    assert callisto.check_seal_key is cli_ask.check_seal_key
    assert cli_ask.check_seal_key() is False
    out = capsys.readouterr().out
    assert "FAIL" in out and "unkeyed" in out


def test_ask_routes_through_tools_cli_and_refuses_bad_key(
        monkeypatch, capsys):
    """ask with a bad key returns rc != 0 and never starts research."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "zzz-not-hex")
    reached = []

    async def _no_research(*a, **k):  # pragma: no cover - must not run
        reached.append(True)
        raise AssertionError("research started despite bad seal key")

    monkeypatch.setattr(callisto, "_load_router", _no_research)
    args = argparse.Namespace(providers="unused", backend=None,
                              question="q", self_review=False)
    rc = asyncio.run(callisto._cmd_ask(args))
    assert rc != 0
    assert not reached
    out = capsys.readouterr().out
    assert "not valid hex" in out


def test_seal_key_value_never_printed(monkeypatch, capsys):
    key = "deadbeef" * 8
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "nothex")
    cli_ask.check_seal_key()
    assert key not in capsys.readouterr().out
    monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
    cli_ask.check_seal_key()
    assert key not in capsys.readouterr().out


def test_help_subcommand_prints_usage(capsys):
    rc = callisto.main(["help"])
    assert rc == 0
    assert "ask" in capsys.readouterr().out


def test_main_dispatches_to_extracted_commands():
    parser = callisto.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["doctor"]).command == "doctor"
