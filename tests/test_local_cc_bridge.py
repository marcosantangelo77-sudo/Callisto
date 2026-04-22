"""Tests for tools.local_cc_bridge — forked CC + Ollama bridge for local-only mode.

Subprocess is mocked throughout; no real bun/CC/Ollama is invoked.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from tools import local_cc_bridge as bridge


# ── Helpers ──────────────────────────────────────────────────────────────


def _fake_completed(returncode=0, stdout='{"type":"result","subtype":"success","result":"4"}', stderr=""):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ── Parsing ──────────────────────────────────────────────────────────────


class TestExtractResultText:
    def test_single_json_object(self):
        js = '{"type":"result","subtype":"success","result":"4"}'
        text, raw = bridge._extract_result_text(js)
        assert text == "4"
        assert raw is not None
        assert raw["result"] == "4"

    def test_stream_of_json_keeps_last_result(self):
        lines = [
            '{"type":"assistant","text":"thinking..."}',
            '{"type":"tool_use","name":"Bash"}',
            '{"type":"result","subtype":"success","result":"bridge-ok"}',
        ]
        text, raw = bridge._extract_result_text("\n".join(lines))
        assert text == "bridge-ok"
        assert raw is not None and raw["type"] == "result"

    def test_unparseable_returns_raw_stdout(self):
        text, raw = bridge._extract_result_text("not json at all")
        assert text == "not json at all"
        assert raw is None

    def test_empty_returns_empty(self):
        text, raw = bridge._extract_result_text("")
        assert text == ""
        assert raw is None


# ── Env var wiring ──────────────────────────────────────────────────────


class TestBuildEnv:
    def test_required_vars_set(self):
        env = bridge._build_env("qwen36:latest")
        assert env["CLAUDE_CODE_USE_OLLAMA"] == "1"
        assert env["OLLAMA_BASE_URL"] == "http://localhost:11434"
        assert env["OLLAMA_MODEL"] == "qwen36:latest"
        assert env["OLLAMA_FORCE_PROMPT_TOOLS"] == "1"
        assert env["OLLAMA_CONTEXT_WINDOW"] == "65536"
        assert env["OLLAMA_TIMEOUT_MS"] == "600000"
        assert env["OLLAMA_KEEP_ALIVE"] == "24h"
        assert env["DISABLE_AUTO_COMPACT"] == "1"
        assert env["DISABLE_ERROR_REPORTING"] == "1"
        assert env["DISABLE_AUTOUPDATE"] == "1"

    def test_model_override(self):
        env = bridge._build_env("custom:1b")
        assert env["OLLAMA_MODEL"] == "custom:1b"

    def test_parent_env_preserved(self):
        with patch.dict(os.environ, {"SOMETHING_ELSE": "keepme"}):
            env = bridge._build_env("qwen36:latest")
        assert env.get("SOMETHING_ELSE") == "keepme"


class TestRunLocalCCEnvPassThrough:
    """Verify the subprocess receives all the required env vars."""

    def test_subprocess_receives_required_env(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")

        captured = {}

        def fake_run(cmd, env, cwd, capture_output, text, timeout, check):
            captured["cmd"] = cmd
            captured["env"] = env
            captured["timeout"] = timeout
            return _fake_completed()

        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli)}), \
             patch("tools.local_cc_bridge.subprocess.run", side_effect=fake_run):
            result = bridge.run_local_cc("hello", timeout_ms=60_000)

        assert result["content"] == "4"
        assert result["error"] is None
        env = captured["env"]
        for key in (
            "CLAUDE_CODE_USE_OLLAMA",
            "OLLAMA_BASE_URL",
            "OLLAMA_MODEL",
            "OLLAMA_FORCE_PROMPT_TOOLS",
            "OLLAMA_CONTEXT_WINDOW",
            "OLLAMA_TIMEOUT_MS",
            "OLLAMA_KEEP_ALIVE",
            "DISABLE_AUTO_COMPACT",
            "DISABLE_ERROR_REPORTING",
            "DISABLE_AUTOUPDATE",
        ):
            assert key in env, f"missing env var {key}"
        assert env["CLAUDE_CODE_USE_OLLAMA"] == "1"
        # Command construction sanity
        cmd = captured["cmd"]
        assert "-p" in cmd
        assert "hello" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        # Timeout is passed in seconds
        assert abs(captured["timeout"] - 60.0) < 0.001

    def test_model_override_via_env(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")

        captured = {}

        def fake_run(cmd, env, **kw):
            captured["env"] = env
            return _fake_completed()

        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli), "LOCAL_CC_MODEL": "gemma4"}), \
             patch("tools.local_cc_bridge.subprocess.run", side_effect=fake_run):
            bridge.run_local_cc("hi")

        assert captured["env"]["OLLAMA_MODEL"] == "gemma4"


# ── Error paths ─────────────────────────────────────────────────────────


class TestRunLocalCCErrors:
    def test_missing_fork_binary_returns_error_dict(self):
        with patch.dict(os.environ, {"LOCAL_CC_PATH": r"C:\does\not\exist.mjs"}):
            result = bridge.run_local_cc("anything")
        assert result["content"] == ""
        assert result["error"] is not None
        assert "not found" in result["error"].lower()
        assert result["timed_out"] is False

    def test_timeout_returns_timed_out_dict(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")

        def fake_run(cmd, env, cwd, capture_output, text, timeout, check):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli)}), \
             patch("tools.local_cc_bridge.subprocess.run", side_effect=fake_run):
            result = bridge.run_local_cc("slow query", timeout_ms=5_000)

        assert result["timed_out"] is True
        assert result["error"] is not None
        assert "timeout" in result["error"].lower()
        assert result["content"] == ""

    def test_bun_missing_returns_error_dict(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")

        def fake_run(*a, **kw):
            raise FileNotFoundError("bun not found")

        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli)}), \
             patch("tools.local_cc_bridge.subprocess.run", side_effect=fake_run):
            result = bridge.run_local_cc("hi")

        assert result["content"] == ""
        assert "bun" in result["error"].lower() or "launch failed" in result["error"].lower()
        assert result["timed_out"] is False

    def test_nonzero_exit_preserved(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")

        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli)}), \
             patch("tools.local_cc_bridge.subprocess.run",
                   return_value=_fake_completed(returncode=2, stdout="", stderr="boom")):
            result = bridge.run_local_cc("hi")

        assert result["returncode"] == 2
        assert "exit 2" in result["error"].lower()


# ── Router / fallback reachability ──────────────────────────────────────


class TestShouldUseBridge:
    def test_disabled_when_not_local_only(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "0", "LOCAL_CC_PATH": str(fake_cli)}):
            assert bridge.should_use_bridge("hypothesis_gen") is False

    def test_disabled_for_non_tool_use_task(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1", "LOCAL_CC_PATH": str(fake_cli)}):
            assert bridge.should_use_bridge("classification") is False

    def test_enabled_for_supported_task(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1", "LOCAL_CC_PATH": str(fake_cli)}):
            for t in ("hypothesis_gen", "deep_work", "reasoning"):
                assert bridge.should_use_bridge(t) is True, t

    def test_disabled_when_binary_missing(self):
        with patch.dict(os.environ,
                        {"CALLISTO_LOCAL_ONLY": "1", "LOCAL_CC_PATH": r"C:\nope\cli.mjs"}):
            assert bridge.should_use_bridge("hypothesis_gen") is False


class TestLadderFallbackReachable:
    """
    When the bridge fails / is disabled, the direct Ollama ladder in
    escalate_with_ladder must still run. We mock Claude as unavailable
    and one Ollama model as returning content; success means the ladder
    path is reached and produces a result.
    """

    def test_bridge_failure_falls_through_to_ladder(self):
        from inference import escalate_with_ladder

        # Bridge returns a clean failure (as if binary missing).
        async def fake_bridge(prompt, system_context="", timeout_ms=None, model=None, cwd=None):
            return {
                "content": "",
                "model_used": "local_cc:qwen36:latest",
                "quality": "none",
                "source_class": "SECONDARY",
                "error": "Forked CC bundle not found",
                "timed_out": False,
                "returncode": None,
                "raw": None,
            }

        class _StubAgent:
            async def achat(self, messages, options=None):
                return {"content": "ladder-answer", "tool_calls": [], "parsed_json": None, "raw": {}}

        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}), \
             patch("tools.local_cc_bridge.arun_local_cc", side_effect=fake_bridge), \
             patch("tools.local_cc_bridge.should_use_bridge", return_value=True), \
             patch("inference._get_inference", return_value=_StubAgent()), \
             patch("tools.claude_code.is_available", return_value=False):
            result = asyncio.run(escalate_with_ladder("prompt", task_type="reasoning"))

        # Either the ladder produced content, OR the ladder path was
        # exercised and exhausted cleanly — both prove fallback works.
        assert result["ladder_step"] != -2, "bridge sentinel should NOT be returned on bridge failure"
        assert result["content"] == "ladder-answer"
        assert result["model_used"] != "local_cc:qwen36:latest"

    def test_bridge_success_short_circuits(self):
        from inference import escalate_with_ladder

        async def fake_bridge(prompt, system_context="", timeout_ms=None, model=None, cwd=None):
            return {
                "content": "bridge-answer",
                "model_used": "local_cc:qwen36:latest",
                "quality": "high",
                "source_class": "SECONDARY",
                "error": None,
                "timed_out": False,
                "returncode": 0,
                "raw": {"type": "result", "result": "bridge-answer"},
            }

        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}), \
             patch("tools.local_cc_bridge.arun_local_cc", side_effect=fake_bridge), \
             patch("tools.local_cc_bridge.should_use_bridge", return_value=True):
            result = asyncio.run(escalate_with_ladder("prompt", task_type="hypothesis_gen"))

        assert result["content"] == "bridge-answer"
        assert result["ladder_step"] == -2
        assert result.get("path") == "local_cc_bridge"
        assert result["model_used"] == "local_cc:qwen36:latest"
