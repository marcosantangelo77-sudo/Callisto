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

TRANSPORT (2026-08-23): complete() no longer shells out per call by default.
It routes through tools/pipeline/transport — a pool of warm in-process Hermes
agents (same library, same keychain auth, zero per-call startup). The
subprocess path remains as an automatic fallback when the pool cannot be
built (no importable Hermes install, no runtime credentials), and the active
transport is logged LOUDLY on the first call either way. A silent fallback
that quietly costs ~10s per call is worse than a loud one.

Real constraints, declared honestly:
  * subprocess fallback still pays ~7-10s process startup per call.
  * no streaming — the answer arrives whole or not at all.
  * JSON-in-text, not schema-enforced — structured_output capability is
    FALSE in providers.yaml. Callers extract JSON from prose and must
    tolerate (or retry) malformed output; the router will never claim
    schema guarantees it cannot enforce.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

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


def proc_semaphore() -> asyncio.Semaphore:
    """Process-count semaphore, created lazily so it binds to whatever event
    loop is running (asyncio primitives are loop-bound before Python 3.10
    semantics settle; lazy creation avoids cross-loop reuse in tests)."""
    global _hermes_proc_semaphore
    if _hermes_proc_semaphore is None:
        _hermes_proc_semaphore = asyncio.Semaphore(_default_max_procs())
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
                          timeout_s: float = 240.0,
                          transport: Optional[str] = None) -> dict:
    """Bounded, awaited CLI completion. Returns {'content', 'rc', 'stderr'}.

    Transport selection (override with CALLISTO_HERMES_TRANSPORT =
    "agent_pool" | "subprocess", or the `transport` kwarg):
      * agent_pool — warm in-process Hermes agents; per-call cost is
        inference time only (~2-10s depending on prompt).
      * subprocess — one fresh `hermes -z` per call; ~7-10s startup paid
        every call. Automatic fallback when the pool can't be built.

    The active transport is logged once, loudly, at selection time.

    Raises RuntimeError when the call failed AND produced nothing — partial
    stdout on a nonzero rc is returned, since the JSON may be intact.
    """
    selected = _select_transport(transport)
    if isinstance(selected, SubprocessTransport):
        return await selected.complete(messages, role=role, binary=binary,
                                       cwd=cwd, timeout_s=timeout_s)
    return await selected.complete(messages, role=role, timeout_s=timeout_s)


_transport_lock = threading.Lock()
_transport_instance: Optional[Any] = None
_transport_kind: Optional[str] = None
_transport_announced = False


def _announce(kind: str) -> None:
    global _transport_announced
    if not _transport_announced:
        _transport_announced = True
        if kind == "hermes-agent-pool":
            logger.info(
                "hermes_cli transport = AGENT POOL (warm in-process agents, "
                "no per-call process startup)")
        else:
            logger.warning(
                "hermes_cli transport = SUBPROCESS fallback (~7-10s process "
                "startup per call). Pool unavailable — see preceding log "
                "lines for the build failure.")


def _select_transport(force: Optional[str] = None) -> Any:
    """Resolve the transport once per process; reuse thereafter."""
    global _transport_instance, _transport_kind
    forced = force or os.getenv("CALLISTO_HERMES_TRANSPORT", "").strip() or None
    with _transport_lock:
        if (_transport_instance is not None and _transport_kind == forced):
            _announce(_transport_instance.name)
            return _transport_instance
        if forced == "subprocess":
            _transport_instance = SubprocessTransport()
            _transport_kind = "subprocess"
        else:
            try:
                pool = get_shared_pool()
                if not pool.available():
                    raise RuntimeError("Hermes runtime credentials unavailable")
                _transport_instance = pool
                _transport_kind = "agent_pool"
            except Exception as exc:
                logger.warning(
                    "hermes_cli: agent-pool transport unavailable (%s) — "
                    "falling back to subprocess path", exc)
                if forced == "agent_pool":
                    raise
                _transport_instance = SubprocessTransport()
                _transport_kind = "subprocess"
        _announce(_transport_instance.name)
        return _transport_instance


def reset_transport_selection() -> None:
    """Test hook: forget the chosen transport (and shared pool)."""
    global _transport_instance, _transport_kind, _transport_announced
    with _transport_lock:
        _transport_instance = None
        _transport_kind = None
        _transport_announced = False
    reset_shared_pool()


class HermesCliModel:
    """PipelineModel-shaped shim over hermes_complete.

    Kept as a distinct class so existing pipeline call sites are untouched;
    new code should prefer ProviderRouter with backend: hermes_cli.
    Contract unchanged: complete(role, messages, schema=None, **kw)
    -> {"content": str}.
    """

    name = "hermes-cli"

    def __init__(self, binary: Optional[str] = None, timeout_s: float = 240.0,
                 cwd: Optional[str] = None, transport: Optional[str] = None):
        self.binary = resolve_binary(binary)
        self.timeout_s = timeout_s
        self.cwd = cwd or "/tmp"
        self._transport_pref = transport
        self.calls: list[dict] = []

    async def complete(self, role: str, messages: list[dict],
                       schema=None, **_ignored) -> dict:
        # schema accepted-and-ignored for signature compatibility with the
        # Adversary caller; the backend cannot enforce schemas (see module
        # docstring) and pretending otherwise broke the adversary once.
        res = await hermes_complete(messages, role=role, binary=self.binary,
                                    cwd=self.cwd, timeout_s=self.timeout_s,
                                    transport=self._transport_pref)
        self.calls.append({"role": role,
                           "stderr": res.get("stderr", ""),
                           "chars_out": len(res["content"]),
                           "transport": res.get("transport", "?")})
        return {"content": res["content"]}
