"""Tests for the Hermes latency measurement script (no live calls)."""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_hermes_latency.py"


def test_script_exists():
    assert SCRIPT.is_file()


def test_help_works():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert "--model" in proc.stdout


def test_does_not_import_tools_autonomous():
    src = SCRIPT.read_text()
    assert "tools.autonomous" not in src and "tools/autonomous" not in src
