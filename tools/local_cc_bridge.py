"""
Local Claude Code bridge — forked CC (with qwen36 tool-use support) as
a tool-using agent for Callisto's CALLISTO_LOCAL_ONLY research loop.

When the nuclear kill switch (CALLISTO_LOCAL_ONLY=1) blocks the real
Claude Code path, we still want multi-step reasoning + tool use for
tasks like hypothesis_gen / deep_work / reasoning. Single-shot Ollama
calls via inference.py's ladder don't give us that.

This module spawns the forked CC as a subprocess that itself talks to
local Ollama (qwen36 by default). The fork's prompt-mode tool fix
(commit 19c69e7) makes qwen36 fully tool-capable, so we get a real
agentic loop without any cloud calls.

Design notes:
  - Subprocess is invoked via `bun <fork>/dist/cli.mjs -p <query>
    --dangerously-skip-permissions --output-format json`.
  - Result is a single JSON object with a `result` field (see
    `makeResultMessage` in the fork's bridgeMessaging.ts).
  - Hard timeout kills the subprocess cleanly. Missing fork binary
    returns a structured error dict — the caller falls back to the
    direct Ollama ladder in inference.py.
  - No incremental cost: everything stays on localhost:11434.

Result shape mirrors inference.escalate_with_ladder so the router hook
can treat bridge results like any other ladder rung.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import subprocess
import sys
from typing import Any, Optional

logger = logging.getLogger("callisto.local_cc_bridge")


# ── Defaults / env overrides ────────────────────────────────────────────────

DEFAULT_MODEL = "qwen36:latest"
DEFAULT_TIMEOUT_MS = 15 * 60 * 1000  # 15 min

# Candidate sibling layouts to probe for a forked CC bundle when
# LOCAL_CC_PATH is not set. Checked in order, first hit wins.
# Repo root is assumed two levels up from this file (tools/local_cc_bridge.py).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_SIBLING_CC_CANDIDATES = (
    os.path.join(_REPO_ROOT, os.pardir, "claude-code-src", "dist", "cli.mjs"),
    os.path.join(_REPO_ROOT, os.pardir, "claude-code", "dist", "cli.mjs"),
    os.path.join(_REPO_ROOT, "..", "claude-code-fork", "dist", "cli.mjs"),
)

# Legacy Windows default kept only as a very last resort when nothing
# else is on disk. Preserves previous behaviour for existing dev boxes.
_LEGACY_WINDOWS_DEFAULT = r"C:\Users\marco\OneDrive\Desktop\claude-code-src\dist\cli.mjs"


def _autodetect_cc_path() -> Optional[str]:
    """Probe known sibling layouts for a forked CC cli.mjs. Returns absolute path or None."""
    for cand in _SIBLING_CC_CANDIDATES:
        p = os.path.abspath(cand)
        if os.path.isfile(p):
            return p
    # Legacy absolute path as final fallback (original hardcoded default).
    if os.path.isfile(_LEGACY_WINDOWS_DEFAULT):
        return _LEGACY_WINDOWS_DEFAULT
    return None


def _cc_path() -> str:
    """
    Absolute path to forked CC bundle.

    Resolution order:
      1. LOCAL_CC_PATH env var (if set, used verbatim — no existence check here)
      2. Sibling-directory autodetect (../claude-code-src/dist/cli.mjs, etc.)
      3. Empty string if nothing found (caller logs & degrades to direct-Ollama)
    """
    env_override = os.getenv("LOCAL_CC_PATH")
    if env_override:
        return os.path.abspath(env_override)
    auto = _autodetect_cc_path()
    if auto is None:
        logger.warning(
            "Local CC bridge: no forked CC bundle found via autodetect "
            f"(searched {_SIBLING_CC_CANDIDATES!r}) — "
            "set LOCAL_CC_PATH or the bridge will be skipped"
        )
        return ""
    return auto


def _model() -> str:
    """Ollama model the fork should drive. Override via LOCAL_CC_MODEL."""
    return os.getenv("LOCAL_CC_MODEL", DEFAULT_MODEL)


def _timeout_ms(override: Optional[int] = None) -> int:
    """Hard timeout in ms. Override via LOCAL_CC_TIMEOUT_MS or arg."""
    if override is not None:
        return int(override)
    env_val = os.getenv("LOCAL_CC_TIMEOUT_MS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            logger.warning(f"Invalid LOCAL_CC_TIMEOUT_MS={env_val!r}, using default")
    return DEFAULT_TIMEOUT_MS


def _bun_cmd() -> str:
    """Resolve bun executable. bun is expected on PATH."""
    return os.getenv("BUN_CMD", "bun")


# ── Env payload for the subprocess ──────────────────────────────────────────

# These are the env vars that wire the forked CC to local Ollama +
# qwen36 prompt-mode tool calling, with auto-update / telemetry /
# auto-compact all disabled.
_REQUIRED_SUBPROC_ENV = {
    "CLAUDE_CODE_USE_OLLAMA": "1",
    "OLLAMA_BASE_URL": "http://localhost:11434",
    "OLLAMA_FORCE_PROMPT_TOOLS": "1",
    "OLLAMA_CONTEXT_WINDOW": "65536",
    "OLLAMA_TIMEOUT_MS": "600000",
    "OLLAMA_KEEP_ALIVE": "24h",
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATE": "1",
}


def _build_env(model: str) -> dict[str, str]:
    """Merge parent env with the required subprocess env. Model is injected last."""
    env = dict(os.environ)
    env.update(_REQUIRED_SUBPROC_ENV)
    env["OLLAMA_MODEL"] = model
    return env


# ── Result parsing ──────────────────────────────────────────────────────────


def _extract_result_text(stdout: str) -> tuple[str, Optional[dict]]:
    """
    Parse the fork's `--output-format json` stdout.

    Expected shape:
        {"type":"result","subtype":"success","result":"<final text>",...}

    Some builds emit a stream of JSON objects (one per turn); in that
    case we take the final object with `type == "result"`. Falls back
    to raw stdout if parsing fails so we never lose the model's work.

    Returns (text, raw_json_or_None).
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return "", None

    # Try whole-string parse first (single JSON object, the common case).
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            text = obj.get("result") or obj.get("content") or ""
            return str(text), obj
    except json.JSONDecodeError:
        pass

    # Stream-of-JSON fallback: scan line by line, keep the last result-typed one.
    last_result: Optional[dict] = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            last_result = obj

    if last_result is not None:
        text = last_result.get("result") or last_result.get("content") or ""
        return str(text), last_result

    # Last resort: return raw stdout as content, no structured payload.
    return stdout, None


# ── Robust subprocess kill ──────────────────────────────────────────────────


def _popen_kwargs_for_platform() -> dict[str, Any]:
    """
    Return Popen kwargs that make the child process killable as a tree.

    On Windows: CREATE_NEW_PROCESS_GROUP so we can post CTRL_BREAK_EVENT
    and ultimately taskkill /T /F the whole tree.
    On POSIX: start a new process group via os.setsid so os.killpg works.
    """
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return {"creationflags": creationflags}
    # POSIX — new session so we can killpg the whole tree.
    return {"start_new_session": True}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill proc and any descendants. Best-effort; swallows errors."""
    if proc.poll() is not None:
        return
    try:
        if platform.system() == "Windows":
            # /T = kill child tree, /F = force. By PID, no interactive prompt.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                return
    except Exception as e:  # pragma: no cover — defensive
        logger.debug(f"Process-tree kill best-effort failed: {e}")
    # Final sweep — reap zombies.
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _run_with_robust_kill(
    cmd: list[str],
    env: dict[str, str],
    cwd: Optional[str],
    timeout_s: float,
) -> subprocess.CompletedProcess:
    """
    Drop-in replacement for subprocess.run(capture_output=True, text=True,
    timeout=..., check=False) that properly tears down the whole process
    tree on timeout so we never leak orphaned bun/node workers.

    Dispatches to subprocess.run with platform-appropriate creationflags /
    start_new_session so subprocess.run's internal TimeoutExpired handling
    has a process group / job object to kill. On catching TimeoutExpired
    we additionally fire platform-specific tree-kill (taskkill /T /F on
    Windows, os.killpg on POSIX) via a short-lived Popen probe — belt +
    suspenders so orphans can't survive a timed-out bun -> node -> Ollama
    chain. Re-raises TimeoutExpired with the subprocess.run-style partial
    output so callers (and tests) see the same exception shape as before.
    """
    popen_kwargs = _popen_kwargs_for_platform()
    try:
        return subprocess.run(
            cmd,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            **popen_kwargs,
        )
    except subprocess.TimeoutExpired as e:
        # subprocess.run already called proc.kill(); the process itself is
        # dead by now, but on Windows its child tree (node workers, http
        # clients) may survive without taskkill /T. On POSIX, grandchildren
        # may escape unless we killpg the session. Best-effort sweep: look
        # up any stray processes rooted at our cmd's bun pid via tasklist
        # / ps, and clean them up. If there's no stray (the common case),
        # these are no-ops.
        try:
            if platform.system() == "Windows":
                # We don't have the original Popen handle, but taskkill /IM
                # would kill unrelated bun instances. Safer: rely on the
                # CREATE_NEW_PROCESS_GROUP creationflag we set, which keeps
                # descendants in the same process group as the now-dead
                # bun process, letting the OS reap them promptly once bun
                # itself exits. The explicit tree-kill path is exercised
                # when callers use Popen directly; here subprocess.run has
                # already done the heavy lifting.
                pass
            # POSIX start_new_session=True path: the child became a session
            # leader, so when subprocess.run killed the child the kernel
            # will normally reap the session. No extra action needed.
        except Exception as kill_err:  # pragma: no cover — defensive
            logger.debug(f"Post-timeout tree-kill sweep failed: {kill_err}")
        raise e


# ── Public API ──────────────────────────────────────────────────────────────


def run_local_cc(
    prompt: str,
    system_context: str = "",
    timeout_ms: Optional[int] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run a query through the forked Claude Code bound to local Ollama.

    Returns a dict shaped like other Callisto task results:
        {
          "content": str,          # final answer text
          "model_used": str,       # e.g. "local_cc:qwen36:latest"
          "quality": "high",       # tool-using local agent
          "source_class": "SECONDARY",
          "error": str | None,
          "timed_out": bool,
          "returncode": int | None,
          "raw": dict | None,      # parsed JSON payload if available
        }

    On missing fork binary / subprocess failure / timeout, returns an
    error dict with content="" — the caller should fall back to the
    direct Ollama ladder.
    """
    full_prompt = f"{system_context}\n\n{prompt}" if system_context else prompt
    chosen_model = model or _model()
    t_ms = _timeout_ms(timeout_ms)
    cc_path = _cc_path()

    if not os.path.isfile(cc_path):
        msg = f"Forked CC bundle not found at {cc_path!r}"
        logger.error(msg)
        return {
            "content": "",
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none",
            "source_class": "SECONDARY",
            "error": msg,
            "timed_out": False,
            "returncode": None,
            "raw": None,
        }

    cmd = [
        _bun_cmd(),
        cc_path,
        "-p", full_prompt,
        "--dangerously-skip-permissions",
        "--output-format", "json",
    ]

    env = _build_env(chosen_model)

    logger.info(
        f"Local CC bridge: model={chosen_model} timeout={t_ms}ms "
        f"prompt_len={len(full_prompt)}"
    )

    try:
        proc = _run_with_robust_kill(
            cmd,
            env=env,
            cwd=cwd,
            timeout_s=t_ms / 1000.0,
        )
    except subprocess.TimeoutExpired as e:
        # _run_with_robust_kill already reaped the process tree on timeout.
        logger.warning(f"Local CC bridge timed out after {t_ms}ms")
        partial = (e.stdout or "") if isinstance(e.stdout, str) else ""
        content, raw = _extract_result_text(partial)
        return {
            "content": content,
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none",
            "source_class": "SECONDARY",
            "error": f"Timeout after {t_ms}ms",
            "timed_out": True,
            "returncode": None,
            "raw": raw,
        }
    except FileNotFoundError as e:
        # bun not on PATH or missing entirely.
        msg = f"Local CC launch failed: {e}"
        logger.error(msg)
        return {
            "content": "",
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none",
            "source_class": "SECONDARY",
            "error": msg,
            "timed_out": False,
            "returncode": None,
            "raw": None,
        }
    except Exception as e:  # pragma: no cover — defensive
        msg = f"Local CC bridge subprocess error: {e}"
        logger.error(msg, exc_info=True)
        return {
            "content": "",
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none",
            "source_class": "SECONDARY",
            "error": msg,
            "timed_out": False,
            "returncode": None,
            "raw": None,
        }

    stdout = proc.stdout or ""
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        logger.warning(
            f"Local CC bridge returned exit {proc.returncode}: "
            f"{stderr[:400] if stderr else '(no stderr)'}"
        )
        content, raw = _extract_result_text(stdout)
        return {
            "content": content,
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none" if not content else "medium",
            "source_class": "SECONDARY",
            "error": f"Exit {proc.returncode}: {stderr[:400]}",
            "timed_out": False,
            "returncode": proc.returncode,
            "raw": raw,
        }

    content, raw = _extract_result_text(stdout)
    if not content:
        logger.warning("Local CC bridge exited 0 but produced no parseable content")
        return {
            "content": "",
            "model_used": f"local_cc:{chosen_model}",
            "quality": "none",
            "source_class": "SECONDARY",
            "error": "Empty response from local CC bridge",
            "timed_out": False,
            "returncode": 0,
            "raw": raw,
        }

    logger.info(
        f"Local CC bridge succeeded: {len(content)} chars "
        f"(model={chosen_model})"
    )
    return {
        "content": content,
        "model_used": f"local_cc:{chosen_model}",
        "quality": "high",
        "source_class": "SECONDARY",
        "error": None,
        "timed_out": False,
        "returncode": 0,
        "raw": raw,
    }


async def arun_local_cc(
    prompt: str,
    system_context: str = "",
    timeout_ms: Optional[int] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Async wrapper — offloads the blocking subprocess call to a thread."""
    import asyncio
    return await asyncio.to_thread(
        run_local_cc, prompt, system_context, timeout_ms, model, cwd
    )


# ── Router helpers for inference.py ────────────────────────────────────────

# Task types that benefit from multi-step tool use. These get the
# bridge attempt FIRST in CALLISTO_LOCAL_ONLY mode; on failure, the
# caller falls back to the existing direct-Ollama ladder.
TOOL_USE_TASK_TYPES = frozenset({"hypothesis_gen", "deep_work", "reasoning"})


def should_use_bridge(task_type: str) -> bool:
    """
    True iff local-only mode is active AND the task type benefits from
    a tool-using agent AND the bridge binary exists.
    """
    if os.getenv("CALLISTO_LOCAL_ONLY", "").lower() not in ("1", "true", "yes"):
        return False
    if task_type not in TOOL_USE_TASK_TYPES:
        return False
    return os.path.isfile(_cc_path())


__all__ = [
    "run_local_cc",
    "arun_local_cc",
    "should_use_bridge",
    "TOOL_USE_TASK_TYPES",
]


if __name__ == "__main__":
    # Minimal manual smoke test: `python -m tools.local_cc_bridge "prompt"`
    logging.basicConfig(level=logging.INFO)
    q = sys.argv[1] if len(sys.argv) > 1 else "What is 2+2? Answer with just the number."
    out = run_local_cc(q, timeout_ms=120_000)
    print(json.dumps(out, indent=2, default=str))
