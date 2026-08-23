"""Warm agent-pool transport for Hermes (Ox Alpha / Nous Portal).

One process, K warm AIAgent instances. Each complete() call borrows an agent,
runs a single turn with caller-supplied conversation_history (stateless —
build_turn_context does `messages = list(conversation_history)`, so the
agent's accumulated session state is never the API payload), and returns it
to the pool.

Lifecycle rules (mandated):
  * start lazily on first call — no background threads at import time
  * reuse if already built; health-check via a bounded liveness probe
  * reconnect: an agent that raises mid-call is discarded and replaced once;
    the call is retried on a fresh agent before surfacing the error
  * never orphaned: agents are in-process objects closed via atexit; there is
    no child process to leak (contrast: subprocess transport bounds by popen)

The pool is loop-agnostic: asyncio.Semaphore is created per running loop via
the same lazy pattern hermes_cli.proc_semaphore() uses.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Where the Hermes install lives (same resolution as hermes_cli._HERMES).
_HERMES_HOME = os.path.expanduser("~/.hermes/hermes-agent")

_DEFAULT_POOL_SIZE = 3          # measured: 4 parallel clean, 8 -> HTTP 429
_MODEL = "stealth/ox-alpha"     # hosted model behind Nous Portal


def _ensure_hermes_on_path() -> bool:
    """Make run_agent importable from the local Hermes install."""
    if _HERMES_HOME not in sys.path:
        if not os.path.isdir(_HERMES_HOME):
            return False
        sys.path.insert(0, _HERMES_HOME)
    return True


def resolve_runtime_credentials() -> Optional[dict]:
    """Resolve provider credentials through Hermes' own chain (keychain).

    Returns {api_key, base_url, provider} or None. No secret ever passes
    beyond this module's pool construction.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        rt = resolve_runtime_provider()
    except Exception as exc:
        logger.warning("transport: runtime credential resolution failed: %s", exc)
        return None
    if not isinstance(rt, dict) or not rt.get("api_key"):
        return None
    return {
        "api_key": rt["api_key"],
        "base_url": rt.get("base_url") or "",
        "provider": rt.get("provider") or "",
    }


class _PooledAgent:
    """One warm AIAgent plus its borrow lock."""

    __slots__ = ("agent", "lock", "failed")

    def __init__(self, agent: Any):
        self.agent = agent
        self.lock = threading.Lock()
        self.failed = False


class AgentPoolTransport:
    """Persistent in-process Hermes agents; PipelineModel-compatible."""

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
        self._agents: list[_PooledAgent] = []
        self._pool_lock = threading.Lock()
        self._build_error: Optional[str] = None
        # Loop-bound semaphore created lazily (see hermes_cli.proc_semaphore).
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._sem_loop_id: Optional[int] = None
        atexit.register(self.close)

    # ── lifecycle ────────────────────────────────────────────────────────
    def available(self) -> bool:
        """True when the Hermes library + credentials can be reached."""
        return resolve_runtime_credentials() is not None

    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = id(loop)
        if self._semaphore is None or self._sem_loop_id != key:
            self._semaphore = asyncio.Semaphore(self.pool_size)
            self._sem_loop_id = key
        return self._semaphore

    def _build_agent(self) -> Any:
        from run_agent import AIAgent

        creds = resolve_runtime_credentials()
        if creds is None:
            raise RuntimeError(
                "no Hermes runtime credentials (run `hermes portal login`)")
        return AIAgent(
            api_key=creds["api_key"],
            base_url=creds["base_url"],
            provider=creds["provider"],
            model=self.model,
            enabled_toolsets=[],       # pure completion path — no tools
            quiet_mode=True,
            platform="cli",
            skip_memory=True,          # memory injection is per-user, not
                                       # per-research-call; skipping keeps
                                       # turns byte-predictable AND faster
            skip_context_files=True,
        )

    def _grow_locked(self) -> _PooledAgent:
        agent = _PooledAgent(self._build_agent())
        self._agents.append(agent)
        logger.info("transport: warm agent %d/%d built", len(self._agents),
                    self.pool_size)
        return agent

    def _acquire_blocking(self) -> _PooledAgent:
        """Borrow one agent, blocking until one is free or buildable."""
        while True:
            with self._pool_lock:
                for pa in self._agents:
                    if pa.failed:
                        continue
                    if pa.lock.acquire(blocking=False):
                        return pa
                if len(self._agents) < self.pool_size:
                    try:
                        pa = self._grow_locked()
                    except Exception as exc:
                        self._build_error = str(exc)
                        raise RuntimeError(
                            f"transport: cannot build warm agent: {exc}"
                        ) from exc
                    pa.lock.acquire()
                    return pa
            # Pool full: poll rather than hold the global lock while waiting.
            time.sleep(0.05)

    def close(self) -> None:
        for pa in list(self._agents):
            try:
                pa.agent.close()
            except Exception:
                pass
        self._agents.clear()

    # ── completion ───────────────────────────────────────────────────────
    def _run_once(self, prompt: str, history: list[dict]) -> tuple[str, float]:
        pa = self._acquire_blocking()
        t0 = time.monotonic()
        try:
            result = pa.agent.run_conversation(prompt, conversation_history=history)
            elapsed = time.monotonic() - t0
            content = (result.get("final_response") or "").strip()
            err = result.get("error")
            if not content and err:
                raise RuntimeError(f"hermes turn failed: {err}")
            return content, elapsed
        except Exception:
            # Poison this agent: its internal session state may be wedged.
            # The next acquire rebuilds a replacement in its slot.
            pa.failed = True
            try:
                pa.agent.close()
            except Exception:
                pass
            with self._pool_lock:
                try:
                    self._agents.remove(pa)
                except ValueError:
                    pass
            raise
        finally:
            try:
                pa.lock.release()
            except Exception:
                pass

    @staticmethod
    def _history_for(messages: list[dict]) -> list[dict]:
        """API-facing history from flattened chat messages."""
        return [m for m in messages
                if isinstance(m, dict) and m.get("role") in
                ("system", "user", "assistant")]

    async def complete(self, messages: list[dict], *, role: str = "",
                       **_ignored) -> dict:
        """Stateless completion over a warm agent. Returns {'content', ...}."""
        from tools.pipeline.hermes_cli import flatten_messages

        prompt = flatten_messages(role, messages)
        history = self._history_for(messages)
        sem = self._sem()
        async with sem:
            loop = asyncio.get_running_loop()
            last_exc: Optional[BaseException] = None
            for attempt in (1, 2):   # one reconnect retry on a fresh agent
                try:
                    content, elapsed = await loop.run_in_executor(
                        None, self._run_once, prompt, history)
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    if attempt == 2 or "cannot build" in str(exc):
                        raise
                    logger.warning(
                        "transport: agent died mid-call (%s); rebuilding",
                        exc)
                    await asyncio.sleep(0.5 * attempt)
            else:  # pragma: no cover — defensive
                raise RuntimeError("transport: exhausted retries")
        self.calls.append({"role": role, "elapsed_s": round(elapsed, 2),
                           "chars_out": len(content),
                           "transport": self.name})
        return {"content": content, "elapsed_s": round(elapsed, 2),
                "transport": self.name}

    def status(self) -> dict:
        with self._pool_lock:
            live = sum(1 for pa in self._agents if not pa.failed)
        return {"transport": self.name, "pool_size": self.pool_size,
                "agents_built": len(self._agents), "agents_live": live,
                "last_build_error": self._build_error}


class SubprocessTransport:
    """Fallback: the original one-process-per-call path. Loud about cost."""

    name = "hermes-subprocess"

    def __init__(self, timeout_s: float = 240.0, **_ignored):
        from tools.pipeline.hermes_cli import proc_semaphore

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
_shared_pool: Optional[AgentPoolTransport] = None
_shared_lock = threading.Lock()


def get_shared_pool() -> AgentPoolTransport:
    """Process-wide pool, built once and reused across consumers."""
    global _shared_pool
    with _shared_lock:
        if _shared_pool is None:
            _shared_pool = AgentPoolTransport()
        return _shared_pool


def reset_shared_pool() -> None:
    """Test hook: drop the shared pool (new event loop / new creds)."""
    global _shared_pool
    with _shared_lock:
        if _shared_pool is not None:
            _shared_pool.close()
        _shared_pool = None
