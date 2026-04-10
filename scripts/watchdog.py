"""
Callisto Watchdog — bulletproof API auto-restart.

This is the SINGLE process responsible for keeping the Callisto API alive.
It replaces the previous multi-layered approach (sentinel.py + watchdog.bat)
with one simple, robust Python process that:

  1. Health-checks localhost:8420/health every 15 seconds
  2. After 3 consecutive failures, kills stale processes on port 8420
  3. Restarts the API with full error logging
  4. Handles the restart_requested signal file
  5. NEVER gives up — always restarts, with exponential backoff on repeated failures
  6. Logs everything to logs/watchdog.log
  7. Survives terminal close (uses CREATE_NO_WINDOW on Windows)

Run: python scripts/watchdog.py
Or:  pythonw scripts/watchdog.py  (fully headless)

The watchdog itself is kept alive by:
  - Windows Task Scheduler (created by scripts/install_watchdog.bat)
  - The start_callisto.bat script (belt and suspenders)
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──

CALLISTO_DIR = Path(__file__).parent.parent.resolve()
API_URL = "http://localhost:8420"
HEALTH_ENDPOINT = f"{API_URL}/health"
LOG_DIR = CALLISTO_DIR / "logs"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
RESTART_SIGNAL = CALLISTO_DIR / "memory" / "restart_requested"
API_SCRIPT = CALLISTO_DIR / "api.py"

# Timing
POLL_INTERVAL = 15          # Health check every 15 seconds
FAILURE_THRESHOLD = 3       # 3 consecutive failures before restart
STARTUP_GRACE = 30          # Wait 30s after starting API before checking again
MIN_BACKOFF = 15            # Minimum restart delay
MAX_BACKOFF = 120           # Maximum restart delay (2 minutes)
PORT = 8420

# ── Logging ──

os.makedirs(LOG_DIR, exist_ok=True)

# Rotating-style log: truncate if > 5MB
if WATCHDOG_LOG.exists() and WATCHDOG_LOG.stat().st_size > 5 * 1024 * 1024:
    try:
        backup = WATCHDOG_LOG.with_suffix(".log.old")
        if backup.exists():
            backup.unlink()
        WATCHDOG_LOG.rename(backup)
    except Exception:
        pass

# Use line-buffered file handler to ensure logs are written immediately
_file_handler = logging.FileHandler(WATCHDOG_LOG, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s [WATCHDOG] %(levelname)s: %(message)s")
_file_handler.setFormatter(_formatter)
_stream_handler.setFormatter(_formatter)

logger = logging.getLogger("watchdog")
logger.setLevel(logging.INFO)
logger.addHandler(_file_handler)
logger.addHandler(_stream_handler)


def log_flush():
    """Flush all log handlers to ensure output is written to disk."""
    for handler in logger.handlers:
        handler.flush()


# ── PID file for single-instance enforcement ──

PID_FILE = CALLISTO_DIR / "memory" / "watchdog.pid"


LOCK_FILE = CALLISTO_DIR / "memory" / "watchdog.lock"
_lock_fh = None  # Keep file handle alive for the process lifetime


def check_single_instance():
    """Ensure only one watchdog runs using an exclusive file lock.

    On Windows uses msvcrt.locking, on Unix uses fcntl.flock.
    The lock is held for the entire process lifetime — if a second watchdog
    starts, it will fail to acquire the lock and exit immediately.
    """
    global _lock_fh

    # Step 1: Kill any watchdog registered in the PID file (handles pre-lock stale instances)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            if old_pid != os.getpid():
                if sys.platform == "win32":
                    try:
                        result = subprocess.run(
                            ["taskkill", "/F", "/PID", str(old_pid)],
                            capture_output=True, text=True, timeout=5,
                        )
                        if result.returncode == 0:
                            logger.warning(f"Killed old watchdog PID {old_pid}")
                            time.sleep(2)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    try:
                        os.kill(old_pid, 0)
                        logger.warning(f"Killing old watchdog PID {old_pid}")
                        os.kill(old_pid, signal.SIGTERM)
                        time.sleep(2)
                    except ProcessLookupError:
                        pass
        except (ValueError, OSError):
            pass

    # Step 2: Acquire exclusive file lock — this is the real singleton gate
    try:
        _lock_fh = open(LOCK_FILE, "w", encoding="utf-8")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except (IOError, OSError) as e:
        logger.error(f"Another watchdog is already running (lock held). Exiting. ({e})")
        sys.exit(42)  # Distinct code: lock held = another instance running

    # Step 3: Write PID file for diagnostic purposes
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        logger.warning(f"Could not write PID file: {e}")


# ── Health check ──

def check_health() -> bool:
    """
    Check if the API is responding. Returns True if healthy.
    Uses raw urllib to avoid any dependency on httpx/requests.
    """
    try:
        req = urllib.request.Request(HEALTH_ENDPOINT, method="GET")
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status == 200:
            data = json.loads(resp.read().decode())
            return data.get("healthy", False)
        return False
    except Exception:
        return False


# ── Port management ──

def kill_port_8420():
    """Kill any process listening on port 8420. Handles Windows and Unix."""
    logger.info("Killing stale processes on port 8420...")

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=15,
            )
            killed = set()
            for line in result.stdout.splitlines():
                if f":{PORT}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    if pid.isdigit() and pid not in killed:
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=10,
                        )
                        logger.info(f"Killed PID {pid} on port {PORT}")
                        killed.add(pid)
            if killed:
                time.sleep(3)  # Let the port release
        except Exception as e:
            logger.error(f"Failed to kill port {PORT}: {e}")
    else:
        try:
            subprocess.run(
                ["fuser", "-k", f"{PORT}/tcp"],
                capture_output=True, timeout=10,
            )
            time.sleep(2)
        except Exception as e:
            logger.error(f"Failed to kill port {PORT}: {e}")


def is_port_free() -> bool:
    """Check if port 8420 is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", PORT))
            return result != 0  # 0 = port is in use
    except Exception:
        return True


def wait_for_port_free(max_wait: int = 90):
    """Wait until port 8420 is free, up to max_wait seconds.

    Windows holds TCP sockets in TIME_WAIT for up to 4 minutes.
    The previous 15s timeout was the #1 cause of crash-loops —
    the port wasn't free yet when the new process tried to bind.
    """
    for i in range(max_wait):
        if is_port_free():
            return True
        if i % 10 == 0 and i > 0:
            logger.info(f"Port {PORT} still in TIME_WAIT ({i}s elapsed)...")
        time.sleep(1)
    return False


# ── API process management ──

def find_python():
    """Find the correct Python executable (venv or system)."""
    venv_python = CALLISTO_DIR / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def start_api() -> subprocess.Popen:
    """
    Start the Callisto API process with full error logging.
    Returns the Popen handle.
    """
    python = find_python()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Log files for the API process
    stdout_log = LOG_DIR / f"api_stdout_{timestamp}.log"
    stderr_log = LOG_DIR / f"api_stderr_{timestamp}.log"

    # Clean up old API log files (keep last 10)
    cleanup_old_logs("api_stdout_", 10)
    cleanup_old_logs("api_stderr_", 10)

    logger.info(f"Starting API: {python} {API_SCRIPT}")
    logger.info(f"  stdout -> {stdout_log}")
    logger.info(f"  stderr -> {stderr_log}")

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    stdout_f = open(stdout_log, "w", encoding="utf-8")
    stderr_f = open(stderr_log, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [python, str(API_SCRIPT)],
        cwd=str(CALLISTO_DIR),
        stdout=stdout_f,
        stderr=stderr_f,
        creationflags=creation_flags,
    )

    logger.info(f"API process started: PID {proc.pid}")
    return proc


def cleanup_old_logs(prefix: str, keep: int):
    """Remove old log files matching prefix, keeping the newest `keep` files."""
    try:
        matching = sorted(
            LOG_DIR.glob(f"{prefix}*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for old_file in matching[keep:]:
            try:
                old_file.unlink()
            except Exception:
                pass
    except Exception:
        pass


def get_recent_api_errors() -> str:
    """Read the most recent API stderr log for crash diagnostics."""
    try:
        stderr_files = sorted(
            LOG_DIR.glob("api_stderr_*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if stderr_files:
            content = stderr_files[0].read_text(encoding="utf-8", errors="replace")
            if content.strip():
                return content[-2000:]  # Last 2KB
    except Exception:
        pass
    return "(no stderr output)"


# ── Restart signal handling ──

def check_restart_signal() -> bool:
    """Check if a restart was requested via signal file. Returns True if restart needed."""
    if RESTART_SIGNAL.exists():
        try:
            reason = RESTART_SIGNAL.read_text(encoding="utf-8").strip() or "code reload"
            RESTART_SIGNAL.unlink()
            logger.info(f"RESTART SIGNAL: {reason}")
            return True
        except Exception as e:
            logger.error(f"Failed to read restart signal: {e}")
            try:
                RESTART_SIGNAL.unlink()
            except Exception:
                pass
    return False


# ── Main loop ──

def main():
    """Main watchdog loop. Runs forever. Never gives up."""
    logger.info("=" * 60)
    logger.info("Callisto Watchdog started")
    logger.info(f"  PID:       {os.getpid()}")
    logger.info(f"  API:       {API_URL}")
    logger.info(f"  Poll:      every {POLL_INTERVAL}s")
    logger.info(f"  Threshold: {FAILURE_THRESHOLD} consecutive failures")
    logger.info(f"  API script: {API_SCRIPT}")
    logger.info(f"  Log:       {WATCHDOG_LOG}")
    logger.info("=" * 60)
    log_flush()

    check_single_instance()
    logger.info("Single instance check passed")
    log_flush()

    consecutive_failures = 0
    consecutive_restarts = 0
    api_proc = None
    last_restart_time = 0
    last_healthy_time = time.time()

    while True:
        try:
            # ── Check for restart signal ──
            if check_restart_signal():
                logger.info("Restart requested — killing API for reload")
                kill_port_8420()
                if api_proc and api_proc.poll() is None:
                    try:
                        api_proc.terminate()
                        api_proc.wait(timeout=10)
                    except Exception:
                        try:
                            api_proc.kill()
                        except Exception:
                            pass
                api_proc = None

                if not wait_for_port_free(30):
                    logger.warning("Port still occupied after kill — retrying")
                    kill_port_8420()
                    wait_for_port_free(15)

                try:
                    api_proc = start_api()
                    last_restart_time = time.time()
                    consecutive_failures = 0
                    consecutive_restarts = 0
                    logger.info(f"Waiting {STARTUP_GRACE}s for API startup...")
                    time.sleep(STARTUP_GRACE)
                    if check_health():
                        logger.info("API restarted successfully after code reload")
                        log_flush()
                    else:
                        logger.warning("API started but health check failed — will monitor")
                        consecutive_failures = 1
                except Exception as e:
                    logger.error(f"Failed to restart API after code reload: {e}")
                    consecutive_failures = FAILURE_THRESHOLD
                continue

            # ── Health check ──
            healthy = check_health()

            if healthy:
                if consecutive_failures > 0:
                    logger.info(f"API recovered after {consecutive_failures} failures")
                    log_flush()
                consecutive_failures = 0
                consecutive_restarts = 0
                last_healthy_time = time.time()

                # Log periodic status (every ~5 minutes)
                if int(time.time()) % 300 < POLL_INTERVAL:
                    uptime_min = (time.time() - last_restart_time) / 60 if last_restart_time else 0
                    logger.info(f"API healthy (uptime ~{uptime_min:.0f}m)")
                    log_flush()

            else:
                consecutive_failures += 1
                logger.warning(
                    f"Health check FAILED ({consecutive_failures}/{FAILURE_THRESHOLD})"
                )
                log_flush()

                # Check if we also lost the process handle
                if api_proc and api_proc.poll() is not None:
                    exit_code = api_proc.returncode
                    logger.error(f"API process exited with code {exit_code}")

                    # Log stderr from the crash
                    errors = get_recent_api_errors()
                    if errors != "(no stderr output)":
                        logger.error(f"API stderr: {errors[:500]}")

                    api_proc = None

            # ── Restart if needed ──
            if consecutive_failures >= FAILURE_THRESHOLD:
                # Calculate backoff
                backoff = min(MIN_BACKOFF * (2 ** min(consecutive_restarts, 3)), MAX_BACKOFF)
                time_since_restart = time.time() - last_restart_time

                if time_since_restart < backoff and last_restart_time > 0:
                    remaining = backoff - time_since_restart
                    logger.info(
                        f"Waiting {remaining:.0f}s before restart "
                        f"(backoff: {backoff}s, attempt #{consecutive_restarts + 1})"
                    )
                    time.sleep(min(remaining, POLL_INTERVAL))
                    continue

                logger.info(f"=== RESTARTING API (attempt #{consecutive_restarts + 1}) ===")
                log_flush()

                # Step 0: If repeated crashes (5+), free system memory before retry
                if consecutive_restarts >= 5:
                    logger.warning(
                        f"Repeated crashes ({consecutive_restarts}) — freeing system memory"
                    )
                    try:
                        # Unload Ollama models to free VRAM/RAM
                        import urllib.request
                        for model in ["devstral-small-2", "gemma4"]:
                            try:
                                req = urllib.request.Request(
                                    "http://localhost:11434/api/generate",
                                    data=json.dumps({"model": model, "keep_alive": "0"}).encode(),
                                    headers={"Content-Type": "application/json"},
                                    method="POST",
                                )
                                urllib.request.urlopen(req, timeout=10)
                                logger.info(f"Unloaded {model} from VRAM")
                            except Exception:
                                pass
                        # Give OS time to reclaim memory
                        time.sleep(5)
                    except Exception as e:
                        logger.warning(f"Memory cleanup failed: {e}")

                # Step 1: Kill anything on port 8420
                kill_port_8420()

                # Step 2: Wait for port to be free (up to 90s for TIME_WAIT)
                if not wait_for_port_free(90):
                    logger.error("Port 8420 still occupied after 90s — force continuing")

                # Step 3: Start the API
                try:
                    api_proc = start_api()
                    last_restart_time = time.time()
                    consecutive_restarts += 1
                    consecutive_failures = 0

                    # Grace period for startup
                    logger.info(f"Waiting {STARTUP_GRACE}s for API startup...")
                    time.sleep(STARTUP_GRACE)

                    # Verify it started
                    if api_proc.poll() is not None:
                        logger.error(
                            f"API died during startup (exit code: {api_proc.returncode})"
                        )
                        errors = get_recent_api_errors()
                        logger.error(f"Startup error: {errors[:1000]}")
                        api_proc = None
                        consecutive_failures = FAILURE_THRESHOLD  # Will retry next loop
                    elif check_health():
                        logger.info("API started successfully and is healthy")
                        consecutive_restarts = 0  # Reset backoff on success
                        log_flush()
                    else:
                        logger.warning("API process running but health check failed — will retry")
                        # Don't reset failures, let it try again next loop
                        consecutive_failures = 1

                except Exception as e:
                    logger.error(f"Failed to start API: {e}")
                    consecutive_failures = FAILURE_THRESHOLD

                continue  # Skip the normal sleep, we already waited

        except KeyboardInterrupt:
            logger.info("Watchdog stopped by user (Ctrl+C)")
            break
        except Exception as e:
            logger.error(f"Watchdog loop error (recovering): {e}")
            # Never crash — log and continue
            time.sleep(POLL_INTERVAL)
            continue

        time.sleep(POLL_INTERVAL)

    # Cleanup
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("Watchdog exited")


if __name__ == "__main__":
    main()
