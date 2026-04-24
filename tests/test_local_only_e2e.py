"""
Integration test: run scripts/local_only_e2e.py as a subprocess, parse
the trailing JSON summary line, and assert PASS.

Why a subprocess (not pytest-importing the module)?
    The E2E spawns ``api.py`` with its own environment and spins up the
    real FastAPI lifespan stack (DB migrations, ladder warmup, loops).
    Doing that inside the pytest process would leak global state across
    every other test in the suite. Run it as a black box, parse the
    final ``E2E_SUMMARY_JSON {...}`` line, and assert.

The test is marked as ``slow`` and auto-skips when:
    - the required Python runtime / deps aren't importable, OR
    - the user sets ``CALLISTO_SKIP_E2E=1`` (CI-friendly toggle).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "local_only_e2e.py"

SKIP_ENV = "CALLISTO_SKIP_E2E"
TIMEOUT_SECONDS = int(os.getenv("CALLISTO_E2E_TIMEOUT", "600"))


pytestmark = [pytest.mark.slow]


def _import_probe() -> str | None:
    """Return a skip reason if the E2E cannot even start locally."""
    if os.getenv(SKIP_ENV, "").strip() in ("1", "true", "yes", "on"):
        return f"{SKIP_ENV}=1 in environment"
    # The E2E boots api.py, which pulls fastapi/uvicorn/aiosqlite. If
    # those aren't installed in the test environment, skip early with a
    # clean message instead of timing out.
    for mod in ("fastapi", "uvicorn", "aiosqlite"):
        try:
            __import__(mod)
        except Exception as e:
            return f"required module {mod!r} not importable: {e!r}"
    if not SCRIPT.exists():
        return f"E2E script missing: {SCRIPT}"
    return None


def _extract_summary(stdout: str) -> dict | None:
    """Find the last 'E2E_SUMMARY_JSON {...}' line and parse it."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("E2E_SUMMARY_JSON "):
            payload = line[len("E2E_SUMMARY_JSON "):]
            try:
                return json.loads(payload)
            except Exception:
                return None
    return None


def test_local_only_e2e_pass():
    reason = _import_probe()
    if reason:
        pytest.skip(reason)

    cmd = [sys.executable, "-u", str(SCRIPT)]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_API_KEY", None)

    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )

    # Always dump the captured stdout/stderr on failure so the CI log
    # tells us exactly which subsystem tripped.
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    summary = _extract_summary(stdout)
    if summary is None:
        pytest.fail(
            "E2E script did not emit E2E_SUMMARY_JSON line.\n"
            f"returncode={proc.returncode}\n"
            f"---- STDOUT ----\n{stdout[-4000:]}\n"
            f"---- STDERR ----\n{stderr[-2000:]}"
        )

    assert summary.get("result") == "PASS", (
        f"E2E result != PASS: {summary.get('result')!r}\n"
        f"failures: {summary.get('failures')}\n"
        f"subsystems: {summary.get('subsystems')}\n"
        f"---- STDOUT (tail) ----\n{stdout[-4000:]}\n"
        f"---- STDERR (tail) ----\n{stderr[-2000:]}"
    )
    assert proc.returncode == 0, (
        f"E2E script exited with code {proc.returncode} despite PASS summary"
    )

    # Minimum subsystem coverage — the script must have exercised each of
    # these entries. (OK or SKIPPED is fine; missing is a regression.)
    required = {
        "GET /health",
        "GET /system/full-status",
        "POST /task -> /task/{id}",
        "POST /research/collect",
        "POST /research/generate",
        "POST /backtest/run",
        "POST /odds/snapshot/basketball_nba",
        "GET /odds/edges",
        "GET /odds/opportunities",
        "GET /odds/movements",
        "log scan: anthropic.com",
        "log scan: claude-cli spawn",
        "log scan: ERROR/CRITICAL",
    }
    exercised = set(summary.get("subsystems", {}).keys())
    missing = required - exercised
    assert not missing, f"E2E did not exercise: {sorted(missing)}"
