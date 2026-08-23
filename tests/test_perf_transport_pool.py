"""Performance transport tests — warm agent-pool vs subprocess fallback.

Contract under test (BUILD mandate, perf wave):
  * PipelineModel signature unchanged: complete(role, messages, schema=None)
    -> {"content": str}
  * transport selection: agent pool preferred, subprocess fallback, LOUD
    logging either way — never a silent 10s-per-call downgrade
  * lifecycle: lazy build, reuse across calls, reconnect after an agent
    dies mid-call, no orphaned processes (pool agents are in-process;
    close() is wired through atexit)
  * concurrency: pool size is the ceiling; more callers than agents queue
    (backpressure) rather than fork
  * the Nous burst ceiling (~4-8 concurrent) is respected structurally

Unit tests use fakes and never touch the network. Exactly one test module
(test_perf_transport_live.py) exercises the real path and is skipped unless
CALLISTO_TRANSPORT_LIVE=1.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.pipeline import hermes_cli  # noqa: E402
from tools.pipeline.transport import agent_pool as ap  # noqa: E402
from tools.pipeline.transport.agent_pool import (  # noqa: E402
    AgentPoolTransport,
    SubprocessTransport,
)


class _FakeAgent:
    """Mimics the AIAgent surface the pool relies on."""

    def __init__(self, reply='{"ok": true}', fail=False, delay=0.0):
        self.reply = reply
        self.fail = fail
        self.delay = delay
        self.conversations = []
        self.closed = False

    def run_conversation(self, prompt, conversation_history=None,
                         **_ignored):
        self.conversations.append((prompt, list(conversation_history or [])))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated agent death")
        return {"final_response": self.reply}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_selection():
    hermes_cli.reset_transport_selection()
    yield
    hermes_cli.reset_transport_selection()


def _patch_pool_build(monkeypatch, factory):
    """Make AgentPoolTransport._build_agent produce fake agents."""
    monkeypatch.setattr(AgentPoolTransport, "_build_agent",
                        staticmethod(factory))


class TestPoolLifecycle:
    def test_agents_built_lazily_and_reused(self, monkeypatch):
        built = []
        def factory():
            a = _FakeAgent()
            built.append(a)
            return a
        _patch_pool_build(monkeypatch, factory)

        async def run():
            pool = AgentPoolTransport(pool_size=2)
            for _ in range(4):
                res = await pool.complete(
                    [{"role": "user", "content": "hi"}])
                assert "content" in res
            assert len(built) == 1, "one warm agent should serve serial calls"
        asyncio.run(run())

    def test_pool_size_is_concurrency_ceiling(self, monkeypatch):
        in_flight = []
        peak = [0]
        def factory():
            a = _FakeAgent(delay=0.15)
            orig = a.run_conversation
            def wrapped(prompt, conversation_history=None, **kw):
                in_flight.append(1)
                peak[0] = max(peak[0], len(in_flight))
                try:
                    return orig(prompt, conversation_history=conversation_history,
                                **kw)
                finally:
                    in_flight.pop()
            a.run_conversation = wrapped
            return a
        _patch_pool_build(monkeypatch, factory)

        async def run():
            pool = AgentPoolTransport(pool_size=3)
            await asyncio.gather(*[
                pool.complete([{"role": "user", "content": f"m{i}"}])
                for i in range(9)])
            assert peak[0] <= 3, (
                f"peak in-flight {peak[0]} exceeded pool size 3")
        asyncio.run(run())

    def test_agent_death_triggers_reconnect_and_retry(self, monkeypatch):
        state = {"n": 0}
        made = []
        def factory():
            a = _FakeAgent(fail=(state["n"] == 0))
            state["n"] += 1
            made.append(a)
            return a
        _patch_pool_build(monkeypatch, factory)

        async def run():
            pool = AgentPoolTransport(pool_size=1)
            res = await pool.complete([{"role": "user", "content": "hi"}])
            assert res["content"] == '{"ok": true}'
            assert len(made) == 2, "dead agent replaced by fresh build"
            assert made[0].closed, "dead agent closed, not leaked"
        asyncio.run(run())

    def test_permanent_failure_surfaces_after_retry(self, monkeypatch):
        _patch_pool_build(monkeypatch,
                          lambda: _FakeAgent(fail=True))
        async def run():
            pool = AgentPoolTransport(pool_size=1)
            with pytest.raises(RuntimeError):
                await pool.complete([{"role": "user", "content": "hi"}])
        asyncio.run(run())

    def test_close_closes_all_agents(self, monkeypatch):
        made = []
        def factory():
            a = _FakeAgent()
            made.append(a)
            return a
        _patch_pool_build(monkeypatch, factory)

        async def run():
            pool = AgentPoolTransport(pool_size=2)
            await pool.complete([{"role": "user", "content": "x"}])
            await pool.complete([{"role": "user", "content": "y"}])
            pool.close()
            assert all(a.closed for a in made)
            assert pool.status()["agents_built"] == 0
        asyncio.run(run())

    def test_history_passed_statelessly(self, monkeypatch):
        seen = {}
        def factory():
            a = _FakeAgent()
            orig = a.run_conversation
            def wrapped(prompt, conversation_history=None, **kw):
                seen["history"] = list(conversation_history or [])
                return orig(prompt, conversation_history=conversation_history,
                            **kw)
            a.run_conversation = wrapped
            return a
        _patch_pool_build(monkeypatch, factory)

        async def run():
            pool = AgentPoolTransport()
            msgs = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"}]
            await pool.complete(msgs)
            assert seen["history"][0]["role"] == "system"
        asyncio.run(run())


class TestCredentialGate:
    def test_unavailable_credentials_block_pool(self, monkeypatch):
        monkeypatch.setattr(ap, "resolve_runtime_credentials",
                            lambda: None)
        pool = AgentPoolTransport()
        assert pool.available() is False


class TestSubprocessFallback:
    def test_subprocess_transport_used_when_forced(self, monkeypatch):
        t = hermes_cli._select_transport("subprocess")
        assert isinstance(t, SubprocessTransport)

    def test_fallback_when_no_hermes_home(self, monkeypatch):
        monkeypatch.setattr(ap, "_HERMES_HOME", "/nonexistent/hermes")
        monkeypatch.setattr(ap, "resolve_runtime_credentials",
                            lambda: None)
        t = hermes_cli._select_transport()   # no force -> auto
        assert isinstance(t, SubprocessTransport), (
            "missing install must fall back loudly to subprocess")

    def test_fallback_is_announced_not_silent(self, monkeypatch, caplog):
        monkeypatch.setattr(ap, "resolve_runtime_credentials", lambda: None)
        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="tools.pipeline.hermes_cli"):
            hermes_cli._select_transport()
        assert any("SUBPROCESS fallback" in r.message or
                   "fallback" in r.message.lower()
                   for r in caplog.records), (
            "downgrade to the slow path must be logged loudly")

    def test_pool_transport_selected_when_available(self, monkeypatch):
        monkeypatch.setattr(ap, "resolve_runtime_credentials",
                            lambda: {"api_key": "k", "base_url": "u",
                                     "provider": "nous"})
        monkeypatch.setattr(AgentPoolTransport, "available",
                            lambda self: True)
        t = hermes_cli._select_transport()
        assert isinstance(t, AgentPoolTransport)


class TestModelContract:
    def test_complete_contract_preserved(self):
        """complete(role, messages, schema=None) -> {'content': str}."""
        model = hermes_cli.HermesCliModel()

        async def run():
            # Force subprocess path but stub hermes_run so no process spawns.
            async def fake_run(binary, prompt, cwd, timeout_s):
                return (0, '{"stubbed": 1}', "")
            monkey_run = fake_run
            orig = hermes_cli.hermes_run
            hermes_cli.hermes_run = monkey_run
            try:
                res = await model.complete("extraction",
                                           [{"role": "user",
                                             "content": "give json"}],
                                           schema={"type": "object"})
            finally:
                hermes_cli.hermes_run = orig
            assert set(res) >= {"content"}
            assert isinstance(res["content"], str)
            assert model.calls[-1]["role"] == "extraction"
        asyncio.run(run())

    def test_flatten_messages_unchanged(self):
        out = hermes_cli.flatten_messages(
            "adversarial_review",
            [{"role": "user", "content": "q"}])
        assert "[task]" in out and "q" in out
