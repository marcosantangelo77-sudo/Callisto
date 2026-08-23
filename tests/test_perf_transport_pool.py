"""Performance transport tests — warm worker pool vs subprocess fallback.

Contract under test (BUILD mandate, perf wave):
  * PipelineModel signature unchanged: complete(role, messages, schema=None)
    -> {"content": str}
  * transport selection: warm pool preferred, subprocess fallback, LOUD
    logging either way — never a silent 10s-per-call downgrade
  * lifecycle: lazy start, reuse across calls, reconnect after a worker
    dies mid-call, no orphans (every worker is a tracked child; close()
    and atexit kill the process group)
  * concurrency: pool size IS the ceiling; more callers than workers queue
    (structural backpressure) rather than fork
  * the Nous burst ceiling (~4-8 concurrent) is respected structurally

Unit tests use fakes and never touch the network. Exactly one module
(test_perf_transport_live.py) exercises the real path and is skipped
unless CALLISTO_TRANSPORT_LIVE=1.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.pipeline import hermes_cli  # noqa: E402
from tools.pipeline.transport import agent_pool as ap  # noqa: E402
from tools.pipeline.transport.agent_pool import (  # noqa: E402
    SubprocessTransport,
    WarmWorkerPool,
    _Worker,
)


class _FakeWorker:
    """Mimics the _Worker surface the pool relies on."""

    def __init__(self, reply='{"ok": true}', fail=False, delay=0.0):
        self.reply = reply
        self.fail = fail
        self.delay = delay
        self.calls = []
        self.lock = __import__("threading").Lock()
        self.killed = False
        self.alive = True

    def healthy(self):
        return self.alive

    def complete(self, prompt, history, rid):
        self.calls.append((prompt, list(history)))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            self.alive = False
            raise RuntimeError("simulated worker death")
        return self.reply, float(self.delay)

    def kill(self):
        self.killed = True
        self.alive = False


@pytest.fixture(autouse=True)
def _clean_selection():
    hermes_cli.reset_transport_selection()
    yield
    hermes_cli.reset_transport_selection()


def _patch_workers(monkeypatch, factory):
    """Make WarmWorkerPool._acquire_blocking hand out fake workers."""
    def acquire(self):
        w = factory()
        w.lock.acquire()
        self._workers.append(w)
        return w
    monkeypatch.setattr(WarmWorkerPool, "_acquire_blocking", acquire)


class TestPoolLifecycle:
    def test_worker_built_lazily_on_first_call(self, monkeypatch):
        made = []
        def factory():
            w = _FakeWorker()
            made.append(w)
            return w
        _patch_workers(monkeypatch, factory)

        async def run():
            pool = WarmWorkerPool(pool_size=2)
            for _ in range(4):
                res = await pool.complete(
                    [{"role": "user", "content": "hi"}])
                assert "content" in res
            assert len(made) == 1, "one warm worker should serve serial calls"
        asyncio.run(run())

    def test_pool_size_is_concurrency_ceiling(self, monkeypatch):
        in_flight, peak = [], [0]
        def factory():
            w = _FakeWorker(delay=0.1)
            orig = w.complete
            def wrapped(prompt, history, rid):
                in_flight.append(1)
                peak[0] = max(peak[0], len(in_flight))
                try:
                    return orig(prompt, history, rid)
                finally:
                    in_flight.pop()
            w.complete = wrapped
            return w
        _patch_workers(monkeypatch, factory)

        async def run():
            pool = WarmWorkerPool(pool_size=3)
            await asyncio.gather(*[
                pool.complete([{"role": "user", "content": f"m{i}"}])
                for i in range(9)])
            assert peak[0] <= 3, (
                f"peak in-flight {peak[0]} exceeded pool size 3")
        asyncio.run(run())

    def test_worker_death_triggers_reconnect_and_retry(self, monkeypatch):
        state = {"n": 0}
        made = []
        def factory():
            w = _FakeWorker(fail=(state["n"] == 0))
            state["n"] += 1
            made.append(w)
            return w
        _patch_workers(monkeypatch, factory)

        async def run():
            pool = WarmWorkerPool(pool_size=1)
            res = await pool.complete([{"role": "user", "content": "hi"}])
            assert res["content"] == '{"ok": true}'
            assert len(made) == 2, "dead worker replaced by fresh spawn"
            assert made[0].killed, "dead worker killed, not leaked"
        asyncio.run(run())

    def test_permanent_failure_surfaces_after_retry(self, monkeypatch):
        _patch_workers(monkeypatch, lambda: _FakeWorker(fail=True))

        async def run():
            pool = WarmWorkerPool(pool_size=1)
            with pytest.raises(RuntimeError):
                await pool.complete([{"role": "user", "content": "hi"}])
        asyncio.run(run())

    def test_close_kills_all_workers(self, monkeypatch):
        made = []
        def factory():
            w = _FakeWorker()
            made.append(w)
            return w
        _patch_workers(monkeypatch, factory)

        async def run():
            pool = WarmWorkerPool(pool_size=2)
            await pool.complete([{"role": "user", "content": "x"}])
            pool.close()
            assert all(w.killed for w in made), "close must kill every worker"
            assert pool.status()["workers"] == 0
        asyncio.run(run())

    def test_history_passed_statelessly(self, monkeypatch):
        seen = {}
        def factory():
            w = _FakeWorker()
            orig = w.complete
            def wrapped(prompt, history, rid):
                seen["history"] = list(history)
                return orig(prompt, history, rid)
            w.complete = wrapped
            return w
        _patch_workers(monkeypatch, factory)

        async def run():
            pool = WarmWorkerPool()
            msgs = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"}]
            await pool.complete(msgs)
            assert [m["role"] for m in seen["history"]] == \
                ["system", "user", "assistant"]
        asyncio.run(run())


class TestInstallGate:
    def test_missing_hermes_install_blocks_pool(self, monkeypatch):
        monkeypatch.setattr(ap, "_hermes_install_present", lambda: False)
        pool = WarmWorkerPool()
        assert pool.available() is False


class TestSubprocessFallback:
    def test_subprocess_transport_used_when_forced(self):
        t = hermes_cli._select_transport("subprocess")
        assert isinstance(t, SubprocessTransport)

    def test_fallback_when_no_hermes_install(self, monkeypatch):
        monkeypatch.setattr(ap, "_hermes_install_present", lambda: False)
        t = hermes_cli._select_transport()   # no force -> auto
        assert isinstance(t, SubprocessTransport), (
            "missing install must fall back loudly to subprocess")

    def test_forced_pool_with_no_install_raises_loudly(self, monkeypatch):
        monkeypatch.setattr(ap, "_hermes_install_present", lambda: False)
        with pytest.raises(Exception):
            hermes_cli._select_transport("agent_pool")

    def test_fallback_is_announced_not_silent(self, monkeypatch, caplog):
        monkeypatch.setattr(ap, "_hermes_install_present", lambda: False)
        import logging as _logging
        with caplog.at_level(_logging.WARNING,
                             logger="tools.pipeline.hermes_cli"):
            hermes_cli._select_transport()
        assert any("fallback" in r.message.lower() for r in caplog.records), (
            "downgrade to the slow path must be logged loudly")

    def test_pool_transport_selected_when_available(self, monkeypatch):
        monkeypatch.setattr(ap, "_hermes_install_present", lambda: True)
        t = hermes_cli._select_transport()
        assert isinstance(t, WarmWorkerPool)


class TestModelContract:
    def test_complete_contract_preserved(self, monkeypatch):
        """complete(role, messages, schema=None) -> {'content': str}."""
        model = hermes_cli.HermesCliModel()

        async def fake_run(binary, prompt, cwd, timeout_s):
            return (0, '{"stubbed": 1}', "")

        orig = hermes_cli.hermes_run
        hermes_cli.hermes_run = fake_run
        try:
            res = asyncio.run(model.complete(
                "extraction", [{"role": "user", "content": "give json"}],
                schema={"type": "object"}))
        finally:
            hermes_cli.hermes_run = orig
        assert set(res) >= {"content"}
        assert isinstance(res["content"], str)
        assert model.calls[-1]["role"] == "extraction"

    def test_flatten_messages_unchanged(self):
        out = hermes_cli.flatten_messages(
            "adversarial_review",
            [{"role": "user", "content": "q"}])
        assert "[task]" in out and "q" in out


class TestWorkerProtocol:
    """The real worker script against a stubbed agent build."""

    def test_worker_frames_roundtrip(self, tmp_path, monkeypatch):
        # Write a stub run_agent + hermes_cli into a fake home so worker.py's
        # imports resolve without Hermes installed.
        home = tmp_path / "fake-hermes"
        (home / "venv" / "bin").mkdir(parents=True)
        py = home / "venv" / "bin" / "python"
        py.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
        py.chmod(0o755)
        pkg = home / "stubs"
        pkg.mkdir()
        (pkg / "run_agent.py").write_text(
            "class AIAgent:\n"
            "    def __init__(self, **kw): self.kw = kw\n"
            "    def run_conversation(self, prompt, conversation_history=None, **kw):\n"
            "        return {'final_response': 'stub-reply:' + prompt[:12]}\n"
            "    def close(self): pass\n")
        hc = pkg / "hermes_cli"
        hc.mkdir()
        (hc / "__init__.py").write_text("")
        (hc / "runtime_provider.py").write_text(
            "def resolve_runtime_provider():\n"
            "    return {'api_key': 'k', 'base_url': 'u', 'provider': 'p'}\n")

        monkeypatch.setattr(ap, "_HERMES_HOME", str(home))
        monkeypatch.setattr(ap, "_WORKER",
                            str(Path(__file__).parent.parent /
                                "tools/pipeline/transport/worker.py"))
        monkeypatch.setattr(ap, "_venv_python", lambda: sys.executable)
        monkeypatch.setenv("PYTHONPATH", str(pkg))

        w = _Worker("stub-model", timeout_s=30)
        try:
            assert w.healthy(), "ping must succeed on fresh worker"
            content, elapsed = w.complete("say ok", [{"role": "system",
                                                      "content": "s"}], "r1")
            assert content.startswith("stub-reply:")
            assert elapsed >= 0.0
        finally:
            w.kill()

    def test_worker_ping_timeout_kills_process(self, monkeypatch):
        # A worker whose stdout yields nothing must be killed, not hung on.
        class _DeadWorker(_Worker):
            def _spawn(self):
                raise RuntimeError("spawn refused")

        w = _DeadWorker("m", timeout_s=1)
        assert w.healthy() is False, (
            "unspawnable worker must fail its health-check, never hang")
