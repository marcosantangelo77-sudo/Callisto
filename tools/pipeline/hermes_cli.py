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
import json
import os
import shutil
from pathlib import Path
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


def _auth_store_path() -> Path:
    override = os.getenv("HERMES_HOME", "").strip()
    root = Path(override) if override else Path.home() / ".hermes"
    return root / "auth.json"


def hermes_logged_in() -> bool:
    """True iff a Nous Portal credential exists in the Hermes auth store.

    Does not print, return, or log token material. Does not hit the network —
    a quarantined/expired session still counts as "present" so callers can
    attempt a completion and surface Hermes' own auth error. False means
    `hermes portal login` / `hermes auth add nous` has never succeeded here.

    ChatGPT's workstation workers worked because `~/.hermes/auth.json` already
    held a Nous session. A fresh cloud VM with only the CLI binary is NOT
    logged in; treating `hermes_available()` as health was the false green.
    """
    path = _auth_store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    nous = (data.get("providers") or {}).get("nous")
    if isinstance(nous, dict):
        last_err = nous.get("last_auth_error")
        relogin = isinstance(last_err, dict) and last_err.get("relogin_required")
        has_cred = bool(
            (isinstance(nous.get("access_token"), str) and nous["access_token"].strip())
            or (isinstance(nous.get("refresh_token"), str) and nous["refresh_token"].strip())
        )
        if has_cred and not relogin:
            return True
        if relogin and not has_cred:
            return False
        if has_cred:
            return True
    pool = (data.get("credential_pool") or {}).get("nous")
    if isinstance(pool, list):
        for entry in pool:
            if not isinstance(entry, dict):
                continue
            # Presence of a pool entry means a login was stored. Do not read
            # secret fields — fingerprint / id is enough to know *a* cred exists.
            if entry.get("id") or entry.get("secret_fingerprint") or entry.get("auth_type"):
                return True
    return False


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


def build_argv(binary: str, prompt: str, cwd: str,
               provider: Optional[str] = None,
               model: Optional[str] = None) -> list[str]:
    """argv for one `hermes -z` invocation.

    provider/model are OPTIONAL routing targets (e.g. --provider nous
    -m stealth/ox-alpha). When either is unset the flag is simply omitted,
    preserving legacy behavior for configurations that don't bind a target.
    Flags precede `-z` so they can never be swallowed by the prompt.
    """
    argv = [binary]
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["-m", model]
    argv += ["-z", prompt, "--in", cwd]
    return argv


async def hermes_run(binary: str, prompt: str, cwd: str,
                     timeout_s: float,
                     provider: Optional[str] = None,
                     model: Optional[str] = None) -> tuple[int, str, str]:
    """One `hermes -z` invocation. Returns (returncode, stdout, stderr).

    Raises RuntimeError on timeout (after killing the child). Bounded by the
    shared process semaphore — callers MUST go through hermes_complete()
    rather than calling this directly, unless they manage their own bounding.
    """
    proc = await asyncio.create_subprocess_exec(
        *build_argv(binary, prompt, cwd, provider=provider, model=model),
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
                          provider: Optional[str] = None,
                          model: Optional[str] = None) -> dict:
    """Bounded, awaited CLI completion. Returns {'content', 'rc', 'stderr'}.

    Raises RuntimeError when the CLI failed AND produced nothing — partial
    stdout on a nonzero rc is returned, since the JSON may be intact.
    """
    bin_path = resolve_binary(binary)
    prompt = flatten_messages(role, messages)
    sem = proc_semaphore()
    async with sem:
        rc, out, err = await hermes_run(bin_path, prompt, cwd, timeout_s,
                                        provider=provider, model=model)
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
