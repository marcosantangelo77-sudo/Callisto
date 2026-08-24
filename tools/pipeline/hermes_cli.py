"""Hermes CLI (Ox Alpha / Nous Portal) as a first-class Callisto backend.

Auth model: Hermes holds its Nous OAuth token in the macOS keychain under its
own service entry. Driving the CLI uses that auth without any process reading,
copying, or storing the credential. `git clone` + `hermes portal login` is the
whole setup.

Two consumers:
  * HermesCliModel   — PipelineModel shim (pipeline e2e runs, kept working)
  * ProviderRouter   — backend="hermes_cli" endpoints in providers.yaml;
                       see inference.py `_dispatch`. This is the first-class
                       path: adversary panel, empirical routing and the
                       retrodiction batch all go through it.

Real constraints, declared honestly:
  * ~14s process startup per call — one fresh CLI session per completion.
    Timeouts must budget for this; this is not a hot-path backend.
  * no streaming — the answer arrives whole or not at all.
  * JSON-in-text, not schema-enforced — structured_output capability is
    FALSE in providers.yaml. Callers extract JSON from prose and must
    tolerate (or retry) malformed output; the router will never claim
    schema guarantees it cannot enforce.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Optional

_HERMES = os.path.expanduser("~/.hermes/bin/hermes")

# Fork bounding: every complete() spawns a process. A fan-out of agents must
# not fork fifty interpreters on an 8 GB laptop. One shared semaphore bounds
# BOTH consumers (PipelineModel shim AND ProviderRouter dispatch), because
# they are separate code paths that would otherwise each need their own cap.
# Override with CALLISTO_HERMES_MAX_PROCS.
def _default_max_procs() -> int:
    try:
        return max(1, int(os.getenv("CALLISTO_HERMES_MAX_PROCS", "3")))
    except ValueError:
        return 3


_hermes_proc_semaphore: Optional[asyncio.Semaphore] = None
# Loop identity the cached semaphore was created under. asyncio primitives are
# loop-bound; a semaphore created on question 1's loop raises "is bound to a
# different event loop" when question 2 opens its own. Track the owning loop
# so proc_semaphore() re-creates (preserving the process-count limit) instead
# of poisoning every subsequent question of a batch.
_semaphore_loop_id: Optional[int] = None


def _current_loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def proc_semaphore() -> asyncio.Semaphore:
    """Process-count semaphore for the RUNNING loop, re-created when the
    running loop changes. The per-process concurrency cap is preserved; what
    changes across loops is only the primitive itself."""
    global _hermes_proc_semaphore, _semaphore_loop_id
    loop_id = _current_loop_id()
    if _hermes_proc_semaphore is None or _semaphore_loop_id != loop_id:
        _hermes_proc_semaphore = asyncio.Semaphore(_default_max_procs())
        _semaphore_loop_id = loop_id
    return _hermes_proc_semaphore


def reset_proc_semaphore() -> None:
    """Test hook: force re-creation on the next call (new event loop)."""
    global _hermes_proc_semaphore
    _hermes_proc_semaphore = None


def hermes_available() -> bool:
    return os.path.exists(_HERMES) or bool(shutil.which("hermes"))


def resolve_binary(binary: Optional[str] = None) -> str:
    return binary or (_HERMES if os.path.exists(_HERMES)
                      else shutil.which("hermes") or "hermes")


def flatten_messages(role_or_none, messages: list[dict]) -> str:
    """Flatten a chat message list into one prompt string for `-z`.

    The CLI takes a single prompt; multi-message conversations are joined
    with role markers, and a strict-JSON instruction is appended because the
    backend has no response_format enforcement — asking nicely is what we
    have, and callers must still validate.
    """
    parts = []
    for m in messages:
        who = m.get("role", "user")
        body = m.get("content", "")
        parts.append(body if who == "user" else f"[{who}]\n{body}")
    if role_or_none:
        parts.append(f"[task]\n{role_or_none}")
    parts.append(
        "\nRespond with ONLY the JSON object requested. No prose, no code "
        "fences, no commentary before or after."
    )
    return "\n\n".join(p for p in parts if p)


async def hermes_run(binary: str, prompt: str, cwd: str,
                     timeout_s: float) -> tuple[int, str, str]:
    """One `hermes -z` invocation. Returns (returncode, stdout, stderr).

    Raises RuntimeError on timeout (after killing the child). Bounded by the
    shared process semaphore — callers MUST go through hermes_complete()
    rather than calling this directly, unless they manage their own bounding.
    """
    proc = await asyncio.create_subprocess_exec(
        binary, "-z", prompt, "--in", cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"hermes timed out after {timeout_s}s")
    return (proc.returncode or 0,
            (out or b"").decode("utf-8", "replace").strip(),
            (err or b"").decode("utf-8", "replace"))


async def hermes_complete(messages: list[dict], *, role: str = "",
                          binary: Optional[str] = None, cwd: str = "/tmp",
                          timeout_s: float = 240.0) -> dict:
    """Bounded, awaited CLI completion. Returns {'content', 'rc', 'stderr'}.

    Raises RuntimeError when the CLI failed AND produced nothing — partial
    stdout on a nonzero rc is returned, since the JSON may be intact.
    """
    bin_path = resolve_binary(binary)
    prompt = flatten_messages(role, messages)
    sem = proc_semaphore()
    async with sem:
        rc, out, err = await hermes_run(bin_path, prompt, cwd, timeout_s)
    if rc != 0 and not out:
        raise RuntimeError(
            f"hermes failed (rc={rc}): {err[:300]}")
    return {"content": out, "rc": rc, "stderr": err[-200:]}


class HermesCliModel:
    """PipelineModel-shaped shim over hermes_complete.

    Kept as a distinct class so existing pipeline call sites are untouched;
    new code should prefer ProviderRouter with backend: hermes_cli.
    """

    name = "hermes-cli"

    def __init__(self, binary: Optional[str] = None, timeout_s: float = 240.0,
                 cwd: Optional[str] = None):
        self.binary = resolve_binary(binary)
        self.timeout_s = timeout_s
        self.cwd = cwd or "/tmp"
        self.calls: list[dict] = []

    async def complete(self, role: str, messages: list[dict],
                       schema=None, **_ignored) -> dict:
        # schema accepted-and-ignored for signature compatibility with the
        # Adversary caller; the backend cannot enforce schemas (see module
        # docstring) and pretending otherwise broke the adversary once.
        res = await hermes_complete(messages, role=role, binary=self.binary,
                                    cwd=self.cwd, timeout_s=self.timeout_s)
        self.calls.append({"role": role, "stderr": res["stderr"],
                           "chars_out": len(res["content"])})
        return {"content": res["content"]}
