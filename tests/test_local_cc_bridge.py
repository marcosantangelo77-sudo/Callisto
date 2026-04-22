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

        def fake_run(cmd, env, cwd, capture_output, text, timeout, check, **kw):
            captured["cmd"] = cmd
            captured["env"] = env
            captured["timeout"] = timeout
            captured["extra_kwargs"] = kw
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

        def fake_run(cmd, env, cwd, capture_output, text, timeout, check, **kw):
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
            # task_type="deep_work" — the short-circuit path is the same
            # for every tool-using task type; we don't pick hypothesis_gen
            # here because that type now schema-validates content and
            # would (correctly) reject the dummy "bridge-answer" string.
            result = asyncio.run(escalate_with_ladder("prompt", task_type="deep_work"))

        assert result["content"] == "bridge-answer"
        assert result["ladder_step"] == -2
        assert result.get("path") == "local_cc_bridge"
        assert result["model_used"] == "local_cc:qwen36:latest"


# ── New coverage added for the consolidate-through-ladder refactor ──────


class TestEmptyStdoutQuality:
    """Empty subprocess output must be reported as quality='none', not as
    a silent success. Closes a gap at tests/test_local_cc_bridge.py:92-97
    where we asserted no-content but not the quality tier."""

    def test_empty_stdout_returns_quality_none(self, tmp_path):
        fake_cli = tmp_path / "cli.mjs"
        fake_cli.write_text("// stub")
        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake_cli)}), \
             patch("tools.local_cc_bridge.subprocess.run",
                   return_value=_fake_completed(returncode=0, stdout="", stderr="")):
            result = bridge.run_local_cc("anything")
        assert result["content"] == ""
        assert result["quality"] == "none"
        assert result["error"] is not None


class TestLocalOnlyKillSwitch:
    """CALLISTO_LOCAL_ONLY=1 must block every Claude subprocess spawn,
    regardless of flags, at the top of claude_code_query / _sync."""

    def test_query_blocked_by_local_only(self):
        from tools import claude_code
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
            # skip_availability_check=True used to bypass the guard; it
            # must NOT bypass the kill switch any longer.
            result = asyncio.run(
                claude_code.claude_code_query("any", skip_availability_check=True)
            )
        assert result["content"] == ""
        assert result["error"] == "blocked_by_local_only"
        assert result["quality"] == "none"

    def test_sync_blocked_by_local_only(self):
        from tools import claude_code
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
            result = claude_code.claude_code_sync("any")
        assert result["content"] == ""
        assert result["error"] == "blocked_by_local_only"
        assert result["quality"] == "none"

    def test_no_subprocess_spawn_under_local_only(self):
        """Belt + suspenders: even if someone patched out the early-return
        check, we assert that the subprocess was never invoked."""
        from tools import claude_code
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}), \
             patch("asyncio.create_subprocess_exec") as spawn:
            asyncio.run(claude_code.claude_code_query("any"))
            assert spawn.call_count == 0


class TestCallCountConcurrency:
    """The hourly soft cap must never be exceeded even when many threads
    race through the ladder at the boundary."""

    def test_reserve_slot_caps_at_max(self):
        import threading
        from tools import claude_code

        claude_code._call_count = 0
        claude_code._last_reset = __import__("time").monotonic()

        results: list = []
        results_lock = threading.Lock()

        def racer():
            got = claude_code._try_reserve_call_slot()
            with results_lock:
                results.append(got)

        threads = [threading.Thread(target=racer) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = [r for r in results if r is not None]
        denied = [r for r in results if r is None]

        assert len(granted) == claude_code.MAX_CALLS_PER_HOUR
        assert len(denied) == 50 - claude_code.MAX_CALLS_PER_HOUR
        # Slot numbers are strictly 1..MAX, each unique.
        assert sorted(granted) == list(range(1, claude_code.MAX_CALLS_PER_HOUR + 1))
        # Final count is exactly the cap — not a single increment over.
        assert claude_code._call_count == claude_code.MAX_CALLS_PER_HOUR


class TestTimeOfDayRouting:
    """Outside the Claude Max hours window, Claude is demoted to the last
    rung of the ladder (or skipped if a local alternative succeeds first)."""

    def test_claude_demoted_when_outside_hours(self):
        import inference

        # Mock ET hour to 3am — well outside the default 8-14 window.
        with patch("inference._current_et_hour", return_value=3):
            ladder = inference.MODEL_LADDER["reasoning"]
            demoted = inference._demote_claude_in_ladder(ladder)

        # Claude was first in the original 'reasoning' ladder; after
        # demotion it must be last, and every other rung is ahead of it.
        assert demoted[-1]["model"] == "claude_code"
        non_claude = [r for r in demoted if r["model"] != "claude_code"]
        # Relative order of non-Claude rungs is preserved.
        original_non_claude = [r for r in ladder if r["model"] != "claude_code"]
        assert non_claude == original_non_claude

    def test_in_hours_preserves_original_ladder(self):
        import inference

        with patch("inference._current_et_hour", return_value=10):
            assert inference._in_claude_hours() is True

    def test_outside_hours_detected(self):
        import inference

        for out_hour in (0, 3, 7, 14, 18, 23):
            with patch("inference._current_et_hour", return_value=out_hour):
                assert inference._in_claude_hours() is False, f"hour {out_hour} should be outside"

        for in_hour in (8, 9, 10, 11, 12, 13):
            with patch("inference._current_et_hour", return_value=in_hour):
                assert inference._in_claude_hours() is True, f"hour {in_hour} should be inside"

    def test_env_override_disables_demotion(self):
        import inference

        with patch.dict(os.environ, {"CALLISTO_CLAUDE_HOURS": "*"}):
            with patch("inference._current_et_hour", return_value=3):
                # "*" means always-on — even at 3am ET, demotion is skipped.
                assert inference._in_claude_hours() is True

    def test_env_override_custom_window(self):
        import inference

        with patch.dict(os.environ, {"CALLISTO_CLAUDE_HOURS": "0-6"}):
            with patch("inference._current_et_hour", return_value=3):
                assert inference._in_claude_hours() is True
            with patch("inference._current_et_hour", return_value=10):
                assert inference._in_claude_hours() is False

    def test_ladder_skips_claude_at_3am_when_local_succeeds(self):
        """End-to-end: at 3am ET a local model returning content should
        win before the demoted Claude rung is ever attempted."""
        import inference

        local_calls = {"n": 0}
        claude_calls = {"n": 0}

        class _FastLocal:
            async def achat(self, messages, options=None):
                local_calls["n"] += 1
                return {"content": "local-win", "tool_calls": [], "parsed_json": None, "raw": {}}

        async def fake_claude(prompt, system_context="", timeout=None, **kw):
            claude_calls["n"] += 1
            return {"content": "claude-win", "error": None, "rate_limited": False}

        with patch("inference._current_et_hour", return_value=3), \
             patch("inference._get_inference", return_value=_FastLocal()), \
             patch("tools.claude_code.claude_code_query", side_effect=fake_claude), \
             patch("tools.claude_code.is_available", return_value=True), \
             patch("tools.local_cc_bridge.should_use_bridge", return_value=False):
            result = asyncio.run(inference.escalate_with_ladder("q", task_type="reasoning"))

        assert result["content"] == "local-win"
        assert result["model_used"] != "claude_code"
        assert claude_calls["n"] == 0  # Claude never invoked at 3am ET
        assert local_calls["n"] >= 1


class TestHypothesisGenSchemaValidation:
    """hypothesis_gen output must have the required shape; malformed
    responses are dropped and the ladder escalates."""

    def test_valid_list_of_hypothesis_dicts(self):
        import inference
        good = json.dumps([
            {"name": "a", "market": "h2h", "edge_logic": "devig", "min_signals": 10},
            {"name": "b", "market": "totals", "edge_logic": "devig", "min_signals": 5},
        ])
        assert inference._validate_hypothesis_gen_output(good) is True

    def test_missing_key_rejected(self):
        import inference
        bad = json.dumps([{"name": "a", "market": "h2h"}])  # missing edge_logic, min_signals
        assert inference._validate_hypothesis_gen_output(bad) is False

    def test_non_json_rejected(self):
        import inference
        assert inference._validate_hypothesis_gen_output("this is prose, not JSON") is False

    def test_empty_list_rejected(self):
        import inference
        assert inference._validate_hypothesis_gen_output("[]") is False

    def test_single_valid_dict_accepted(self):
        import inference
        good = json.dumps({
            "name": "x", "market": "spreads",
            "edge_logic": "devig", "min_signals": 3,
        })
        assert inference._validate_hypothesis_gen_output(good) is True


class TestCrossPlatformBridgePath:
    """LOCAL_CC_PATH env override + autodetect behaviour."""

    def test_env_override_wins(self, tmp_path):
        fake = tmp_path / "custom.mjs"
        fake.write_text("// stub")
        with patch.dict(os.environ, {"LOCAL_CC_PATH": str(fake)}):
            assert bridge._cc_path() == os.path.abspath(str(fake))

    def test_autodetect_finds_sibling(self, tmp_path, monkeypatch):
        # Point autodetect at a single known candidate that exists.
        fake = tmp_path / "dist" / "cli.mjs"
        fake.parent.mkdir(parents=True)
        fake.write_text("// stub")
        monkeypatch.delenv("LOCAL_CC_PATH", raising=False)
        monkeypatch.setattr(bridge, "_SIBLING_CC_CANDIDATES", (str(fake),))
        # Pretend the legacy fallback is absent so autodetect must hit our stub.
        monkeypatch.setattr(bridge, "_LEGACY_WINDOWS_DEFAULT", str(tmp_path / "nope.mjs"))
        assert bridge._cc_path() == os.path.abspath(str(fake))

    def test_autodetect_returns_empty_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOCAL_CC_PATH", raising=False)
        monkeypatch.setattr(bridge, "_SIBLING_CC_CANDIDATES", (str(tmp_path / "a.mjs"),))
        monkeypatch.setattr(bridge, "_LEGACY_WINDOWS_DEFAULT", str(tmp_path / "b.mjs"))
        # Neither candidate exists on disk — _cc_path returns empty string.
        assert bridge._cc_path() == ""
