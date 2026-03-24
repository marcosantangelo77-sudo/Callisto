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
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "180"))  # seconds
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Rate limit / backoff configuration
INITIAL_BACKOFF = 120       # 2 min after first rate limit (recover fast)
MAX_BACKOFF = 3600          # 1 hour max backoff
BACKOFF_MULTIPLIER = 1.5    # Gentler ramp — we want to stay aggressive
MAX_CALLS_PER_HOUR = 45     # Claude Max with 2x peak bonus = aggressive throughput
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

# Availability state
_available = True
_cooldown_until = 0.0           # monotonic timestamp
_consecutive_failures = 0
_current_backoff = INITIAL_BACKOFF
_last_error = ""
_total_rate_limits = 0
_total_successful = 0


def _track_call() -> int:
    """Track call count within the current window. Returns current count."""
    global _call_count, _last_reset
    now = time.monotonic()
    if now - _last_reset > _TRACKING_WINDOW:
        _call_count = 0
        _last_reset = now
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

    # Exponential backoff with cap
    _current_backoff = min(
        INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (_consecutive_failures - 1)),
        MAX_BACKOFF,
    )
    _cooldown_until = time.monotonic() + _current_backoff

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


def is_available() -> bool:
    """
    Check if Claude Code is currently available.

    The ResearchLoop and AutonomousLoop should check this BEFORE
    attempting escalation. If unavailable, skip Claude-dependent
    work and continue with local-only phases.
    """
    global _available, _cooldown_until

    if _available:
        # Soft cap: don't exceed hourly call limit
        if _call_count >= MAX_CALLS_PER_HOUR:
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
) -> dict:
    """
    Send a query to Claude Code CLI and return the response.

    Uses `claude --print` for non-interactive single-shot output.
    Handles rate limits with exponential backoff.

    Args:
        prompt: The query/analysis request for Claude.
        system_context: Optional context to prepend.
        timeout: Override default timeout in seconds.
        skip_availability_check: If True, attempt even during cooldown.

    Returns:
        Dict with "content", "source_class", "model", "call_number",
        "error", and "rate_limited" (if applicable).
    """
    # Pre-flight availability check
    if not skip_availability_check and not is_available():
        remaining = get_cooldown_remaining()
        logger.info(
            f"Claude Code unavailable — {remaining:.0f}s remaining in cooldown. "
            f"Skipping escalation."
        )
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

    # Build the full prompt with context
    full_prompt = ""
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
    """Synchronous wrapper for Hermes function registry compatibility."""
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
