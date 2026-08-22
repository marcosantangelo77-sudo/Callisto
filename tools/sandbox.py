"""S0 — Sandboxed execution of model-authored Python.

Threat model (DEEP_RESEARCH.md §3-S0): the code is authored by an LLM that
may be prompt-injected. The realistic attack is reading environment secrets
(API keys, seal keys) and exfiltrating them, or scribbling outside its
workspace. Deny network, scrub the environment, confine the filesystem to a
scratch dir, cap CPU/wall-clock/memory, and destroy everything afterwards.
This is defense-in-depth hardening of a subprocess — not a security boundary
against a determined local adversary; run_python()'s docstring says so.

Design rules:
- Child runs `python -I` (isolated: no user site, no PYTHON* env vars).
- Environment is rebuilt from an explicit allowlist; HOME points at /tmp so
  naive os.environ reads find nothing sensitive.
- Network is denied at up to three independent layers:
    1. macOS: sandbox-exec profile denying network* and file-write* outside
       the workspace.
    2. Linux: `unshare --net` when available.
    3. In-child guard: the bootstrap replaces socket.socket with a raiser, so
       even with layers 1-2 unavailable sockets raise PermissionError.
- Wall clock enforced by subprocess timeout; on timeout the child is killed
  and the result records status="timeout" (no output is trusted).
- The scratch workspace is destroyed after every run unless keep_workspace.

Domain-general by construction: nothing here knows what the code computes.
"""
from __future__ import annotations

import json
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Limits (generous defaults for research compute; caller may override).
DEFAULT_WALL_CLOCK_S = 60
DEFAULT_CPU_S = 30
DEFAULT_MEMORY_MB = 1024
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000

# Env vars passed through to the child. Everything else is stripped — this
# closes the "read env secrets" attack.
_CHILD_ENV_ALLOWLIST = ("LANG", "LC_ALL", "TZ")

_MAIN = "__sandbox_main__.py"
_PROFILE = ".sandbox_profile"
_MARKER = "__SANDBOX_RESULT__"


@dataclass
class SandboxResult:
    """Everything needed to re-run and check a computation.

    `code` + `stdout` + `return_value` + `files` (name→sha256) are the
    reproducibility payload; the artifact store seals exactly these.
    """

    status: str  # ok | error | timeout
    code: str
    stdout: str = ""
    return_value: Any = None
    return_value_repr: Optional[str] = None
    files: list[dict] = field(default_factory=list)  # [{name, sha256, size}]
    stderr_tail: str = ""
    duration_s: float = 0.0
    limits: dict = field(default_factory=dict)
    isolation: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "code": self.code,
            "stdout": self.stdout,
            "return_value": self.return_value,
            "return_value_repr": self.return_value_repr,
            "files": self.files,
            "stderr_tail": self.stderr_tail,
            "duration_s": round(self.duration_s, 4),
            "limits": self.limits,
            "isolation": self.isolation,
            "error": self.error,
        }


def _sandbox_exec_profile(workspace: Path) -> Optional[str]:
    """sandbox-exec profile (macOS layer-1 isolation). None if unavailable."""
    if platform.system() != "Darwin":
        return None
    if not shutil.which("sandbox-exec"):
        return None
    # sandbox-exec matches subpaths against the canonical (resolved) path.
    # On macOS /tmp and /var/folders/... resolve under /private/..., so the
    # profile must spell them that way or writes are silently denied.
    import os

    ws = os.path.realpath(workspace)
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process-exec)\n"
        "(allow file-read*)\n"
        "(deny file-write*)\n"
        f'(allow file-write* (subpath "{ws}"))\n'
        "(deny network*)\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
    )


def _child_preexec_limits(memory_mb: int, cpu_s: int):
    """rlimit hardening applied inside the child before exec."""

    def _apply():
        try:
            mem = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ValueError, OSError):
            pass  # RLIMIT_AS unsupported on this platform
        try:
            cpu = min(cpu_s, 600)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        except (ValueError, OSError):
            pass

    return _apply


def _build_child_env() -> dict:
    env: dict[str, str] = {}
    for key in _CHILD_ENV_ALLOWLIST:
        if key in os_environ():
            env[key] = os_environ()[key]
    exe_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = exe_dir + _pathsep() + "/usr/bin:/bin"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = "/tmp"  # never the owner's real home
    return env


def os_environ() -> dict:
    import os

    return os.environ


def _pathsep() -> str:
    import os

    return os.pathsep


def _write_inputs(workspace: Path, inputs: Optional[dict[str, Any]]) -> list[str]:
    """Materialise `inputs` as JSON files in the workspace. Keys become
    `<name>.json`; values must be JSON-serialisable. Returns filenames the
    epilogue must exclude from output capture."""
    skip = []
    for name, value in (inputs or {}).items():
        safe = "".join(c for c in str(name) if c.isalnum() or c in "_-")
        if not safe or safe.startswith("."):
            raise ValueError(f"invalid input name: {name!r}")
        fname = f"{safe}.json"
        (workspace / fname).write_text(json.dumps(value), encoding="utf-8")
        skip.append(fname)
    return skip


def _child_script(code: str, input_names: list[str]) -> str:
    """Full child program: network kill switch + user code + marshalling."""
    bootstrap = f'''
import io, json as _json, sys
_result = {{"stdout": "", "return": None, "files": [], "error": None}}
_inputs = {json.dumps(input_names)}

class _BlockedSocket:
    def __init__(self, *a, **k):
        raise PermissionError(
            "network access is disabled in the Callisto compute sandbox")

import socket as _socket_mod
_socket_mod.socket = _BlockedSocket
_socket_mod.create_connection = _BlockedSocket
try:
    import ssl as _ssl_mod
    _ssl_mod.SSLSocket = _BlockedSocket
except Exception:
    pass

class _Tee(io.StringIO):
    def write(self, s):
        n = super().write(s)
        if len(_result["stdout"]) < 2_000_000:
            _result["stdout"] += s
        return n

sys.stdout = _Tee()
'''
    epilogue = f'''

# ---- sandbox epilogue: marshal result + generated files ----
import hashlib as _hashlib, os as _os
try:
    _rv = result  # user code may assign `result`
except NameError:
    _rv = None
_files = []
for _root, _dirs, _names in _os.walk("."):
    _dirs[:] = [d for d in _dirs if d != "__pycache__"]
    for _n in _names:
        if _n in ({_MAIN!r}, {_PROFILE!r}) or _n in _inputs:
            continue
        if _n.endswith(".pyc"):
            continue
        _p = _os.path.join(_root, _n)
        try:
            _h = _hashlib.sha256()
            with open(_p, "rb") as _fh:
                for _chunk in iter(lambda: _fh.read(65536), b""):
                    _h.update(_chunk)
            _files.append({{"name": _n, "sha256": _h.hexdigest(),
                           "size": _os.path.getsize(_p)}})
        except OSError:
            pass
_rv_repr = None
if _rv is not None:
    try:
        _json.dumps(_rv)
        _rv_repr = _rv
    except (TypeError, ValueError):
        _rv_repr = repr(_rv)
sys.__stdout__.write({_MARKER!r} + _json.dumps({{
    "stdout": _result["stdout"],
    "return": _rv_repr,
    "files": _files,
}}))
'''
    return bootstrap + "\n" + code + epilogue


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(workspace: Path, input_names: list[str]) -> list[dict]:
    """Fallback file collection when the child died before marshalling."""
    skip = {_MAIN, _PROFILE, *input_names}
    found = []
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or p.name in skip or p.suffix == ".pyc":
            continue
        if "__pycache__" in p.parts:
            continue
        try:
            found.append({
                "name": p.name,
                "sha256": _sha256_file(p),
                "size": p.stat().st_size,
            })
        except OSError:
            pass
    return found


def run_python(
    code: str,
    inputs: Optional[dict] = None,
    *,
    wall_clock_s: int = DEFAULT_WALL_CLOCK_S,
    cpu_s: int = DEFAULT_CPU_S,
    memory_mb: int = DEFAULT_MEMORY_MB,
    keep_workspace: bool = False,
) -> SandboxResult:
    """Execute untrusted model-authored Python in a hardened subprocess.

    NOT a security boundary against a determined local adversary — it is
    defense-in-depth for prompt-injected LLM code on the owner's machine:
    no network (sandbox-exec / unshare / in-child socket block), scrubbed
    environment, rlimits, wall-clock kill, disposable scratch workspace.

    Contract for the child code:
    - inputs arrive as `<name>.json` files in the cwd;
    - print for stdout, and/or assign a JSON-serialisable value to `result`
      for structured return;
    - files written to the cwd are captured (name + sha256 + size).

    With keep_workspace=True the returned SandboxResult gains `workspace`
    pointing at the preserved scratch dir (for artifact extraction).
    """
    started = time.monotonic()
    workspace = Path(tempfile.mkdtemp(prefix="callisto_sbx_"))
    isolation = {"layers": ["isolated-interpreter", "env-scrub", "in-child-socket-block"]}
    limits = {"wall_clock_s": wall_clock_s, "cpu_s": cpu_s, "memory_mb": memory_mb}

    try:
        input_names = _write_inputs(workspace, inputs)

        argv: list[str] = []
        # Layer 1: macOS seatbelt.
        profile = _sandbox_exec_profile(workspace)
        if profile:
            (workspace / _PROFILE).write_text(profile)
            argv = ["/usr/bin/sandbox-exec", "-f", str(workspace / _PROFILE)]
            isolation["layers"].insert(0, "sandbox-exec")
        # Layer 2: Linux network namespace.
        elif platform.system() == "Linux" and shutil.which("unshare"):
            argv = ["unshare", "--net"]
            isolation["layers"].insert(0, "unshare-net")

        script_path = workspace / _MAIN
        script_path.write_text(_child_script(code, input_names), encoding="utf-8")
        argv += [sys.executable, "-I", str(script_path)]

        result = SandboxResult(status="error", code=code, limits=limits,
                               isolation=isolation)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workspace),
                env=_build_child_env(),
                capture_output=True,
                timeout=wall_clock_s,
                preexec_fn=_child_preexec_limits(memory_mb, cpu_s),
            )
        except subprocess.TimeoutExpired as exc:
            result.status = "timeout"
            result.stdout = (exc.stdout or b"").decode("utf-8", "replace")[-2000:]
            result.stderr_tail = (exc.stderr or b"").decode("utf-8", "replace")[-2000:]
            result.error = f"wall-clock limit exceeded ({wall_clock_s}s); process killed"
            result.duration_s = time.monotonic() - started
            return result

        result.duration_s = time.monotonic() - started
        result.status = "ok" if proc.returncode == 0 else "error"
        out_text = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        result.stderr_tail = stderr[-2000:]

        payload = None
        idx = out_text.rfind(_MARKER)
        if idx >= 0:
            head, tail = out_text[:idx], out_text[idx + len(_MARKER):]
            try:
                payload = json.loads(tail.strip())
                payload["stdout"] = payload.get("stdout") or head
            except json.JSONDecodeError:
                payload = None

        if payload:
            result.stdout = payload.get("stdout", "")[-DEFAULT_MAX_OUTPUT_BYTES:]
            result.return_value = payload.get("return")
            if isinstance(result.return_value, str) and payload.get("return") is not None:
                result.return_value_repr = result.return_value
                result.return_value = None
            else:
                result.return_value_repr = repr(result.return_value)
            result.files = payload.get("files") or []
        else:
            # Child died before marshalling (crash, SyntaxError, hard kill).
            result.stdout = out_text[-DEFAULT_MAX_OUTPUT_BYTES:]
            result.files = _collect_files(workspace, input_names)
            if not result.error:
                result.error = f"exit code {proc.returncode}"

        if keep_workspace:
            result.workspace = str(workspace)  # type: ignore[attr-defined]
            return result
    finally:
        # When keep_workspace is set the caller owns cleanup; run_python
        # returns early from inside the try block in that case.
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
    return result
