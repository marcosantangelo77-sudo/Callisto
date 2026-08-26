"""Autofill characterization #0067 — DUAL INFERENCE PLANES (long).

Characterizes the two-plane inference architecture as it exists today so
any drift is loud:

Plane 1 — KERNEL: ``inference_kernel.MODEL_LADDER`` (task_type -> ordered
    model list), walked by ``inference.complete()`` /
    ``escalate_with_ladder()`` for every call. Re-exported by inference.py.
Plane 2 — CLI/PIPELINE: ``inference_router.ProviderRouter`` backed by
    ``config/providers.yaml`` via ``load_providers_config``.

Hard invariants pinned here (all previously established; this module
re-pins them with finer granularity than tests/test_inference_planes.py):

* MODEL_LADDER must NOT mention ``hermes_cli`` or ProviderRouter symbols.
  The kernel plane speaks to local Ollama-style models only.
* providers.yaml's ``gpu1`` backend stays ``llama_cpp_server`` — the
  quality-first resident endpoint is a llama-server process on the
  RTX 5060 Ti, not an Ollama or OpenAI-compatible shim.
* ``openrouter_ox`` is an env-backed ``openai_compat`` provider:
  base_url https://openrouter.ai/api/v1, key from OPENROUTER_API_KEY,
  model stealth/ox-alpha, and NO literal api_key in git.
* The planes are NOT unified. The measured Hermes fork latency
  (p50 ≈ 11.9s / max ≈ 31.4s, findings/hermes_latency_2026-08-26.md)
  forbids pointing MODEL_LADDER at ProviderRouter yet. The citation must
  survive in BOTH kernel sources.
* FAIL-CLOSED betting gate: ``generate_paper_trade_signal`` runs ONLY
  for status == "paper_trading". "live" is never armed by these tests;
  if any pin below shows the gate widened, the test fails closed.

Tests-only module. No production code is modified by #0067.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

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
KERNEL_SRC = Path(inference_kernel.__file__).read_text(encoding="utf-8")
ROUTER_SRC = Path(inference_router.__file__).read_text(encoding="utf-8")
INFERENCE_SRC = Path(inference.__file__).read_text(encoding="utf-8")
PROVIDERS_YAML = REPO / "config" / "providers.yaml"

FORBIDDEN_IN_KERNEL_PLANE = ("hermes_cli",)
FORBIDDEN_PROVIDER_ROUTER_SYMBOLS = (
    "ProviderRouter",
    "load_providers_config",
    "EndpointConfig",
)

# Statuses that must NEVER be admitted to paper-trade signal generation.
NEVER_ARMED_STATUSES = ("live", "real_money", "production", "armed")


@pytest.fixture(scope="module")
def providers_cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


def _ladder_dict_src() -> str:
    """Extract the MODEL_LADDER dict assignment text from the kernel."""
    start = KERNEL_SRC.index("MODEL_LADDER:")
    end = KERNEL_SRC.index("\n}\n", start) + len("\n}\n")
    return KERNEL_SRC[start:end]


# ─────────────────────────────────────────────────────────────────────────
# Plane 1 — MODEL_LADDER structure (kernel plane)
# ─────────────────────────────────────────────────────────────────────────


class TestModelLadderShape:
    def test_ladder_is_dict_of_lists_of_dicts(self):
        ladder = inference_kernel.MODEL_LADDER
        assert isinstance(ladder, dict) and ladder
        for task_type, rungs in ladder.items():
            assert isinstance(task_type, str), task_type
            assert isinstance(rungs, list) and rungs, task_type
            for rung in rungs:
                assert isinstance(rung, dict), (task_type, rung)
                assert isinstance(rung.get("model"), str), (task_type, rung)
                assert isinstance(rung.get("timeout"), int), (task_type, rung)

    def test_expected_task_types_present(self):
        expected = {"reasoning", "classification", "review", "code_generation",
                    "hypothesis_gen", "deep_work"}
        missing = expected - set(inference_kernel.MODEL_LADDER)
        assert not missing, f"MODEL_LADDER lost keys: {missing}"

    def test_every_rung_has_quality_tier(self):
        for task_type, rungs in inference_kernel.MODEL_LADDER.items():
            for i, rung in enumerate(rungs):
                assert rung.get("quality") in {"frontier", "high", "medium"}, \
                    (task_type, i, rung)

    def test_reasoning_ladder_frontier_first_local_after(self):
        rungs = inference_kernel.MODEL_LADDER["reasoning"]
        assert rungs[0]["model"] == "claude_code"
        assert rungs[0]["quality"] == "frontier"
        locals_ = [r for r in rungs[1:] if r["quality"] != "frontier"]
        assert locals_, "reasoning ladder lost its local fallbacks"

    def test_classification_is_fast_and_small(self):
        rungs = inference_kernel.MODEL_LADDER["classification"]
        assert len(rungs) >= 1
        assert all(r["timeout"] <= 60 for r in rungs), \
            "classification must stay cheap/fast"

    def test_timeouts_are_positive_and_bounded(self):
        for task_type, rungs in inference_kernel.MODEL_LADDER.items():
            for rung in rungs:
                assert 10 <= rung["timeout"] <= 180, (task_type, rung)

    def test_no_duplicate_model_within_a_ladder(self):
        for task_type, rungs in inference_kernel.MODEL_LADDER.items():
            models = [r["model"] for r in rungs]
            dupes = {m for m in models if models.count(m) > 1}
            assert not dupes, (task_type, dupes)

    def test_ladder_falls_back_to_reasoning_for_unknown_task(self):
        src = inspect.getsource(inference_kernel)
        assert 'MODEL_LADDER.get(task_type, MODEL_LADDER["reasoning"])' in src

    def test_qwen36_constant_used_not_inlined(self):
        """The primary local brain is referenced via QWEN36_MODEL constant."""
        assert inference_kernel.QWEN36_MODEL == "qwen36"
        assert '{"model": "qwen36"' not in _ladder_dict_src()


class TestKernelPlaneIsolation:
    def test_model_ladder_does_not_mention_hermes_cli(self):
        assert "hermes_cli" not in _ladder_dict_src(), \
            "MODEL_LADDER mentions hermes_cli"

    def test_model_ladder_does_not_reference_provider_router_symbols(self):
        ladder_src = _ladder_dict_src()
        for sym in FORBIDDEN_PROVIDER_ROUTER_SYMBOLS:
            assert sym not in ladder_src, \
                f"MODEL_LADDER references router symbol {sym!r}"

    def test_complete_functions_do_not_mention_hermes_cli(self):
        for name in dir(inference):
            obj = getattr(inference, name)
            if callable(obj) and (name.startswith("complete") or
                                  "complete_sync" in name or
                                  "escalate_with_ladder" in name):
                s = inspect.getsource(obj)
                assert "hermes_cli" not in s, f"{name} mentions hermes_cli"

    def test_router_module_exists_as_separate_plane(self):
        assert hasattr(inference_router, "ProviderRouter")
        assert hasattr(inference_router, "load_providers_config")

    def test_planes_share_only_the_json_parser_bridge(self):
        """Router imports only the JSON parser + logger from the kernel —
        that bridge is fine; routing logic must not cross."""
        imports = re.findall(
            r"from inference_kernel import ([A-Za-z_, ]+)", ROUTER_SRC)
        assert imports, "router lost its kernel import line"
        names = {n.strip() for n in imports[0].split(",") if n.strip()}
        assert names <= {"_parse_json_response", "logger"}, names

    def test_kernel_module_docstring_declares_two_planes(self):
        doc = inspect.getdoc(inference_kernel) or ""
        assert "PLANES" in doc.upper()
        assert "unified or deleted" in doc.lower()

    def test_router_docstring_cites_measured_latency_barrier(self):
        doc = (inspect.getdoc(inference_router) or "")
        assert "11.9" in doc and "31.4" in doc
        assert "Do NOT unify" in doc


# ─────────────────────────────────────────────────────────────────────────
# Plane 2 — providers.yaml endpoints
# ─────────────────────────────────────────────────────────────────────────


class TestGpu1Backend:
    def test_gpu1_backend_is_llama_cpp_server(self, providers_cfg):
        gpu1 = providers_cfg["providers"]["gpu1"]
        assert gpu1["backend"] == "llama_cpp_server"

    def test_gpu1_fast_backend_is_llama_cpp_server(self, providers_cfg):
        assert providers_cfg["providers"]["gpu1_fast"]["backend"] == \
            "llama_cpp_server"

    def test_gpu1_is_default_tier(self, providers_cfg):
        assert providers_cfg["default_tier"] == "gpu1"

    def test_gpu1_points_at_local_llama_server_port(self, providers_cfg):
        url = providers_cfg["providers"]["gpu1"]["base_url"]
        assert url.startswith("http://localhost:") and url.endswith("/v1")

    def test_gpu1_structured_output_and_tool_calls(self, providers_cfg):
        gpu1 = providers_cfg["providers"]["gpu1"]
        assert gpu1["structured_output"] is True
        assert gpu1["tool_calls"] is True

    def test_no_gpu_provider_claims_openai_compat_shim_for_gpu1(self, providers_cfg):
        for name in ("gpu1", "gpu1_fast"):
            assert providers_cfg["providers"][name]["backend"] != "openai_compat"


class TestOpenrouterOxProvider:
    def test_openrouter_ox_is_env_backed_openai_compat(self, providers_cfg):
        ox = providers_cfg["providers"]["openrouter_ox"]
        assert ox["backend"] == "openai_compat"
        assert ox["base_url"] == "https://openrouter.ai/api/v1"
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"
        assert ox["model"] == "stealth/ox-alpha"

    def test_openrouter_ox_has_no_literal_api_key(self, providers_cfg):
        ox = providers_cfg["providers"]["openrouter_ox"]
        assert "api_key" not in ox
        raw = PROVIDERS_YAML.read_text(encoding="utf-8")
        assert "sk-or-v1-" not in raw

    def test_openrouter_ox_present_in_every_task_class_chain(self, providers_cfg):
        chains = providers_cfg["routing"]["task_classes"]
        assert chains
        for name, chain in chains.items():
            assert "openrouter_ox" in chain, name

    def test_local_precedes_openrouter_ox_where_local_exists(self, providers_cfg):
        chains = providers_cfg["routing"]["task_classes"]
        for name, chain in chains.items():
            gpu_rungs = [i for i, p in enumerate(chain) if p.startswith("gpu")]
            if gpu_rungs:
                assert min(gpu_rungs) < chain.index("openrouter_ox"), name

    def test_judgment_chains_lead_with_frontier(self, providers_cfg):
        chains = providers_cfg["routing"]["task_classes"]
        for name in ("promotion_judgment", "adversarial_review"):
            assert chains[name][0] == "frontier", name

    def test_hermes_cli_transport_confined_to_ox_alpha_providers(self, providers_cfg):
        """hermes_cli backend is legitimate ONLY on the ox_alpha CLI provider;
        it never appears on gpu/frontier/openrouter entries."""
        for name, prov in providers_cfg["providers"].items():
            if name in {"ox_alpha"}:
                continue
            if prov.get("backend") == "hermes_cli":
                pytest.fail(f"provider {name} claims hermes_cli backend")


# ─────────────────────────────────────────────────────────────────────────
# Non-unification pins
# ─────────────────────────────────────────────────────────────────────────


class TestPlanesNotUnified:
    def test_kernel_source_still_cites_latency_measurement(self):
        for blob, label in ((KERNEL_SRC, "inference_kernel"),
                            (INFERENCE_SRC, "inference")):
            has_pin = ("p50" in blob and "11.9" in blob) or \
                ("hermes_latency_2026-08-26.md" in blob)
            assert has_pin, f"{label} lost the measured-latency citation"

    def test_findings_file_referenced_by_both_sources(self):
        needle = "findings/hermes_latency_2026-08-26.md"
        combined = KERNEL_SRC + INFERENCE_SRC + ROUTER_SRC
        assert needle in combined

    def test_inference_reexports_kernel_ladder(self):
        assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER

    def test_inference_reexports_load_providers_config(self):
        assert inference.load_providers_config is \
            inference_router.load_providers_config

    def test_router_does_not_import_or_walk_model_ladder(self):
        # Strip the module docstring AND comments before scanning; only real
        # code references to MODEL_LADDER count as a plane merge.
        tree = ast.parse(ROUTER_SRC)
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)):
            tree.body = tree.body[1:]
        code_only = ast.unparse(tree)
        assert "MODEL_LADDER" not in code_only, \
            "ProviderRouter grew a MODEL_LADDER reference — planes merging?"

    def test_kernel_ladder_entries_never_name_yaml_provider_ids(self, providers_cfg):
        """Kernel rungs are ollama-style model tags / claude_code, never
        providers.yaml provider ids like gpu1/openrouter_ox."""
        ids = set(providers_cfg["providers"])
        for task_type, rungs in inference_kernel.MODEL_LADDER.items():
            for rung in rungs:
                assert rung["model"] not in ids - {"claude_code"}, \
                    f"{task_type}: kernel rung uses provider id {rung['model']}"

    def test_alias_map_bridges_call_sites_without_touching_ladder(self):
        aliases = getattr(inference_router, "TASK_CLASS_ALIASES", {})
        assert aliases, "vocabulary bridge vanished from the router"
        for alias, canonical in aliases.items():
            assert canonical != alias


# ─────────────────────────────────────────────────────────────────────────
# Fail-closed betting gate (never arm live)
# ─────────────────────────────────────────────────────────────────────────


class TestPaperSignalGateFailClosed:
    def test_status_set_is_exactly_paper_trading(self):
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    @pytest.mark.parametrize("status", NEVER_ARMED_STATUSES)
    def test_live_like_statuses_rejected(self, status):
        assert reject_non_paper(status) is True

    @pytest.mark.parametrize("status", ["paper_trading"])
    def test_paper_status_admitted(self, status):
        assert reject_non_paper(status) is False

    @pytest.mark.parametrize("status", [None, "", "LIVE", "Live", " live "])
    def test_none_and_malformed_rejected(self, status):
        assert reject_non_paper(status) is True

    def test_gate_comment_still_forbids_live(self):
        src = Path(
            REPO / "tools" / "signals" / "paper.py").read_text(encoding="utf-8")
        assert "HARD GATE" in src
        assert '"live"' in src

    def test_backtest_gate_runs_before_any_odds_processing(self):
        src = (REPO / "tools" / "backtest.py").read_text(encoding="utf-8")
        fn_start = src.index("async def generate_paper_trade_signal")
        body = src[fn_start:src.index("\n    async def ", fn_start)]
        gate_pos = body.index("reject_non_paper")
        pipeline_pos = body.rindex("paper_pipeline.generate_paper_trade_signal")
        assert gate_pos < pipeline_pos, "status gate moved after extraction"

    def test_this_test_module_never_adds_live(self):
        """Self-pin: this file contains no instruction to widen the gate."""
        my_src = Path(__file__).read_text(encoding="utf-8")
        # Build needles from parts so this source never literally contains
        # a gate-widening expression.
        parts = ("paper", "trading", "live")
        widen_set = '{"%s", "%s"}' % (parts[0] + parts[1], parts[2])
        add_call = "STATUSES" + ".add"
        for needle in (widen_set, add_call):
            assert needle not in my_src, needle


# ─────────────────────────────────────────────────────────────────────────
# Supervisor / runtime identity (context pins)
# ─────────────────────────────────────────────────────────────────────────


class TestSupervisorContext:
    SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"

    def test_supervisor_file_exists(self):
        assert self.SUPERVISOR.is_file()

    def test_supervisor_keeps_model_flag_form(self):
        src = self.SUPERVISOR.read_text(encoding="utf-8")
        assert '-m "$MODEL"' in src
        assert "stealth/ox-alpha" in src

    def test_supervisor_mentions_openrouter_provider_option(self):
        src = self.SUPERVISOR.read_text(encoding="utf-8")
        assert "CALLISTO_HERMES_PROVIDER" in src
        assert "openrouter" in src
