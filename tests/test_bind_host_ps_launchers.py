"""Ensure PowerShell launchers bind uvicorn to loopback by default."""

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

PS_SCRIPTS = [
    REPO_ROOT / "scripts" / "start-callisto.ps1",
    REPO_ROOT / "scripts" / "watchdog.ps1",
]


@pytest.mark.parametrize("script", PS_SCRIPTS, ids=lambda p: p.name)
def test_no_uvicorn_bind_to_wildcard(script):
    text = script.read_text(encoding="utf-8")
    # The old LAN-exposure hole was passing 0.0.0.0 as the uvicorn --host.
    assert not re.search(r"--host['\"`,\s]+0\.0\.0\.0", text), (
        f"{script.name} still binds uvicorn to the wildcard address"
    )
    assert not re.search(r"0\.0\.0\.0", text), f"{script.name} mentions 0.0.0.0"


@pytest.mark.parametrize("script", PS_SCRIPTS, ids=lambda p: p.name)
def test_uses_callisto_bind_host_with_loopback_default(script):
    text = script.read_text(encoding="utf-8")
    assert "CALLISTO_BIND_HOST" in text, (
        f"{script.name} must honor CALLISTO_BIND_HOST"
    )
    assert "127.0.0.1" in text, f"{script.name} must default to loopback"
    # The default must be loopback, not a silent wildcard fallback
    assert re.search(r'127\.0\.0\.1', text)
