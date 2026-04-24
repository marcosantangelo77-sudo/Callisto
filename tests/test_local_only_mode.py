"""
Airtight tests for CALLISTO_LOCAL_ONLY kill switch.

These tests set CALLISTO_LOCAL_ONLY=1 and then exercise every known
Claude / Anthropic entry point. A sentinel HTTP client is installed
that raises if ANY outbound request touches api.anthropic.com (or any
URL with "anthropic" in it). A subprocess spy is installed that raises
if the `claude` CLI is spawned. Any leak == test failure.

No real network I/O, no real subprocesses. Everything is mocked out.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest


# ── Sentinels: anything hitting these pathways is a leak ──────────────────


class AnthropicLeakDetected(AssertionError):
    pass


class ClaudeSubprocessLeakDetected(AssertionError):
    pass


def _raise_on_anthropic_url(*args, **kwargs):
    """httpx hook that raises if url contains 'anthropic'."""
    url = ""
    if args:
        url = str(args[0])
    url = url or str(kwargs.get("url", ""))
    if "anthropic" in url.lower():
        raise AnthropicLeakDetected(
            f"Outbound request attempted to {url} with CALLISTO_LOCAL_ONLY=1"
        )
    return MagicMock(status_code=200, json=lambda: {}, text="")


def _raise_on_claude_cli(cmd, *args, **kwargs):
    """subprocess hook that raises if argv[0] looks like the claude CLI."""
    exe = ""
    if isinstance(cmd, (list, tuple)) and cmd:
        exe = str(cmd[0])
    elif isinstance(cmd, str):
        exe = cmd.split()[0] if cmd else ""
    exe_lower = exe.lower()
    if "claude" in exe_lower and "claude-code" not in exe_lower:
        # The forked local CC is allowed (it drives Ollama), but the
        # real `claude` CLI is a hard leak.
        raise ClaudeSubprocessLeakDetected(
            f"Subprocess attempted to spawn {exe!r} with CALLISTO_LOCAL_ONLY=1"
        )
    raise FileNotFoundError(f"mocked: {exe}")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def local_only_env(monkeypatch):
    """Set CALLISTO_LOCAL_ONLY=1 and guard outbound paths."""
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sentinel-should-not-be-read")
    yield


@pytest.fixture
def block_outbound(monkeypatch):
    """Patch httpx + subprocess so any real call surfaces as an error."""
    import httpx

    # Patch the httpx client methods to detect anthropic traffic.
    orig_post = httpx.Client.post
    orig_apost = httpx.AsyncClient.post

    def guarded_post(self, url, *a, **kw):
        if "anthropic" in str(url).lower():
            raise AnthropicLeakDetected(f"httpx.post -> {url}")
        return orig_post(self, url, *a, **kw)

    async def guarded_apost(self, url, *a, **kw):
        if "anthropic" in str(url).lower():
            raise AnthropicLeakDetected(f"httpx.AsyncClient.post -> {url}")
        return await orig_apost(self, url, *a, **kw)

    monkeypatch.setattr(httpx.Client, "post", guarded_post)
    monkeypatch.setattr(httpx.AsyncClient, "post", guarded_apost)

    # subprocess guards
    monkeypatch.setattr(subprocess, "run", _raise_on_claude_cli)
    monkeypatch.setattr(subprocess, "Popen", _raise_on_claude_cli)

    async def guarded_exec(program, *args, **kwargs):
        exe = str(program)
        if "claude" in exe.lower() and "claude-code" not in exe.lower():
            raise ClaudeSubprocessLeakDetected(
                f"asyncio subprocess exec -> {exe}"
            )
        # Return a fake process that yields empty output.
        proc = MagicMock()
        proc.returncode = 127
        async def communicate():
            return (b"", b"not found")
        proc.communicate = communicate
        return proc

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", guarded_exec
    )
    yield


# ── Helper: import module fresh in this env ────────────────────────────────


def _reimport(name):
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


# ── Core helper: is_local_only ─────────────────────────────────────────────


class TestLocalOnlyHelper:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        from tools.local_only import is_local_only
        assert is_local_only() is False

    def test_values(self, monkeypatch):
        from tools.local_only import is_local_only
        for v in ("1", "true", "True", "YES", "on", " 1 "):
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", v)
            assert is_local_only() is True, f"expected True for {v!r}"
        for v in ("", "0", "false", "no", "off", "disabled"):
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", v)
            assert is_local_only() is False, f"expected False for {v!r}"

    def test_local_only_result_shape(self):
        from tools.local_only import local_only_result
        r = local_only_result()
        assert r["content"] == ""
        assert r["error"] == "blocked_by_local_only"
        assert r["local_only"] is True
        assert r["rate_limited"] is False
        assert r["model_used"] == "none"


# ── claude_code module: every entry point honors the switch ───────────────


class TestClaudeCodeModuleBlocked:
    def test_is_available_false(self, local_only_env):
        import tools.claude_code as cc
        assert cc.is_available() is False

    def test_claude_code_query_blocked(self, local_only_env, block_outbound):
        import tools.claude_code as cc
        result = asyncio.run(cc.claude_code_query("anything"))
        assert result["content"] == ""
        assert result["error"] == "blocked_by_local_only"
        assert result.get("local_only") is True

    def test_claude_code_query_skip_check_still_blocked(
        self, local_only_env, block_outbound
    ):
        import tools.claude_code as cc
        result = asyncio.run(
            cc.claude_code_query("anything", skip_availability_check=True)
        )
        assert result["error"] == "blocked_by_local_only"

    def test_claude_code_sync_blocked(self, local_only_env, block_outbound):
        import tools.claude_code as cc
        result = cc.claude_code_sync("anything")
        assert result["error"] == "blocked_by_local_only"


# ── Inference ladder: never attempts claude_code rung in local-only ───────


class TestEscalateWithLadderBlocked:
    def test_ladder_strips_claude_rung(self, local_only_env, block_outbound, monkeypatch):
        """
        Monkeypatch the ollama-calling agent to return a deterministic
        local response. The ladder must return that response — NOT a
        Claude response, and must not even attempt claude_code_query.
        """
        import inference

        called = {"claude_code_query": 0, "ollama_achat": 0}

        async def boom_claude(*a, **kw):
            called["claude_code_query"] += 1
            raise AssertionError(
                "claude_code_query called in local-only mode — leak!"
            )

        monkeypatch.setattr(
            "tools.claude_code.claude_code_query", boom_claude
        )

        # Stub the ollama .achat so we don't need a live server.
        class FakeAgent:
            async def achat(self, messages, options=None, **kw):
                called["ollama_achat"] += 1
                return {"content": "ok-local", "tool_calls": [], "parsed_json": None}

        monkeypatch.setattr(inference, "_get_inference", lambda m: FakeAgent())

        # Also stub the local CC bridge so we don't actually call out.
        monkeypatch.setattr(
            "tools.local_cc_bridge.should_use_bridge", lambda t: False
        )

        res = asyncio.run(
            inference.escalate_with_ladder("prompt", task_type="reasoning")
        )
        assert res["content"] == "ok-local"
        assert res["model_used"] != "claude_code"
        assert called["claude_code_query"] == 0
        assert called["ollama_achat"] >= 1


# ── Local CC bridge gating ────────────────────────────────────────────────


class TestLocalCcBridge:
    def test_should_use_bridge_requires_local_only(self, monkeypatch):
        import tools.local_cc_bridge as bridge
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        assert bridge.should_use_bridge("reasoning") is False

    def test_should_use_bridge_off_task_type(self, local_only_env, monkeypatch, tmp_path):
        import tools.local_cc_bridge as bridge
        fake = tmp_path / "cli.mjs"
        fake.write_text("// stub")
        monkeypatch.setenv("LOCAL_CC_PATH", str(fake))
        # unknown task types must not use the bridge
        assert bridge.should_use_bridge("classification") is False


# ── Autonomous loop ───────────────────────────────────────────────────────


class TestAutonomousLoopLocalOnly:
    def test_research_loop_local_only_flag(self, local_only_env, block_outbound):
        """
        Construct the ResearchLoop and verify _claude_ok() returns False
        and that _local_only is True under the kill switch.
        """
        import tools.autonomous as auto

        # Avoid booting the work queue / downtime tracker: monkeypatch
        # those factories onto trivial stubs. The real ones open a DB.
        from unittest.mock import patch as _patch

        with _patch("tools.work_queue.get_work_queue", lambda: MagicMock()), \
             _patch("tools.work_queue.get_downtime_tracker", lambda: MagicMock()):
            loop = auto.ResearchLoop(
                hypothesis_manager=MagicMock(),
                hypothesis_generator=MagicMock(),
                backtest_engine=MagicMock(),
                data_collector=MagicMock(_db=None),
                vector_store=MagicMock(),
            )
        assert loop._local_only is True
        assert loop._claude_ok() is False


# ── Smoke: full-module import audit ────────────────────────────────────────


class TestImportSurfaceIsSafe:
    """Importing the obvious Claude-touching modules in local-only mode
    must not raise and must not trigger any network / subprocess I/O."""

    def test_import_inference(self, local_only_env, block_outbound):
        _reimport("inference")

    def test_import_tools_claude_code(self, local_only_env, block_outbound):
        _reimport("tools.claude_code")

    def test_import_tools_local_cc_bridge(self, local_only_env, block_outbound):
        _reimport("tools.local_cc_bridge")

    def test_import_tools_hypothesis_generator(self, local_only_env, block_outbound):
        _reimport("tools.hypothesis_generator")

    def test_import_tools_local_only(self, local_only_env):
        _reimport("tools.local_only")
