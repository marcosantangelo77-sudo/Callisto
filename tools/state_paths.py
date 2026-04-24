"""
state_paths — Resolve Callisto's fast-lock state directory.

Rationale (2026-04-21 incident):
    OneDrive's cloud sync places an opportunistic lock ("oplock") on files
    inside the synced tree. When the watchdog opens memory/watchdog.lock
    with msvcrt.locking() on Windows, OneDrive can hold the lock indefinitely
    while it negotiates cloud consistency, freezing the process inside
    check_single_instance(). The same hazard applies to restart_requested,
    watchdog.pid, and any other high-churn lock/signal file.

This module centralises the resolver so every component writes small
state files OUTSIDE the OneDrive tree. Heavy persistent data
(memory/callisto.db, embeddings, model caches) stays put — the freeze
risk is specifically on small, frequently-touched lock/signal files.

Resolution order:
    1. $CALLISTO_STATE_DIR         (explicit override — pin anywhere)
    2. $LOCALAPPDATA\\Callisto     (Windows default)
    3. ~/.local/state/callisto     (Unix/fallback default)

The directory is created on first access. Logs subdirectory is created
lazily by state_log_dir(). This module has zero dependencies beyond the
stdlib so it is safe to import from the watchdog (which runs outside the
main venv in some setups).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ENV_OVERRIDE = "CALLISTO_STATE_DIR"


def _default_state_dir() -> Path:
    """Compute the platform-appropriate default state directory."""
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "Callisto"
        # Fallback if LOCALAPPDATA is missing (rare: custom profile setups)
        return Path.home() / "AppData" / "Local" / "Callisto"
    return Path.home() / ".local" / "state" / "callisto"


def state_dir() -> Path:
    """Return the state directory, creating it if necessary.

    Honours $CALLISTO_STATE_DIR for users on shared machines who want
    to pin state somewhere specific (e.g. a RAM disk).
    """
    override = os.environ.get(_ENV_OVERRIDE, "").strip()
    root = Path(override).expanduser() if override else _default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_log_dir() -> Path:
    """Return $STATE_DIR/logs, creating it if necessary."""
    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


# Convenience accessors — one per well-known file.  These are the ONLY
# call sites that should change when this directory layout is revisited.

def restart_signal_path() -> Path:
    """Path of the restart-request signal file."""
    return state_dir() / "restart_requested"


def watchdog_pid_path() -> Path:
    """Path of the watchdog PID file (single-instance diagnostic)."""
    return state_dir() / "watchdog.pid"


def watchdog_lock_path() -> Path:
    """Path of the watchdog singleton lock file."""
    return state_dir() / "watchdog.lock"


def watchdog_heartbeat_path() -> Path:
    """Path of the watchdog liveness heartbeat JSON."""
    return state_dir() / "watchdog_heartbeat.json"


def watchdog_log_path() -> Path:
    """Path of the watchdog's own log file (off OneDrive)."""
    return state_log_dir() / "watchdog.log"


def db_path() -> str:
    """Return the Callisto primary DB path.

    Honours $CALLISTO_DB_PATH first; otherwise uses memory/callisto.db
    relative to the current working directory. This mirrors the legacy
    resolution used by tools/schema.py and api.py so all callers can
    converge on a single accessor without changing behaviour.
    """
    override = os.environ.get("CALLISTO_DB_PATH", "").strip()
    if override:
        return override
    return os.path.join("memory", "callisto.db")


__all__ = [
    "state_dir",
    "state_log_dir",
    "restart_signal_path",
    "watchdog_pid_path",
    "watchdog_lock_path",
    "watchdog_heartbeat_path",
    "watchdog_log_path",
    "db_path",
]
