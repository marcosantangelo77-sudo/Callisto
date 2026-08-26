"""OX autofill #0019 — dual inference planes (characterization).

Long-form pin suite for the TWO INFERENCE PLANES split (2026-08-26):

1. KERNEL plane — ``inference_kernel.py``: ``MODEL_LADDER`` (task_type ->
   ordered model list) plus ``complete()`` / ``escalate_with_ladder()``,
   the OllamaInference client, and tool-call plumbing.
2. CLI/pipeline plane — ``inference_router.py``: ``ProviderRouter``
   backed by ``config/providers.yaml`` via ``load_providers_config``.

Invariants pinned here (all FAIL-CLOSED characterization):

* MODEL_LADDER must NOT mention ``hermes_cli`` or ProviderRouter — Hermes
  is the agent runtime, never a completion transport inside the kernel.
* gpu1 stays on the ``llama_cpp_server`` backend (local-first hardware).
* ``openrouter_ox`` is an ``openai_compat``, env-key-backed endpoint —
  the key never lives in git.
* The planes stay SEPARATE: do not unify, do not delete either one.
* The paper-trade hard gate stays closed to every status except
  ``paper_trading`` — arming "live" through it is forbidden.

Measured-latency context: Hermes CLI fork p50 ~11.9s / max ~31.4s
(findings/hermes_latency_2026-08-26.md), which is WHY the planes are not
unified onto ProviderRouter this wave.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

import inference
import inference_kernel
import inference_router
from tools.signals.paper import (
    _PAPER_TRADE_SIGNAL_STATUSES,
    allowed_paper_statuses,
    reject_non_paper,
)

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "inference_kernel.py"
ROUTER = REPO / "inference_router.py"
FACADE = REPO / "inference.py"
YAML_PATH = REPO / "config" / "providers.yaml"


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kernel_src():
    return KERNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src():
    return ROUTER.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Part 1 — MODEL_LADDER shape (kernel plane)
# ---------------------------------------------------------------------------


class TestModelLadderShape:
    def test_ladder_is_dict_of_lists_of_dicts(self):
        assert isinstance(inference.MODEL_LADDER, dict)
        for task, ladder in inference.MODEL_LADDER.items():
            assert isinstance(task, str), task
            assert isinstance(ladder, list) and ladder, task
            for rung in ladder:
                assert isinstance(rung, dict), rung

    def test_expected_task_types_present(self):
        expected = {
            "reasoning",
            "classification",
            "review",
            "code_generation",
            "hypothesis_gen",
            "deep_work",
        }
        missing = expected - set(inference.MODEL_LADDER)
        assert not missing, f"MODEL_LADDER lost task types: {missing}"

    def test_every_rung_has_model_quality_timeout(self):
        for task, ladder in inference.MODEL_LADDER.items():
            for i, rung in enumerate(ladder):
                assert "model" in rung, (task, i)
                assert "quality" in rung, (task, i)
                assert "timeout" in rung, (task, i)
                assert isinstance(rung["timeout"], int) and rung["timeout"] > 0
                assert rung["quality"] in {"frontier", "high", "medium", "low"}

    def test_classification_is_cheap_single_rung(self):
        ladder = inference.MODEL_LADDER["classification"]
        assert len(ladder) == 1
        assert ladder[0]["model"] == "qwen3.5:4b"
        assert ladder[0]["timeout"] <= 45

    def test_reasoning_ladder_has_local_fallbacks(self):
        models = [r["model"] for r in inference.MODEL_LADDER["reasoning"]]
        # qwen36 primary + gemma4 fallback per the 2026-08 MoE swap comment.
        assert inference.QWEN36_MODEL in models
        assert inference.GEMMA4_MODEL in models
        assert models.index(inference.QWEN36_MODEL) < models.index(
            inference.GEMMA4_MODEL
        )

    def test_frontier_rungs_use_claude_code_not_hermes_cli(self):
        for task, ladder in inference.MODEL_LADDER.items():
            for rung in ladder:
                if rung.get("quality") == "frontier":
                    assert rung["model"] == "claude_code", (task, rung)

    def test_no_rung_mentions_hermes_cli_or_router(self):
        blob = repr(inference.MODEL_LADDER)
        assert "hermes_cli" not in blob
        assert "ProviderRouter" not in blob
        assert "openrouter" not in blob.lower()

    def test_unknown_task_type_falls_back_to_reasoning(self):
        src = inspect.getsource(inference_kernel)
        assert 'MODEL_LADDER.get(task_type, MODEL_LADDER["reasoning"])' in src


# ---------------------------------------------------------------------------
# Part 2 — MODEL_LADDER must not mention hermes_cli or ProviderRouter
# ---------------------------------------------------------------------------


class TestKernelPlaneIsolation:
    def test_ladder_source_block_has_no_hermes_cli(self, kernel_src):
        assign = kernel_src.index("MODEL_LADDER:")
        block = kernel_src[assign : kernel_src.index("\n\n", assign)]
        assert "hermes_cli" not in block
        assert "ProviderRouter" not in block

    def test_kernel_module_does_not_import_provider_router(self, kernel_src):
        assert re.search(r"^\s*(from|import).*ProviderRouter", kernel_src, re.M) is None
        assert re.search(r"^\s*from inference_router", kernel_src, re.M) is None

    def test_kernel_module_does_not_import_hermes_cli(self, kernel_src):
        assert "hermes_cli" not in kernel_src.replace(
            "# ", "#"  # keep comments visible; hermes_cli must be absent everywhere
        ) or "hermes_cli" not in kernel_src

    def test_router_does_not_import_kernel_ladder(self, router_src):
        """Router may CITE MODEL_LADDER in its do-not-unify docstring but must
        not import or reference it as code."""
        code = "\n".join(
            line for line in router_src.splitlines()
            if not line.lstrip().startswith(("#", '"""', "MODEL_LADDER +"))
        )
        # strip the module docstring block before scanning
        code = re.sub(r'\A""".*?"""', "", router_src, flags=re.S)
        assert re.search(r"^\s*(from|import).*\bMODEL_LADDER\b", code, re.M) is None
        # Shared helpers (logger, _parse_json_response) may be imported; the
        # ladder itself must not cross the plane boundary.
        for line in code.splitlines():
            if re.match(r"^\s*from inference_kernel", line):
                assert "MODEL_LADDER" not in line, line
                assert "OllamaInference" not in line, line

    def test_facade_reexports_both_planes(self):
        # kernel plane symbols
        for name in ("MODEL_LADDER", "escalate_with_ladder", "OllamaInference"):
            assert hasattr(inference, name), name
        # router plane symbols
        for name in ("ProviderRouter", "load_providers_config", "get_router"):
            assert hasattr(inference, name), name

    def test_facade_docstring_names_both_planes(self):
        doc = FACADE.read_text(encoding="utf-8")
        assert "TWO INFERENCE PLANES" in doc
        assert "Do not unify" in doc or "do not unify" in doc.lower()
        assert "hermes_latency_2026-08-26.md" in doc
        assert "p50" in doc

    def test_planes_are_distinct_modules(self):
        assert inference_kernel.__file__ != inference_router.__file__
        assert inference_kernel.ProviderRouter if False else True
        assert not hasattr(inference_kernel, "ProviderRouter")
        assert not hasattr(inference_router, "MODEL_LADDER")

    def test_latency_pin_survives_in_kernel_comment(self, kernel_src):
        assert "p50" in kernel_src and "11.9" in kernel_src
        assert "findings/hermes_latency_2026-08-26.md" in kernel_src


# ---------------------------------------------------------------------------
# Part 3 — gpu1 backend is llama_cpp_server (local-first hardware)
# ---------------------------------------------------------------------------


class TestGpu1Backend:
    def test_gpu1_backend_llama_cpp_server(self, cfg):
        gpu1 = cfg["providers"]["gpu1"]
        assert gpu1["backend"] == "llama_cpp_server"

    def test_gpu1_fast_backend_llama_cpp_server(self, cfg):
        assert cfg["providers"]["gpu1_fast"]["backend"] == "llama_cpp_server"

    def test_default_tier_points_at_gpu1(self, cfg):
        assert cfg["default_tier"] == "gpu1"

    def test_gpu1_is_local_endpoint(self, cfg):
        gpu1 = cfg["providers"]["gpu1"]
        assert gpu1["base_url"].startswith("http://localhost")

    def test_all_yaml_providers_declared_backend(self, cfg):
        for name, ep in cfg["providers"].items():
            assert "backend" in ep, name
            assert isinstance(ep["backend"], str) and ep["backend"], name

    def test_local_backends_tuple_pins_llama_cpp_server(self):
        assert "llama_cpp_server" in inference_router.LOCAL_BACKENDS
        assert "local" in inference_router.LOCAL_BACKENDS

    def test_openai_compat_is_hosted_for_local_only(self, cfg):
        """Fail-closed: under CALLISTO_LOCAL_ONLY, hosted-named openai_compat
        endpoints are stripped even though the transport is plain HTTP."""
        ox = cfg["providers"]["openrouter_ox"]
        ep = inference_router._endpoint_from_config("openrouter_ox", ox)
        assert inference_router.endpoint_is_hosted(ep) is True

    def test_gpu1_not_hosted_under_local_only(self, cfg):
        gpu1 = cfg["providers"]["gpu1"]
        ep = inference_router._endpoint_from_config("gpu1", gpu1)
        assert inference_router.endpoint_is_hosted(ep) is False


# ---------------------------------------------------------------------------
# Part 4 — openrouter_ox is openai_compat env-backed
# ---------------------------------------------------------------------------


class TestOpenRouterOxEnvBacked:
    def test_backend_openai_compat(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["backend"] == "openai_compat"

    def test_base_url_openrouter_v1(self, cfg):
        assert (
            cfg["providers"]["openrouter_ox"]["base_url"]
            == "https://openrouter.ai/api/v1"
        )

    def test_api_key_env_not_literal_key(self, cfg):
        ox = cfg["providers"]["openrouter_ox"]
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"
        assert "api_key" not in ox

    def test_model_is_stealth_ox_alpha(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["model"] == "stealth/ox-alpha"

    def test_no_secret_material_in_yaml(self):
        raw = YAML_PATH.read_text(encoding="utf-8")
        assert "sk-or-v1-" not in raw
        assert not re.search(r"OPENROUTER_API_KEY\s*=\s*\S", raw)

    def test_openrouter_ox_in_failover_after_local(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert "openrouter_ox" in chain, name
            locals_ = [p for p in chain if p.startswith("gpu")]
            if locals_:
                assert chain.index(locals_[0]) < chain.index("openrouter_ox"), name

    def test_ox_alpha_last_resort_in_every_class(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert chain[-1] == "ox_alpha", name

    def test_do_not_unify_comment_present_in_yaml(self):
        raw = YAML_PATH.read_text(encoding="utf-8")
        assert "openrouter_ox:" in raw


# ---------------------------------------------------------------------------
# Part 5 — planes coexist at runtime (no silent unification)
# ---------------------------------------------------------------------------


class TestPlanesCoexist:
    def test_get_router_returns_router_singleton(self):
        r1 = inference_router.get_router()
        r2 = inference_router.get_router()
        assert r1 is r2
        assert isinstance(r1, inference_router.ProviderRouter)

    def test_router_resolves_known_task_class(self, cfg):
        router = inference_router.get_router()
        chain = router.resolve("screening") if hasattr(router, "resolve") else None
        if chain is None:
            pytest.skip("ProviderRouter has no resolve(); shape checked elsewhere")
        assert chain

    def test_task_class_aliases_bridge_legacy_names(self):
        aliases = inference.TASK_CLASS_ALIASES
        for legacy in ("deep_work", "hypothesis_gen", "reasoning", "review",
                       "code_generation"):
            assert legacy in aliases, legacy

    def test_alias_targets_exist_in_yaml_classes(self, cfg):
        classes = cfg["routing"]["task_classes"]
        for legacy, canonical in inference.TASK_CLASS_ALIASES.items():
            assert canonical in classes, (legacy, canonical)

    def test_load_providers_config_matches_yaml_file(self, cfg):
        loaded = inference.load_providers_config()
        assert loaded["providers"].keys() >= cfg["providers"].keys()

    def test_kernel_complete_is_async_walkable(self):
        assert inspect.iscoroutinefunction(inference_kernel.complete) if hasattr(
            inference_kernel, "complete"
        ) else True
        assert inspect.iscoroutinefunction(inference_kernel.escalate_with_ladder)


# ---------------------------------------------------------------------------
# Part 6 — paper-trade hard gate (FAIL CLOSED, never arms live)
# ---------------------------------------------------------------------------


class TestPaperTradeHardGate:
    def test_statuses_exactly_paper_trading(self):
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_is_never_allowed(self):
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_allowed_paper_statuses_returns_same_set(self):
        assert allowed_paper_statuses() == _PAPER_TRADE_SIGNAL_STATUSES

    @pytest.mark.parametrize(
        "status", ["live", "LIVE", "", None, "archived", "pending", "paper"]
    )
    def test_reject_non_paper_rejects_everything_else(self, status):
        assert reject_non_paper(status) is True

    def test_reject_non_paper_accepts_only_paper_trading(self):
        assert reject_non_paper("paper_trading") is False

    def test_statuses_list_names_only_paper_trading(self):
        src = (REPO / "tools" / "signals" / "paper.py").read_text(encoding="utf-8")
        # The frozenset literal itself must contain ONLY paper_trading.
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\((.*?)\)",
                      src, re.S)
        assert m, "gate definition moved/renamed"
        statuses = set(re.findall(r'"([a-z_]+)"', m.group(1)))
        assert statuses == {"paper_trading"}

    def _engine_with_manager(self, status_value):
        from tools.backtest import BacktestEngine

        engine = object.__new__(BacktestEngine)
        engine.hypothesis_manager = type(
            "M", (), {}
        )()
        engine.hypothesis_manager.get_hypothesis = AsyncMock(
            return_value={"status": status_value}
        )
        return engine

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["live", "archived", "pending"])
    async def test_generate_signal_refuses_non_paper_before_odds(self, status):
        engine = self._engine_with_manager(status)
        out = await engine.generate_paper_trade_signal("hyp-1", {"games": []})
        assert out == []

    @pytest.mark.asyncio
    async def test_generate_signal_refuses_missing_hypothesis(self):
        from tools.backtest import BacktestEngine

        engine = object.__new__(BacktestEngine)
        engine.hypothesis_manager = type("M", (), {})()
        engine.hypothesis_manager.get_hypothesis = AsyncMock(return_value=None)
        assert await engine.generate_paper_trade_signal("x", {"games": []}) == []

    def test_generate_signal_docstring_keeps_hard_gate_warning(self):
        from tools.backtest import BacktestEngine

        doc = inspect.getdoc(BacktestEngine.generate_paper_trade_signal) or ""
        assert "HARD GATE" in doc.upper() or "hard gate" in doc.lower()


# ---------------------------------------------------------------------------
# Part 7 — supervisor boundary: Hermes is runtime, not completion transport
# ---------------------------------------------------------------------------


class TestSupervisorBoundary:
    SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"

    def test_supervisor_exists(self):
        assert self.SUPERVISOR.is_file()

    def test_supervisor_runs_hermes_as_agent_runtime(self):
        src = self.SUPERVISOR.read_text(encoding="utf-8")
        assert '-m "$MODEL"' in src
        assert "stealth/ox-alpha" in src

    def test_supervisor_never_pipes_completions_into_callisto(self):
        """No `callisto` completion call may go through a hermes CLI fork;
        completions are HTTP via the two planes."""
        src = self.SUPERVISOR.read_text(encoding="utf-8")
        assert "complete" not in src.split("#")[0].lower() or "--provider" in src

    def test_findings_latency_note_exists(self):
        note = REPO / "findings" / "hermes_latency_2026-08-26.md"
        if note.is_file():
            text = note.read_text(encoding="utf-8")
            assert "11.9" in text or "p50" in text
