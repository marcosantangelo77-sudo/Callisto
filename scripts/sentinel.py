"""
Sentinel Watchdog — Layer 3 resilience for Callisto.

This is a SEPARATE process from api.py. It runs alongside the main system
and handles the failure modes that the main system cannot fix itself:

  1. Crash loop detection — main process keeps dying on the same bug
  2. Automated bug diagnosis — reads logs, identifies the error
  3. Claude Code escalation — asks Claude to write a fix
  4. Safe patching — git stash, apply fix, run tests, revert on failure
  5. Telegram alert — notifies Marco when fixes succeed or exhaust retries

Architecture:
  - Polls /health endpoint every 30 seconds
  - If HTTP fails, reads memory/health.json (written by Layer 2)
  - If both fail, checks if the process is alive via PID
  - Crash loop = 5 crashes in 10 minutes
  - On crash loop: read last 200 lines of logs, formulate diagnosis prompt,
    call Claude Code CLI directly, apply fix, restart

CRITICAL DESIGN DECISIONS:
  - Minimal dependencies (stdlib + httpx only)
  - Never imports from tools/ (main process may be broken)
  - Has its own error handling (can't rely on main process logging)
  - Protected files list — refuses to modify AGP core, schema, sentinel itself
  - Max 3 fix attempts per unique error — then gives up and alerts Marco
  - Git stash before every patch, revert if tests fail

Run: python scripts/sentinel.py
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──

CALLISTO_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(CALLISTO_DIR))
from tools.state_paths import restart_signal_path  # noqa: E402

API_URL = "http://localhost:8420"
HEALTH_ENDPOINT = f"{API_URL}/health"
HEALTH_FILE = CALLISTO_DIR / "memory" / "health.json"
# Signal file lives off OneDrive to avoid oplock-induced freezes.
RESTART_SIGNAL = restart_signal_path()
LOG_DIR = CALLISTO_DIR / "logs"
SENTINEL_LOG = CALLISTO_DIR / "logs" / "sentinel.log"

# Crash loop detection
CRASH_WINDOW = 600          # 10 minutes
CRASH_THRESHOLD = 5         # 5 crashes in window = crash loop
POLL_INTERVAL = 30          # Check every 30 seconds
HEALTHY_STREAK_RESET = 10   # 10 consecutive healthy checks = reset crash counter

# Fix attempts
MAX_FIX_ATTEMPTS = 3        # Per unique error
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
CLAUDE_TIMEOUT = 300        # 5 minutes for fix generation

# Protected files — NEVER let Claude modify these
PROTECTED_FILES = {
    "agp.py",                   # AGP protocol is non-negotiable
    "scripts/sentinel.py",      # Can't modify yourself
    "scripts/watchdog.bat",     # Can't break the restart loop
    "scripts/setup_always_on.bat",
    "scripts/undo_always_on.bat",
    "memory/callisto.db",       # Binary file
}

# Telegram (optional — works without it)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logging ──

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SENTINEL] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(SENTINEL_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("sentinel")


# ── State ──

class SentinelState:
    def __init__(self):
        self.crash_times: list[float] = []
        self.fix_attempts: dict[str, int] = {}  # error_hash -> attempts
        self.fixes_applied: list[dict] = []
        self.last_healthy = 0.0
        self.healthy_streak = 0
        self.total_checks = 0
        self.total_fixes = 0
        self.total_failed_fixes = 0


state = SentinelState()


# ── Utilities ──

def error_hash(error_msg: str) -> str:
    """Hash an error message for dedup. Strips line numbers and timestamps."""
    # Normalize: remove line numbers, timestamps, memory addresses
    normalized = re.sub(r'line \d+', 'line N', error_msg)
    normalized = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', normalized)
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', 'TIMESTAMP', normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def send_telegram(message: str) -> None:
    """Send Telegram message. Fire-and-forget, never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.request
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"[SENTINEL] {message}",
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.info(f"Telegram notification failed (non-critical): {e}")


def get_recent_logs(n_lines: int = 200) -> str:
    """Read the last N lines from the most recent log file."""
    # Try structured log first, then fall back to any .log file
    log_candidates = [
        CALLISTO_DIR / "logs" / "callisto.log",
        CALLISTO_DIR / "callisto.log",
    ]
    # Also check for any .log files in logs/
    if LOG_DIR.exists():
        for f in sorted(LOG_DIR.glob("*.log"), key=os.path.getmtime, reverse=True):
            if f.name != "sentinel.log":
                log_candidates.insert(0, f)

    for log_path in log_candidates:
        if log_path.exists() and log_path.stat().st_size > 0:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                return "".join(lines[-n_lines:])
            except Exception:
                continue

    return "(no log files found)"


def get_traceback_from_logs(logs: str) -> str:
    """Extract the most recent Python traceback from logs."""
    lines = logs.split("\n")
    tb_start = -1
    tb_end = -1

    # Find the last traceback
    for i in range(len(lines) - 1, -1, -1):
        if "Traceback (most recent call last)" in lines[i]:
            tb_start = i
            break

    if tb_start < 0:
        # No traceback found — return last 50 lines
        return "\n".join(lines[-50:])

    # Find the end of the traceback
    for i in range(tb_start + 1, len(lines)):
        # Traceback ends at the error line (not indented after File/line refs)
        if lines[i] and not lines[i].startswith(" ") and not lines[i].startswith("\t"):
            tb_end = i + 1
            break

    if tb_end < 0:
        tb_end = len(lines)

    return "\n".join(lines[tb_start:tb_end])


def read_source_file(filepath: str) -> str:
    """Read a source file for Claude context. Returns empty string on error."""
    full_path = CALLISTO_DIR / filepath
    if not full_path.exists():
        return ""
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def extract_files_from_traceback(traceback: str) -> list[str]:
    """Extract file paths mentioned in a traceback."""
    files = set()
    for match in re.finditer(r'File "([^"]+)"', traceback):
        path = match.group(1)
        # Convert to relative path within project
        try:
            rel = os.path.relpath(path, CALLISTO_DIR).replace("\\", "/")
            if not rel.startswith("..") and rel not in PROTECTED_FILES:
                files.add(rel)
        except ValueError:
            pass
    return sorted(files)


# ── Process control ──

async def _kill_api_process() -> None:
    """Kill the Callisto API process so watchdog restarts it with new code.

    Tries graceful HTTP shutdown first, then falls back to killing port 8420.
    """
    import httpx

    # 1. Try graceful: POST /admin/restart (may not exist in old code)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{API_URL}/admin/restart", params={"confirm": "YES"})
            if resp.status_code == 200:
                logger.info("Graceful restart triggered via /admin/restart")
                return
            logger.info(
                f"Graceful restart returned {resp.status_code} — falling back to kill: {resp.text[:200]}"
            )
    except Exception as e:
        logger.info(f"Graceful restart endpoint unavailable (falling back to kill): {e}")

    # 2. Fall back to killing the process on port 8420
    logger.info("Graceful restart unavailable — killing port 8420 process")
    try:
        if sys.platform == "win32":
            # Find PID on port 8420 and kill it
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if ":8420" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(
                            ["taskkill", "/F", "/PID", pid],
                            capture_output=True, timeout=10,
                        )
                        logger.info(f"Killed API process PID {pid}")
                        return
        else:
            subprocess.run(
                ["fuser", "-k", "8420/tcp"],
                capture_output=True, timeout=10,
            )
            logger.info("Killed process on port 8420 via fuser")
            return
    except Exception as e:
        logger.error(f"Failed to kill API process: {e}")


# ── Health check ──

async def check_health() -> dict:
    """
    Check if the main process is healthy.
    Returns: {"alive": bool, "healthy": bool, "details": dict}
    """
    import httpx

    # Try HTTP endpoint first
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(HEALTH_ENDPOINT)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "alive": True,
                    "healthy": data.get("healthy", False),
                    "source": "http",
                    "details": data,
                }
    except Exception as e:
        logger.debug(f"Health HTTP check failed (trying file fallback): {e}")

    # Fall back to health file
    if HEALTH_FILE.exists():
        try:
            with open(HEALTH_FILE, "r") as f:
                data = json.load(f)

            # Check if file is recent (less than 5 minutes old)
            ts = data.get("timestamp", "")
            if ts:
                file_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - file_time).total_seconds()
                if age < 300:
                    return {
                        "alive": True,
                        "healthy": data.get("healthy", False),
                        "source": "file",
                        "age_seconds": round(age),
                        "details": data,
                    }
        except Exception as e:
            logger.debug(f"Health file check failed: {e}")

    # Both failed — process is probably dead
    return {"alive": False, "healthy": False, "source": "none", "details": {}}


# ── Claude Code fix generation ──

def generate_fix(error_info: dict) -> dict:
    """
    Call Claude Code CLI to generate a fix for a crash.

    Args:
        error_info: {traceback, affected_files, logs_excerpt, error_hash}

    Returns:
        {"fix": str, "files_to_modify": list, "error": str or None}
    """
    traceback = error_info.get("traceback", "")
    affected_files = error_info.get("affected_files", [])
    logs = error_info.get("logs_excerpt", "")

    # Read source files for context
    file_contents = {}
    for f in affected_files[:5]:  # Max 5 files
        content = read_source_file(f)
        if content:
            file_contents[f] = content

    # Build prompt
    file_context = ""
    for fpath, content in file_contents.items():
        file_context += f"\n\n--- {fpath} ---\n{content}"

    prompt = f"""You are Callisto's self-repair system. The main process is crash-looping.
Diagnose the bug and provide a MINIMAL fix.

TRACEBACK:
{traceback}

RECENT LOGS (last relevant section):
{logs[-3000:]}

AFFECTED SOURCE FILES:
{file_context[-15000:]}

RULES:
1. Return ONLY a JSON object with this structure:
   {{"diagnosis": "one-line explanation",
    "fixes": [{{"file": "relative/path.py", "old": "exact text to replace", "new": "replacement text"}}]}}
2. Make the MINIMUM change to fix the crash. Do not refactor.
3. Do not modify these protected files: {', '.join(PROTECTED_FILES)}
4. If you cannot determine the fix, return: {{"diagnosis": "cannot determine", "fixes": []}}
5. The "old" text must be an EXACT match of the current file content.
6. Return ONLY valid JSON. No markdown, no explanation outside the JSON.
"""

    cmd = [
        CLAUDE_CMD,
        "--print",
        "--dangerously-skip-permissions",
        "-p", prompt,
        "--model", CLAUDE_MODEL,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(CALLISTO_DIR),
        )

        if result.returncode != 0:
            return {"fix": None, "error": f"Claude exit code {result.returncode}: {result.stderr[:500]}"}

        content = result.stdout.strip()

        # Extract JSON from response
        # Try to find JSON object in the response
        json_match = re.search(r'\{[\s\S]*\}', content)
        if not json_match:
            return {"fix": None, "error": "No JSON found in Claude response"}

        fix_data = json.loads(json_match.group())

        # Validate fixes don't touch protected files
        fixes = fix_data.get("fixes", [])
        safe_fixes = [
            f for f in fixes
            if f.get("file") not in PROTECTED_FILES
        ]

        return {
            "diagnosis": fix_data.get("diagnosis", "unknown"),
            "fixes": safe_fixes,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return {"fix": None, "error": f"Claude timed out after {CLAUDE_TIMEOUT}s"}
    except json.JSONDecodeError as e:
        return {"fix": None, "error": f"Invalid JSON from Claude: {e}"}
    except FileNotFoundError:
        return {"fix": None, "error": f"Claude CLI not found: {CLAUDE_CMD}"}
    except Exception as e:
        return {"fix": None, "error": str(e)}


# ── Safe patching ──

def apply_fix_safely(fix_data: dict) -> dict:
    """
    Apply a fix with safety guardrails:
    1. Git stash current state
    2. Apply the fix
    3. Run tests
    4. If tests fail: revert
    5. Return result

    Returns: {"success": bool, "error": str or None, "test_output": str}
    """
    fixes = fix_data.get("fixes", [])
    if not fixes:
        return {"success": False, "error": "No fixes to apply"}

    diagnosis = fix_data.get("diagnosis", "unknown")
    logger.info(f"Applying fix: {diagnosis} ({len(fixes)} file(s))")

    # Step 1: Git stash
    try:
        stash_result = subprocess.run(
            ["git", "stash", "push", "-m", f"sentinel-pre-fix-{int(time.time())}"],
            capture_output=True, text=True, cwd=str(CALLISTO_DIR),
        )
        stashed = "No local changes" not in stash_result.stdout
        logger.info(f"Git stash: {'created' if stashed else 'nothing to stash'}")
    except Exception as e:
        logger.warning(f"Git stash failed: {e}")
        stashed = False

    # Step 2: Apply fixes
    applied_files = []
    try:
        for fix in fixes:
            filepath = CALLISTO_DIR / fix["file"]
            if not filepath.exists():
                raise FileNotFoundError(f"File not found: {fix['file']}")

            old_text = fix["old"]
            new_text = fix["new"]

            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if old_text not in content:
                raise ValueError(
                    f"Old text not found in {fix['file']}. "
                    f"Expected: {old_text[:100]}..."
                )

            # Only replace first occurrence
            content = content.replace(old_text, new_text, 1)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            applied_files.append(fix["file"])
            logger.info(f"Patched: {fix['file']}")

    except Exception as e:
        logger.error(f"Fix application failed: {e}")
        # Revert
        if stashed:
            subprocess.run(
                ["git", "stash", "pop"],
                capture_output=True, cwd=str(CALLISTO_DIR),
            )
        return {"success": False, "error": str(e), "test_output": ""}

    # Step 3: Run tests
    try:
        test_result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-x", "--tb=short", "-q"],
            capture_output=True, text=True, timeout=120,
            cwd=str(CALLISTO_DIR),
        )
        test_output = test_result.stdout + test_result.stderr
        tests_passed = test_result.returncode == 0

        logger.info(f"Tests: {'PASSED' if tests_passed else 'FAILED'}")
        if not tests_passed:
            logger.error(f"Test output: {test_output[-500:]}")

    except subprocess.TimeoutExpired:
        tests_passed = False
        test_output = "Tests timed out after 120s"
        logger.error("Tests timed out")
    except Exception as e:
        tests_passed = False
        test_output = str(e)
        logger.error(f"Test run failed: {e}")

    # Step 4: Revert if tests failed
    if not tests_passed:
        logger.warning("Tests failed — reverting fix")
        try:
            # Restore patched files from git
            for f in applied_files:
                subprocess.run(
                    ["git", "checkout", "--", f],
                    capture_output=True, cwd=str(CALLISTO_DIR),
                )
            # Restore stash
            if stashed:
                subprocess.run(
                    ["git", "stash", "pop"],
                    capture_output=True, cwd=str(CALLISTO_DIR),
                )
        except Exception as e:
            logger.error(f"Revert failed: {e}")

        return {
            "success": False,
            "error": "Tests failed after applying fix",
            "test_output": test_output[-1000:],
        }

    # Step 5: Success — commit the fix
    try:
        subprocess.run(
            ["git", "add"] + applied_files,
            capture_output=True, cwd=str(CALLISTO_DIR),
        )
        commit_msg = (
            f"sentinel: auto-fix — {diagnosis}\n\n"
            f"Files modified: {', '.join(applied_files)}\n"
            f"Applied by Callisto Sentinel (Layer 3 self-repair)\n\n"
            f"Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, cwd=str(CALLISTO_DIR),
        )
        logger.info("Fix committed to git")
    except Exception as e:
        logger.warning(f"Git commit failed (fix is applied but uncommitted): {e}")

    # Restore any stashed changes on top
    if stashed:
        try:
            subprocess.run(
                ["git", "stash", "pop"],
                capture_output=True, cwd=str(CALLISTO_DIR),
            )
        except Exception as e:
            logger.warning(f"Git stash pop failed after fix: {e}")

    state.total_fixes += 1
    state.fixes_applied.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diagnosis": diagnosis,
        "files": applied_files,
    })

    return {
        "success": True,
        "error": None,
        "test_output": test_output[-500:],
        "files_modified": applied_files,
    }


# ── Crash loop handler ──

async def handle_crash_loop() -> None:
    """
    Called when a crash loop is detected.
    Diagnoses the issue and attempts to fix it.
    """
    logger.warning("CRASH LOOP DETECTED — beginning diagnosis")
    send_telegram("Crash loop detected. Diagnosing...")

    # Gather diagnostics
    logs = get_recent_logs(200)
    traceback = get_traceback_from_logs(logs)
    affected_files = extract_files_from_traceback(traceback)

    eh = error_hash(traceback)
    attempts = state.fix_attempts.get(eh, 0)

    if attempts >= MAX_FIX_ATTEMPTS:
        msg = (
            f"Crash loop — max fix attempts ({MAX_FIX_ATTEMPTS}) exhausted "
            f"for error {eh}. Will keep restarting API (watchdog handles restart).\n\n"
            f"Error:\n{traceback[:500]}"
        )
        logger.error(msg)
        send_telegram(msg)
        # Wait 2 min before re-checking (watchdog.py handles the actual restart)
        # Do NOT sleep 30 min — that leaves the system dead
        await asyncio.sleep(120)
        return

    state.fix_attempts[eh] = attempts + 1
    logger.info(
        f"Fix attempt {attempts + 1}/{MAX_FIX_ATTEMPTS} for error {eh}"
    )

    # Call Claude Code for diagnosis and fix
    error_info = {
        "traceback": traceback,
        "affected_files": affected_files,
        "logs_excerpt": logs[-3000:],
        "error_hash": eh,
    }

    fix_result = generate_fix(error_info)

    if fix_result.get("error"):
        msg = f"Claude Code fix generation failed: {fix_result['error']}"
        logger.error(msg)
        send_telegram(msg)
        state.total_failed_fixes += 1
        return

    if not fix_result.get("fixes"):
        msg = (
            f"Claude couldn't determine a fix.\n"
            f"Diagnosis: {fix_result.get('diagnosis', 'unknown')}\n"
            f"Error: {traceback[:300]}"
        )
        logger.warning(msg)
        send_telegram(msg)
        state.total_failed_fixes += 1
        return

    # Apply the fix safely
    apply_result = apply_fix_safely(fix_result)

    if apply_result["success"]:
        msg = (
            f"FIX APPLIED SUCCESSFULLY\n"
            f"Diagnosis: {fix_result.get('diagnosis', '?')}\n"
            f"Files: {', '.join(apply_result.get('files_modified', []))}\n"
            f"Tests: PASSED\n"
            f"Restarting main process..."
        )
        logger.info(msg)
        send_telegram(msg)
        # Reset crash times since we fixed the issue
        state.crash_times.clear()
    else:
        msg = (
            f"Fix attempt FAILED (reverted)\n"
            f"Diagnosis: {fix_result.get('diagnosis', '?')}\n"
            f"Error: {apply_result.get('error', '?')}\n"
            f"Attempt {attempts + 1}/{MAX_FIX_ATTEMPTS}"
        )
        logger.warning(msg)
        send_telegram(msg)
        state.total_failed_fixes += 1


# ── Main loop ──

async def sentinel_loop() -> None:
    """Main sentinel monitoring loop."""
    logger.info(f"Sentinel started — monitoring {API_URL}")
    logger.info(f"Crash threshold: {CRASH_THRESHOLD} in {CRASH_WINDOW}s")
    logger.info(f"Protected files: {PROTECTED_FILES}")
    send_telegram("Sentinel watchdog started")

    while True:
        try:
            # Check for restart signal file (written by Claude Code after code changes)
            if RESTART_SIGNAL.exists():
                try:
                    reason = RESTART_SIGNAL.read_text(encoding="utf-8").strip() or "code reload"
                    RESTART_SIGNAL.unlink()
                    logger.info(f"RESTART SIGNAL detected: {reason}")
                    send_telegram(f"Restart signal: {reason} — killing API for watchdog restart")
                    await _kill_api_process()
                    await asyncio.sleep(5)  # Let watchdog handle restart
                    continue
                except Exception as e:
                    logger.error(f"Failed to process restart signal: {e}")

            health = await check_health()
            state.total_checks += 1

            if health["alive"] and health["healthy"]:
                state.healthy_streak += 1
                state.last_healthy = time.monotonic()

                # Reset crash counter after sustained healthy period
                if state.healthy_streak >= HEALTHY_STREAK_RESET:
                    if state.crash_times:
                        logger.info("Sustained healthy streak — clearing crash history")
                        state.crash_times.clear()

            elif health["alive"] and not health["healthy"]:
                state.healthy_streak = 0
                # Alive but unhealthy — Layer 2 should handle this
                if state.total_checks % 10 == 0:
                    logger.info(
                        f"Main process alive but unhealthy "
                        f"(source: {health.get('source', '?')})"
                    )

            else:
                # Dead
                state.healthy_streak = 0
                now = time.monotonic()
                state.crash_times.append(now)

                # Prune old crash times
                cutoff = now - CRASH_WINDOW
                state.crash_times = [t for t in state.crash_times if t > cutoff]

                recent_crashes = len(state.crash_times)
                logger.warning(
                    f"Main process NOT alive — "
                    f"{recent_crashes}/{CRASH_THRESHOLD} crashes in window"
                )

                # AUTO-RESTART: If process is dead, restart it directly.
                # Log output to files (not DEVNULL) so crashes are diagnosable.
                if recent_crashes < CRASH_THRESHOLD:
                    logger.info("Restarting Callisto API directly...")
                    try:
                        from datetime import datetime as _dt
                        _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                        _stdout_log = LOG_DIR / f"api_stdout_{_ts}.log"
                        _stderr_log = LOG_DIR / f"api_stderr_{_ts}.log"
                        _stdout_f = open(_stdout_log, "w", encoding="utf-8")
                        _stderr_f = open(_stderr_log, "w", encoding="utf-8")
                        subprocess.Popen(
                            [sys.executable, str(CALLISTO_DIR / "api.py")],
                            cwd=str(CALLISTO_DIR),
                            stdout=_stdout_f,
                            stderr=_stderr_f,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        )
                        send_telegram(f"Auto-restarted Callisto API (crash #{recent_crashes})")
                        logger.info(f"Restart command issued — stderr -> {_stderr_log}")
                        logger.info("Waiting 30s for startup")
                        await asyncio.sleep(30)  # Give it time to start
                    except Exception as e:
                        logger.error(f"Failed to restart API: {e}")

                # Check for crash loop (5+ crashes in 10 min)
                if recent_crashes >= CRASH_THRESHOLD:
                    await handle_crash_loop()

        except Exception as e:
            logger.error(f"Sentinel check error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def main():
    """Entry point."""
    print(f"Callisto Sentinel — Layer 3 Watchdog")
    print(f"Monitoring: {API_URL}")
    print(f"Logs: {SENTINEL_LOG}")
    print()

    try:
        asyncio.run(sentinel_loop())
    except KeyboardInterrupt:
        logger.info("Sentinel stopped by user")
        send_telegram("Sentinel watchdog stopped")


if __name__ == "__main__":
    main()
