"""Warm worker-pool transport for Hermes (Ox Alpha / Nous Portal).

Architecture (measured rationale, 2026-08-23):
  * `hermes -z` per call: ~7-15s, dominated by process startup + agent build.
  * Importing Hermes in-process is impossible — both projects ship a
    top-level ``tools`` package; the namespaces collide.
  * Solution: PERSISTENT WORKER PROCESSES. Each worker runs under the Hermes
    venv (tools/pipeline/transport/worker.py), builds one AIAgent at startup,
    then serves newline-delimited JSON requests on stdin/stdout. Agent
    startup (~1.5-3s) is paid once per worker lifetime; each completion costs
    roughly inference time.

Lifecycle (mandated):
  * lazy start on first call; workers reused across calls
  * health-check via a bounded ping before first use of a borrowed worker
    and reconnect: a worker that dies or errors mid-call is killed and
    replaced once; the request retries on the fresh worker before failing
  * never orphaned: every worker is a tracked child process; close() and
    atexit terminate them; SIGTERM propagation kills the whole pool

Concurrency honesty: pool size IS the concurrency ceiling. Extra callers
queue on asyncio.Semaphore (structural backpressure) rather than forking.
Default 3 — measured: 4 parallel Nous requests succeed cleanly, 8 triggers
HTTP 429 ("low available credits" account burst cap).
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HERMES_HOME = os.path.expanduser("~/.hermes/hermes-agent")
_HERMES_BIN = os.path.expanduser("~/.hermes/bin/hermes")
_WORKER = os.path.join(os.path.dirname(__file__), "worker.py")

_DEFAULT_POOL_SIZE = 3          # measured: 4 parallel clean, 8 -> HTTP 429
_MODEL = "stealth/ox-alpha"
_PING_TIMEOUT_S = 30.0          # cold worker builds its agent after ping


def _venv_python() -> Optional[str]:
    """Interpreter that can import hermes_cli + run_agent."""
    venv_py = os.path.join(_HERMES_HOME, "venv", "bin", "python")
    if os.path.exists(venv_py):
        return venv_py
    return sys.executable if shutil.which("hermes") else None


def _hermes_install_present() -> bool:
    return (os.path.isdir(_HERMES_HOME) and _hermes_python_available())


def _hermes_python_available() -> bool:
    return _venv_python() is not None


class _Worker:
    """One persistent worker process. NOT thread-safe; guarded by its lock."""

    def __init__(self, model: str, timeout_s: float):
        self.model = model
        self.timeout_s = timeout_s
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None

    # ── low-level ────────────────────────────────────────────────────────
    def _spawn(self) -> None:
        py = _venv_python()
        if py is None:
            raise RuntimeError("no Hermes install / venv python found")
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(
            [py, "-u", _WORKER, f"--model={self.model}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,   # Hermes chatter must never corrupt frames
            text=True, cwd=_HERMES_HOME, env=env,
            start_new_session=True,      # own process group: killable as a tree
        )

    def _send(self, req: dict, timeout_s: float) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()
        assert self.proc is not None and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout_s

        def _readline():
            # run in executor by caller when async; here blocking is fine —
            # we're always called from a worker thread.
            return self.proc.stdout.readline()

        line = _readline()
        while line and time.monotonic() < deadline:
            line = line.strip()
            if line:
                try:
                    resp = json.loads(line)
                    if "id" in resp or "pong" in resp:
                        return resp
                except json.JSONDecodeError:
                    pass
            if time.monotonic() >= deadline:
                break
            line = self.proc.stdout.readline()
        self.kill()
        raise RuntimeError(f"worker timeout/no frame after {timeout_s}s")

    def kill(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    # ── protocol ─────────────────────────────────────────────────────────
    def healthy(self) -> bool:
        """Bounded ping; rebuilds the process if it died."""
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._spawn()
                resp = self._send({"op": "ping"}, _PING_TIMEOUT_S)
                if resp.get("ok") and resp.get("agent_ready"):
                    return True
                logger.warning("worker agent build failed: %s",
                               resp.get("build_error"))
                return False
            except Exception as exc:
                logger.warning("worker ping failed (%s); will respawn", exc)
                self.kill()
                return False

    def complete(self, prompt: str, history: list[dict],
                 rid: str) -> tuple[str, float]:
        """Blocking single completion. Raises on failure."""
        with self.lock:
            resp = self._send({"op": "complete", "id": rid,
                               "prompt": prompt, "history": history},
                              self.timeout_s)
        if not resp.get("ok"):
            err = str(resp.get("error", "unknown"))
            if resp.get("agent_dead"):
                self.kill()
            raise RuntimeError(err)
        return resp.get("content", ""), float(resp.get("elapsed_s", 0))


class WarmWorkerPool:
    """Pool of persistent worker processes; PipelineModel-compatible."""

    name = "hermes-agent-pool"

    def __init__(self,
                 pool_size: Optional[int] = None,
                 timeout_s: float = 240.0,
                 model: str = _MODEL):
        env = os.getenv("CALLISTO_HERMES_POOL_SIZE")
        try:
            size = int(pool_size if pool_size is not None else
                       (env if env else _DEFAULT_POOL_SIZE))
        except ValueError:
            size = _DEFAULT_POOL_SIZE
        self.pool_size = max(1, size)
        self.timeout_s = timeout_s
        self.model = model
        self.calls: list[dict] = []
        self._workers: list[_Worker] = []
        self._lock = threading.Lock()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._sem_loop_id: Optional[int] = None
        atexit.register(self.close)

    # ── lifecycle ────────────────────────────────────────────────────────
    def available(self) -> bool:
        return _hermes_install_present()

    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = id(loop)
        if self._semaphore is None or self._sem_loop_id != key:
            self._semaphore = asyncio.Semaphore(self.pool_size)
            self._sem_loop_id = key
        return self._semaphore

    def _acquire_blocking(self) -> _Worker:
        """Borrow one worker; grow the pool up to pool_size; else wait."""
        while True:
            with self._lock:
                for w in self._workers:
                    if w.lock.acquire(blocking=False):
                        return w
                if len(self._workers) < self.pool_size:
                    w = _Worker(self.model, self.timeout_s)
                    self._workers.append(w)
                    w.lock.acquire()
                    return w
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            workers, self._workers = list(self._workers), []
        for w in workers:
            w.kill()

    # ── completion ───────────────────────────────────────────────────────
    @staticmethod
    def _history_for(messages: list[dict]) -> list[dict]:
        return [m for m in messages
                if isinstance(m, dict) and m.get("role") in
                ("system", "user", "assistant")]

    def _discard(self, w: _Worker) -> None:
        """Remove a dead/poisoned worker so acquire can grow a replacement."""
        w.kill()
        with self._lock:
            try:
                self._workers.remove(w)
            except ValueError:
                pass

    def _run_once(self, prompt: str, history: list[dict]) -> tuple[str, float]:
        w = self._acquire_blocking()
        try:
            if not w.healthy():
                self._discard(w)
                raise RuntimeError("worker failed health-check")
            return w.complete(prompt, history, rid=f"c{time.monotonic_ns()}")
        except RuntimeError as exc:
            # complete() marks agent-dead workers; anything that raised is
            # suspect — discard rather than hand it out again poisoned.
            if getattr(w, "proc", None) is None or exc.args and "died" in str(exc):
                self._discard(w)
            raise
        except Exception as exc:
            self._discard(w)   # poisoned — next acquire respawns fresh
            raise RuntimeError(f"worker died mid-call: {exc}") from exc
        finally:
            try:
                w.lock.release()
            except Exception:
                pass

    async def complete(self, messages: list[dict], *, role: str = "",
                       **_ignored) -> dict:
        """Stateless completion over a warm worker. Returns {'content', ...}."""
        from tools.pipeline.hermes_cli import flatten_messages

        prompt = flatten_messages(role, messages)
        history = self._history_for(messages)
        sem = self._sem()
        async with sem:
            loop = asyncio.get_running_loop()
            last_exc: Optional[BaseException] = None
            for attempt in (1, 2):     # one reconnect retry on fresh worker
                try:
                    content, elapsed = await loop.run_in_executor(
                        None, self._run_once, prompt, history)
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    if attempt == 2:
                        raise
                    logger.warning(
                        "transport: worker lost mid-call (%s); reconnecting",
                        exc)
                    await asyncio.sleep(0.3 * attempt)
            else:  # pragma: no cover — defensive
                raise RuntimeError("transport: exhausted retries")
        self.calls.append({"role": role, "elapsed_s": round(elapsed, 2),
                           "chars_out": len(content),
                           "transport": self.name})
        return {"content": content, "elapsed_s": round(elapsed, 2),
                "transport": self.name}

    def status(self) -> dict:
        with self._lock:
            live = sum(1 for w in self._workers
                       if w.proc is not None and w.proc.poll() is None)
        return {"transport": self.name, "pool_size": self.pool_size,
                "workers": len(self._workers), "workers_live": live}


class SubprocessTransport:
    """Fallback: the original one-process-per-call path. Loud about cost."""

    name = "hermes-subprocess"

    def __init__(self, timeout_s: float = 240.0, **_ignored):
        self.timeout_s = timeout_s
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], *, role: str = "",
                       binary: Optional[str] = None, cwd: str = "/tmp",
                       **_ignored) -> dict:
        from tools.pipeline.hermes_cli import (
            flatten_messages, hermes_run, resolve_binary, proc_semaphore)

        bin_path = resolve_binary(binary)
        prompt = flatten_messages(role, messages)
        sem = proc_semaphore()
        async with sem:
            rc, out, err = await hermes_run(bin_path, prompt, cwd,
                                            self.timeout_s)
        if rc != 0 and not out:
            raise RuntimeError(f"hermes failed (rc={rc}): {err[:300]}")
        logger.info(
            "transport=subprocess hermes call completed (~7-10s process "
            "overhead paid this call; pool transport unavailable)")
        self.calls.append({"role": role, "chars_out": len(out)})
        return {"content": out, "rc": rc, "stderr": err[-200:],
                "transport": self.name}


# ── process-wide shared pool ─────────────────────────────────────────────
_shared_pool: Optional[WarmWorkerPool] = None
_shared_lock = threading.Lock()


def get_shared_pool() -> WarmWorkerPool:
    """Process-wide pool, built once and reused across consumers."""
    global _shared_pool
    with _shared_lock:
        if _shared_pool is None:
            _shared_pool = WarmWorkerPool()
        return _shared_pool


def reset_shared_pool() -> None:
    """Test hook: drop the shared pool (new event loop / new creds)."""
    global _shared_pool
    with _shared_lock:
        if _shared_pool is not None:
            _shared_pool.close()
        _shared_pool = None
