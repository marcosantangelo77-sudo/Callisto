"""CLI help must state the fail-closed money defaults."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _help_text():
    r = subprocess.run(
        [sys.executable, str(REPO / "callisto.py"), "--help"],
        capture_output=True, text=True)
    assert r.returncode == 0
    return r.stdout + r.stderr


def test_help_mentions_live_execute_default_off():
    out = _help_text()
    assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in out
    # states it's off by default (fail-closed), not just naming the switch
    assert "OFF" in out or "off" in out.lower().replace("live execution is on", "")


def test_help_mentions_loopback_bind_default():
    out = _help_text()
    assert "loopback" in out and "127.0.0.1" in out


def test_help_mentions_local_only_strips_hosted():
    out = _help_text()
    assert "CALLISTO_LOCAL_ONLY=1" in out
    assert "hosted" in out.lower()


def test_help_is_short_epilog():
    out = _help_text()
    ep = out.split("options:", 1)[-1] if "options:" in out else ""
    lines = [l for l in out.splitlines()
             if "CALLISTO_ALLOW_LIVE_EXECUTE" in l or "loopback" in l]
    # money-defaults mention stays compact: <=6 lines total touching it
    assert len(lines) <= 6


def test_ask_help_inherits_money_defaults():
    r = subprocess.run(
        [sys.executable, str(REPO / "callisto.py"), "ask", "--help"],
        capture_output=True, text=True)
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in out
    assert "OFF" in out or "off" in out.lower()
    assert "loopback" in out and "127.0.0.1" in out
    assert "CALLISTO_LOCAL_ONLY=1" in out
    assert "gpu1" in out


@pytest.mark.parametrize("cmd", ["status", "runs", "show", "doctor", "help"])
def test_subcommand_help_inherits_money_defaults(cmd):
    r = subprocess.run(
        [sys.executable, str(REPO / "callisto.py"), cmd, "--help"],
        capture_output=True, text=True)
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in out
    assert "OFF" in out or "off" in out.lower()
    assert "loopback" in out and "127.0.0.1" in out
    assert "CALLISTO_LOCAL_ONLY=1" in out


def test_module_doc_mentions_local_only():
    """The front-door module docstring is what `pydoc` / GitHub show."""
    text = (REPO / "callisto.py").read_text(encoding="utf-8")
    assert text.startswith('#!/usr/bin/env python3\n"""')
    doc = text.split('"""', 2)[1]
    assert "CALLISTO_LOCAL_ONLY=1" in doc
    assert "--backend gpu1" in doc
    # docstring, not the later argparse epilog
    assert "Hosted inference is stripped" in doc


def test_readme_mentions_front_door():
    """Root READme.md must name the CLI and the hosted-strip switch."""
    text = (REPO / "READme.md").read_text(encoding="utf-8")
    assert "callisto.py ask" in text
    assert "CALLISTO_LOCAL_ONLY=1" in text
    assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in text
    assert "--backend gpu1" in text
    qs = text.split("Quick Start", 1)[1].split("API Endpoints", 1)[0]
    assert "python callisto.py doctor" in qs
    assert "CALLISTO_SEAL_KEY" in qs
    assert "never printed" in qs
    assert qs.find("CALLISTO_SEAL_KEY") < qs.find("python callisto.py doctor")
    assert qs.find("python callisto.py doctor") < qs.find("python api.py")
    # do not document a real-looking key value
    assert not re.search(r"\b[0-9a-fA-F]{32,}\b", qs)
