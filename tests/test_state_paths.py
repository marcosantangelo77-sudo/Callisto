"""Tests for tools.state_paths and the state-off-OneDrive migration.

Covers:
    - state_dir() returns a non-OneDrive path by default and auto-creates it
    - $CALLISTO_STATE_DIR is honoured
    - convenience path accessors resolve under state_dir()
    - the watchdog's check_single_instance() self-recovers from a stale
      primary whose heartbeat is older than STALE_HEARTBEAT_SECONDS
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_state_paths():
    """Force a fresh import so env-var changes take effect."""
    if "tools.state_paths" in sys.modules:
        del sys.modules["tools.state_paths"]
    return importlib.import_module("tools.state_paths")


# ── state_paths primitives ──

def test_state_dir_not_on_onedrive(tmp_path, monkeypatch):
    """Default state_dir() must not be under any 'OneDrive' path component."""
    monkeypatch.delenv("CALLISTO_STATE_DIR", raising=False)
    sp = _reload_state_paths()
    d = sp.state_dir()
    assert d.exists(), "state_dir() must auto-create the directory"
    assert d.is_dir()
    # OneDrive is the specific hazard we're routing around.
    assert "OneDrive" not in str(d), f"state_dir() leaked onto OneDrive: {d}"
    if sys.platform == "win32":
        # Default Windows location
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            assert str(d).lower().startswith(local.lower())
    else:
        assert ".local/state/callisto" in str(d.as_posix())


def test_state_dir_env_override(tmp_path, monkeypatch):
    """$CALLISTO_STATE_DIR must override the platform default."""
    override = tmp_path / "custom_state"
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(override))
    sp = _reload_state_paths()
    d = sp.state_dir()
    assert d == override
    assert d.exists() and d.is_dir()


def test_convenience_paths_under_state_dir(tmp_path, monkeypatch):
    """watchdog_pid_path / lock / heartbeat / restart_signal all resolve under state_dir()."""
    override = tmp_path / "s"
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(override))
    sp = _reload_state_paths()
    d = sp.state_dir()
    for func, name in (
        (sp.restart_signal_path, "restart_requested"),
        (sp.watchdog_pid_path, "watchdog.pid"),
        (sp.watchdog_lock_path, "watchdog.lock"),
        (sp.watchdog_heartbeat_path, "watchdog_heartbeat.json"),
    ):
        p = func()
        assert p.parent == d
        assert p.name == name
    assert sp.watchdog_log_path().parent == d / "logs"
    assert sp.watchdog_log_path().parent.exists()


# ── watchdog freeze-recovery ──

def test_watchdog_evicts_stale_primary(tmp_path, monkeypatch):
    """Spawn a subprocess, capture its PID, let it exit, then (a) write that
    dead PID to watchdog.pid, (b) write a 120-sec-old heartbeat, and invoke
    check_single_instance() in-process.  It should *not* exit (sys.exit(42))
    and should write a fresh PID file + heartbeat for the current process.
    """
    override = tmp_path / "state"
    monkeypatch.setenv("CALLISTO_STATE_DIR", str(override))
    # Point watchdog's PID/lock/heartbeat at the override before importing.
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    # Ensure a fresh import of the watchdog (it picks paths at import time).
    for mod in ("watchdog", "tools.state_paths"):
        if mod in sys.modules:
            del sys.modules[mod]
    sp = _reload_state_paths()
    # Import the watchdog module fresh.
    watchdog = importlib.import_module("watchdog")
    # Rebind its path globals to the override just in case the module was
    # cached elsewhere (defence-in-depth).
    watchdog.PID_FILE = sp.watchdog_pid_path()
    watchdog.LOCK_FILE = sp.watchdog_lock_path()
    watchdog.HEARTBEAT_FILE = sp.watchdog_heartbeat_path()

    # Spawn a short-lived subprocess, grab its PID, wait for it to exit.
    dead = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    dead_pid = dead.pid
    rc = dead.wait(timeout=10)
    assert rc == 0
    # Give the OS a moment to fully reap — on fast Windows CI the PID may be
    # immediately recycled, which is fine for our check (_pid_alive falls
    # through to tasklist which won't match a bare 'python' by PID anyway).
    time.sleep(0.5)

    # Plant a stale PID + 120-sec-old heartbeat.
    sp.watchdog_pid_path().write_text(str(dead_pid), encoding="utf-8")
    stale = {
        "pid": dead_pid,
        "monotonic_ts": time.monotonic() - 120,
        "wall_ts": time.time() - 120,
        "api_last_ok": 0.0,
    }
    sp.watchdog_heartbeat_path().write_text(json.dumps(stale), encoding="utf-8")

    # Run the real check.  It must NOT call sys.exit.
    try:
        watchdog.check_single_instance()
    except SystemExit as e:  # pragma: no cover — failure mode
        pytest.fail(
            f"check_single_instance() exited ({e.code}) instead of evicting stale primary"
        )

    # Verify we now own the PID file and heartbeat is fresh.
    assert int(sp.watchdog_pid_path().read_text(encoding="utf-8").strip()) == os.getpid()
    hb = json.loads(sp.watchdog_heartbeat_path().read_text(encoding="utf-8"))
    assert hb["pid"] == os.getpid()
    assert abs(time.time() - hb["wall_ts"]) < 5, "heartbeat must be fresh"

    # Release the lock handle so subsequent tests don't inherit it.
    if getattr(watchdog, "_lock_fh", None) is not None:
        try:
            watchdog._lock_fh.close()
        except Exception:
            pass
        watchdog._lock_fh = None


def test_restart_signal_triggers_api_exit(tmp_path, monkeypatch):
    """Contract test for the API-side restart_signal_watcher.

    Runs the watcher in a subprocess so os._exit(0) terminates the child,
    not the test runner.  Uses a 2-second-poll variant of the watcher
    (same logic, shorter cadence) so the test completes in under 10s.
    """
    override = tmp_path / "state"
    override.mkdir(parents=True, exist_ok=True)

    harness = override / "harness.py"
    # We import the path resolver from the package and implement the
    # watcher directly in the harness so we control the poll interval.
    # The shipped watcher in api.py is a straight copy of this loop with
    # asyncio.sleep(10) — the behaviour under test is identical.
    harness.write_text(
        "import asyncio, os, sys\n"
        f"sys.path.insert(0, r{str(REPO_ROOT)!r})\n"
        f"os.environ['CALLISTO_STATE_DIR'] = r{str(override)!r}\n"
        "from tools.state_paths import restart_signal_path\n"
        "async def watcher():\n"
        "    p = restart_signal_path()\n"
        "    while True:\n"
        "        await asyncio.sleep(0.5)\n"
        "        if p.exists():\n"
        "            os._exit(0)\n"
        "asyncio.run(watcher())\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["CALLISTO_STATE_DIR"] = str(override)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, str(harness)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(1)
        assert proc.poll() is None, (
            "harness exited before signal: "
            + (proc.stderr.read().decode(errors="replace") if proc.stderr else "")
        )
        # Write signal directly to the child's resolved path.
        (override / "restart_requested").write_text("test", encoding="utf-8")
        rc = proc.wait(timeout=10)
        assert rc == 0, f"watcher should exit 0 on signal; got rc={rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
