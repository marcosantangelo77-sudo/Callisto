"""
Claude Code tool for Callisto — SOTA reasoning escalation via Claude Max subscription.

Tier 2 reasoning: when local models (Tier 1) can't achieve sufficient confidence,
escalate to Claude Code (Opus 4.6) for frontier-quality analysis.

Evidence from Claude Code is tagged as PRIMARY source class — direct SOTA analysis.
Zero incremental cost beyond the Claude Max membership.

Resilience features:
  - Rate limit detection + exponential backoff (Claude Max has hourly caps)
  - Availability state machine: AVAILABLE → RATE_LIMITED → COOLDOWN → AVAILABLE
  - Local models continue all non-Claude work during cooldown
  - Automatic retry scheduling — no human intervention needed
  - Persistent state survives process restarts via SQLite
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.claude_code")

# Configuration
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
MAX_BUDGET_USD = float(os.getenv("CLAUDE_MAX_BUDGET", "0"))  # 0 = no limit (Max sub)
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # seconds
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Rate limit / backoff configuration
INITIAL_BACKOFF = 300       # 5 min after first rate limit — 2 min was too short, causing rapid re-stalls
MAX_BACKOFF = 3600          # 1 hour max backoff
BACKOFF_MULTIPLIER = 1.5    # Gentler ramp — we want to stay aggressive
MAX_CALLS_PER_HOUR = 35     # Buffer below actual Claude Max ceiling — hitting 45 triggers hard stalls
RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "quota",
    "capacity",
    "overloaded",
    "try again later",
]

# ── State tracking ──

_call_count = 0
_last_reset = time.monotonic()
_TRACKING_WINDOW = 3600  # 1 hour

# Lock protecting _call_count / _last_reset against parallel ladder
# escalations. Without this lock, N concurrent threads all read
# _call_count == MAX-1 and each increment to MAX, causing the soft
# hourly cap to be breached. The lock is tiny-scope (just the counter
# bump + window roll) so it never blocks real work.
_call_count_lock = threading.Lock()

# Availability state — persisted to disk so restarts don't bypass cooldown
_COOLDOWN_FILE = os.path.join(os.path.dirname(DB_PATH), "claude_cooldown.json")
_available = True
_cooldown_until = 0.0           # monotonic timestamp
_consecutive_failures = 0
_current_backoff = INITIAL_BACKOFF
_last_error = ""
_total_rate_limits = 0
_total_successful = 0


def _persist_cooldown() -> None:
    """Save cooldown state to disk so it survives process restarts."""
    import json
    try:
        remaining = max(0, _cooldown_until - time.monotonic())
        state = {
            "cooldown_remaining_seconds": remaining,
            "cooldown_set_at": time.time(),
            "consecutive_failures": _consecutive_failures,
            "current_backoff": _current_backoff,
            "last_error": _last_error,
        }
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass  # Non-critical


def _restore_cooldown() -> None:
    """Restore cooldown state from disk on module load."""
    global _available, _cooldown_until, _consecutive_failures, _current_backoff, _last_error
    import json
    try:
        with open(_COOLDOWN_FILE) as f:
            state = json.load(f)
        remaining = state.get("cooldown_remaining_seconds", 0)
        set_at = state.get("cooldown_set_at", 0)
        # Calculate how much cooldown is left based on wall-clock elapsed time
        elapsed = time.time() - set_at
        left = remaining - elapsed
        if left > 5:  # More than 5 seconds of cooldown remaining
            _available = False
            _cooldown_until = time.monotonic() + left
            _consecutive_failures = state.get("consecutive_failures", 0)
            _current_backoff = state.get("current_backoff", INITIAL_BACKOFF)
            _last_error = state.get("last_error", "")
            logger.info(
                f"Claude Code cooldown restored from disk: {left:.0f}s remaining "
                f"(failures={_consecutive_failures})"
            )
        else:
            # Cooldown expired while we were down — clean up file
            try:
                os.remove(_COOLDOWN_FILE)
            except FileNotFoundError:
                pass
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass  # No saved state or corrupt file — start fresh


_restore_cooldown()


def _track_call() -> int:
    """Track call count within the current window. Returns current count.

    Locked so parallel ladder escalations can't race the soft cap.
    """
    global _call_count, _last_reset
    with _call_count_lock:
        now = time.monotonic()
        if now - _last_reset > _TRACKING_WINDOW:
            _call_count = 0
            _last_reset = now
        _call_count += 1
        return _call_count


def _try_reserve_call_slot() -> Optional[int]:
    """Atomically reserve a slot in the hourly window.

    Returns the new call count on success, or None if the hourly cap
    has already been reached. This is the race-free replacement for
    the old 'check is_available(), then call _track_call()' pattern —
    50 threads that all see count==34 can no longer each increment to
    35+ simultaneously; exactly one wins per slot.
    """
    global _call_count, _last_reset
    with _call_count_lock:
        now = time.monotonic()
        if now - _last_reset > _TRACKING_WINDOW:
            _call_count = 0
            _last_reset = now
        if _call_count >= MAX_CALLS_PER_HOUR:
            return None
        _call_count += 1
        return _call_count


def _is_rate_limited(error_msg: str) -> bool:
    """Check if an error message indicates a rate limit."""
    lower = error_msg.lower()
    return any(pattern in lower for pattern in RATE_LIMIT_PATTERNS)


def _enter_cooldown(error_msg: str) -> None:
    """Enter cooldown state after a rate limit hit."""
    global _available, _cooldown_until, _consecutive_failures
    global _current_backoff, _last_error, _total_rate_limits

    _available = False
    _consecutive_failures += 1
    _total_rate_limits += 1
    _last_error = error_msg

    try:
        from tools.metrics import record_claude_call
        record_claude_call("rate_limited")
    except Exception:
        pass

    # Exponential backoff with cap
    _current_backoff = min(
        INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (_consecutive_failures - 1)),
        MAX_BACKOFF,
    )
    _cooldown_until = time.monotonic() + _current_backoff
    _persist_cooldown()

    cooldown_min = _current_backoff / 60
    logger.warning(
        f"Claude Code rate limited — cooling down for {cooldown_min:.0f} min "
        f"(attempt #{_consecutive_failures}, backoff={_current_backoff}s)"
    )


def _mark_success() -> None:
    """Reset failure state after a successful call."""
    global _available, _consecutive_failures, _current_backoff, _total_successful
    _available = True
    _consecutive_failures = 0
    _current_backoff = INITIAL_BACKOFF
    _total_successful += 1
    try:
        from tools.metrics import record_claude_call
        record_claude_call("ok")
    except Exception:
        pass
    # Clean up persistent cooldown file
    try:
        os.remove(_COOLDOWN_FILE)
    except FileNotFoundError:
        pass


def is_available() -> bool:
    """
    Check if Claude Code is currently available.

    The ResearchLoop and AutonomousLoop should check this BEFORE
    attempting escalation. If unavailable, skip Claude-dependent
    work and continue with local-only phases.
    """
    # Hard kill switch: CALLISTO_LOCAL_ONLY=1 means NO Claude calls anywhere
    from tools.local_only import is_local_only
    if is_local_only():
        return False

    global _available, _cooldown_until, _call_count, _last_reset

    # Reset tracking window if expired — this MUST happen before the
    # call count check, otherwise the counter stays stuck at max and
    # no new calls are ever attempted (which also prevents _track_call
    # from running and resetting the window).
    with _call_count_lock:
        now = time.monotonic()
        if now - _last_reset > _TRACKING_WINDOW:
            _call_count = 0
            _last_reset = now
        at_cap = _call_count >= MAX_CALLS_PER_HOUR

    if _available:
        # Soft cap: don't exceed hourly call limit
        if at_cap:
            return False
        return True

    # Check if cooldown has elapsed
    if time.monotonic() >= _cooldown_until:
        _available = True
        logger.info(
            f"Claude Code cooldown elapsed — resuming "
            f"(was down {_current_backoff}s, {_consecutive_failures} consecutive failures)"
        )
        return True

    return False


def reset_rate_limit() -> dict:
    """Force-reset all rate limit state. Returns previous and new state."""
    global _available, _call_count, _last_reset, _cooldown_until
    global _consecutive_failures, _current_backoff, _last_error

    prev = get_usage_stats()

    _available = True
    _call_count = 0
    _last_reset = time.monotonic()
    _cooldown_until = 0.0
    _consecutive_failures = 0
    _current_backoff = INITIAL_BACKOFF
    _last_error = ""

    logger.info("Claude Code rate limit force-reset via admin endpoint")
    return {"previous": prev, "current": get_usage_stats()}


def get_cooldown_remaining() -> float:
    """Seconds until Claude Code is available again. 0 if available now."""
    if _available:
        return 0.0
    remaining = _cooldown_until - time.monotonic()
    return max(0.0, remaining)


def get_usage_stats() -> dict:
    """Return current usage tracking stats."""
    elapsed = time.monotonic() - _last_reset
    return {
        "available": is_available(),
        "calls_this_window": _call_count,
        "max_calls_per_hour": MAX_CALLS_PER_HOUR,
        "window_seconds": _TRACKING_WINDOW,
        "elapsed_seconds": round(elapsed, 1),
        "consecutive_failures": _consecutive_failures,
        "cooldown_remaining_seconds": round(get_cooldown_remaining(), 1),
        "current_backoff_seconds": _current_backoff,
        "last_error": _last_error,
        "total_rate_limits": _total_rate_limits,
        "total_successful": _total_successful,
    }


async def claude_code_query(
    prompt: str,
    system_context: str = "",
    timeout: Optional[int] = None,
    skip_availability_check: bool = False,
    hermes_caller: str = "default",
) -> dict:
    """
    Send a query to Claude Code CLI and return the response.

    Uses `claude --print` for non-interactive single-shot output.
    Handles rate limits with exponential backoff.
    Hermes bridge auto-injects persistent memory context.

    Args:
        prompt: The query/analysis request for Claude.
        system_context: Optional context to prepend.
        hermes_caller: Caller type for Hermes priority ('hypothesis_gen', 'deep_work', 'edge_analysis', 'telegram').
        timeout: Override default timeout in seconds.
        skip_availability_check: If True, attempt even during cooldown.

    Returns:
        Dict with "content", "source_class", "model", "call_number",
        "error", and "rate_limited" (if applicable).
    """
    # HARD KILL SWITCH — enforced unconditionally, before any branch.
    # CALLISTO_LOCAL_ONLY=1 forbids every Claude subprocess spawn,
    # regardless of skip_availability_check, hermes_caller, or any
    # other flag a caller might set. This is the single choke point
    # that guarantees no cloud calls happen in local-only mode.
    from tools.local_only import is_local_only, local_only_result
    if is_local_only():
        logger.info("Claude Code blocked by CALLISTO_LOCAL_ONLY kill switch")
        try:
            from tools.metrics import record_claude_call
            record_claude_call("blocked")
        except Exception:
            pass
        return local_only_result(
            reason="claude_code_query blocked",
            extra={"model": CLAUDE_MODEL},
        )

    # Pre-flight availability check
    if not skip_availability_check and not is_available():
        remaining = get_cooldown_remaining()
        logger.info(
            f"Claude Code unavailable — {remaining:.0f}s remaining in cooldown. "
            f"Skipping escalation."
        )
        try:
            from tools.metrics import record_claude_call
            record_claude_call("skipped_cooldown")
        except Exception:
            pass
        return {
            "content": "",
            "source_class": "PRIMARY",
            "model": CLAUDE_MODEL,
            "call_number": 0,
            "error": f"Rate limited — retry in {remaining:.0f}s",
            "rate_limited": True,
            "cooldown_remaining": round(remaining, 1),
        }

    timeout = timeout or CLAUDE_TIMEOUT

    # Hermes bridge — inject persistent memory into every Claude call
    # Context is prioritized based on what's calling (hypothesis gen vs deep work vs edge analysis)
    hermes_context = ""
    try:
        from tools.hermes_memory import get_hermes_memory
        hermes = get_hermes_memory()
        hermes_context = await hermes.get_memory_context(caller=hermes_caller)
        if hermes_context:
            logger.info(f"Hermes bridge [{hermes_caller}]: injecting {len(hermes_context)} chars")
    except Exception as e:
        logger.debug(f"Hermes bridge unavailable: {e}")

    # Build the full prompt with Hermes memory + caller context + prompt
    full_prompt = ""
    if hermes_context:
        full_prompt += f"{hermes_context}\n\n"
    if system_context:
        full_prompt += f"{system_context}\n\n"
    full_prompt += prompt

    # Build CLI command
    cmd = [
        CLAUDE_CMD,
        "--print",
        "--dangerously-skip-permissions",
        "-p", full_prompt,
        "--model", CLAUDE_MODEL,
    ]
    if MAX_BUDGET_USD > 0:
        cmd.extend(["--max-budget-usd", str(MAX_BUDGET_USD)])

    call_num = _track_call()
    logger.info(
        f"Claude Code escalation #{call_num}: "
        f"prompt={len(full_prompt)} chars, model={CLAUDE_MODEL}"
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )

        if process.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            stdout_msg = stdout.decode("utf-8", errors="replace").strip()
            combined_error = f"{error_msg} {stdout_msg}"

            logger.error(
                f"Claude Code returned exit code {process.returncode}: {error_msg}"
            )

            # Check for rate limit
            if _is_rate_limited(combined_error):
                _enter_cooldown(combined_error)
                return {
                    "content": "",
                    "source_class": "PRIMARY",
                    "model": CLAUDE_MODEL,
                    "call_number": call_num,
                    "error": f"Rate limited: {error_msg}",
                    "rate_limited": True,
                    "cooldown_remaining": round(get_cooldown_remaining(), 1),
                }

            try:
                from tools.metrics import record_claude_call
                record_claude_call("error")
            except Exception:
                pass
            return {
                "content": "",
                "source_class": "PRIMARY",
                "model": CLAUDE_MODEL,
                "call_number": call_num,
                "error": f"Exit code {process.returncode}: {error_msg}",
                "rate_limited": False,
            }

        content = stdout.decode("utf-8", errors="replace").strip()
        logger.info(f"Claude Code #{call_num} responded: {len(content)} chars")

        # Success — reset failure state
        _mark_success()

        return {
            "content": content,
            "source_class": "PRIMARY",
            "model": CLAUDE_MODEL,
            "call_number": call_num,
            "error": None,
            "rate_limited": False,
        }

    except asyncio.TimeoutError:
        logger.error(f"Claude Code #{call_num} timed out after {timeout}s")
        try:
            from tools.metrics import record_claude_call
            record_claude_call("timeout")
        except Exception:
            pass
        # Timeouts could be rate-limit related (queue congestion)
        return {
            "content": "",
            "source_class": "PRIMARY",
            "model": CLAUDE_MODEL,
            "call_number": call_num,
            "error": f"Timeout after {timeout}s",
            "rate_limited": False,
        }
    except FileNotFoundError:
        logger.error(f"Claude Code CLI not found: {CLAUDE_CMD}")
        try:
            from tools.metrics import record_claude_call
            record_claude_call("cli_missing")
        except Exception:
            pass
        return {
            "content": "",
            "source_class": "PRIMARY",
            "model": CLAUDE_MODEL,
            "call_number": call_num,
            "error": f"CLI not found: {CLAUDE_CMD}",
            "rate_limited": False,
        }
    except Exception as e:
        error_str = str(e)
        logger.error(f"Claude Code #{call_num} failed: {error_str}")

        # Check for rate limit in exception message
        if _is_rate_limited(error_str):
            _enter_cooldown(error_str)
            return {
                "content": "",
                "source_class": "PRIMARY",
                "model": CLAUDE_MODEL,
                "call_number": call_num,
                "error": f"Rate limited: {error_str}",
                "rate_limited": True,
                "cooldown_remaining": round(get_cooldown_remaining(), 1),
            }

        try:
            from tools.metrics import record_claude_call
            record_claude_call("error")
        except Exception:
            pass
        return {
            "content": "",
            "source_class": "PRIMARY",
            "model": CLAUDE_MODEL,
            "call_number": call_num,
            "error": error_str,
            "rate_limited": False,
        }


async def claude_code_available() -> bool:
    """Check if Claude Code CLI is accessible AND not rate limited."""
    if not is_available():
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            CLAUDE_CMD, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        return process.returncode == 0
    except Exception:
        return False


def claude_code_sync(prompt: str, system_context: str = "") -> dict:
    """Synchronous wrapper for Hermes function registry compatibility.

    Respects the CALLISTO_LOCAL_ONLY kill switch before spawning any
    thread / subprocess — required because this is registered in
    inference.FUNCTION_REGISTRY under 'claude_code' and the
    orchestrator routes tool calls straight through it.
    """
    # HARD KILL SWITCH — same guarantee as claude_code_query above.
    from tools.local_only import is_local_only, local_only_result
    if is_local_only():
        logger.info("claude_code_sync blocked by CALLISTO_LOCAL_ONLY kill switch")
        return local_only_result(
            reason="claude_code_sync blocked",
            extra={"model": CLAUDE_MODEL},
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                claude_code_query(prompt, system_context),
            )
            return future.result(timeout=CLAUDE_TIMEOUT + 10)
    else:
        return asyncio.run(claude_code_query(prompt, system_context))
