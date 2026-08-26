"""Autofill characterization #0075 — dual inference planes (LONG).

Characterizes the intentional TWO-PLANE split of Callisto inference and the
adjacent safety gates, without changing any production code:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER`` + ``complete()`` /
   ``escalate_with_ladder`` (re-exported through ``inference.py``). It must
   NEVER mention hermes_cli or ProviderRouter: Hermes is the agent runtime,
   not a completion transport.
2. CLI/pipeline plane — ``inference_router.ProviderRouter`` backed by
   ``config/providers.yaml`` via ``load_providers_config()``. Here
   hermes_cli IS a legitimate backend, and gpu1/gpu1_fast are
   llama_cpp_server endpoints while openrouter_ox is an env-backed
   openai_compat endpoint.

The two planes are deliberately NOT unified. Measured Hermes CLI fork latency
(findings/hermes_latency_2026-08-26.md: p50 ≈ 11.9s, max ≈ 31.4s) forbids
collapsing MODEL_LADDER onto ProviderRouter. These tests pin that decision.

Also pins the paper-trade hard gate: ``_PAPER_TRADE_SIGNAL_STATUSES`` is
exactly frozenset({"paper_trading"}) — "live" must never be armed through it.

Tests only; no production file is modified by this module.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "inference_kernel.py"
ROUTER = REPO / "inference_router.py"
INFERENCE = REPO / "inference.py"
PROVIDERS_YAML = REPO / "config" / "providers.yaml"
SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"
PAPER = REPO / "tools" / "signals" / "paper.py"


# ---------------------------------------------------------------------------
# Module-level fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def kernel_src():
    return KERNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src():
    return ROUTER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def providers_cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


def _ladder_value() -> dict:
    """The live MODEL_LADDER, imported from the kernel plane."""
    import inference
    return dict(inference.MODEL_LADDER)


@pytest.fixture(scope="module")
def ladder_value():
    return _ladder_value()


# ---------------------------------------------------------------------------
# Part 1 — kernel plane exists and keeps its shape
# ---------------------------------------------------------------------------

class TestKernelLadderShape:
    def test_ladder_is_nonempty_mapping_of_lists(self, ladder_value):
        assert isinstance(ladder_value, dict) and ladder_value

    def test_expected_task_types_present(self, ladder_value):
        for key in ("reasoning", "classification", "review"):
            assert key in ladder_value, f"MODEL_LADDER lost {key!r}"

    def test_every_rung_is_model_quality_timeout(self, ladder_value):
        for task, rungs in ladder_value.items():
            assert isinstance(rungs, list) and rungs, task
            for rung in rungs:
                assert set(rung) == {"model", "quality", "timeout"}, (task, rung)
                assert isinstance(rung["model"], str) and rung["model"]
                assert rung["quality"] in {"frontier", "high", "medium"}
                assert isinstance(rung["timeout"], int) and 0 < rung["timeout"] <= 180

    def test_reasoning_ladder_frontier_first_local_rest(self, ladder_value):
        models = [r["model"] for r in ladder_value["reasoning"]]
        assert models[0] == "claude_code"
        localish = [m for m in models[1:] if m != "claude_code"]
        assert len(localish) >= 3

    def test_classification_is_single_fast_rung(self, ladder_value):
        rungs = ladder_value["classification"]
        assert len(rungs) == 1
        assert rungs[0]["timeout"] <= 60

    def test_no_duplicate_models_within_a_ladder(self, ladder_value):
        for task, rungs in ladder_value.items():
            models = [r["model"] for r in rungs]
            assert len(models) == len(set(models)), f"duplicate model in {task}"

    def test_timeout_bounds_per_ladder(self, ladder_value):
        for task, rungs in ladder_value.items():
            timeouts = [r["timeout"] for r in rungs]
            assert max(timeouts) <= 180 and min(timeouts) >= 30, task


# ---------------------------------------------------------------------------
# Part 2 — MODEL_LADDER / kernel plane never names hermes_cli or ProviderRouter
# ---------------------------------------------------------------------------

class TestKernelPlanePurity:
    def test_ladder_literal_does_not_mention_hermes_cli(self, ladder_value):
        blob = repr(ladder_value)
        assert "hermes_cli" not in blob
        assert "hermes" not in blob.lower()

    def test_ladder_literal_does_not_mention_provider_router(self, ladder_value):
        assert "ProviderRouter" not in repr(ladder_value)

    def test_kernel_names_are_not_hermes_transports(self, ladder_value):
        known = {
            "claude_code", "qwen36", "qwen3:14b", "qwen3.5:4b",
            "manager:latest",
        }
        for task, rungs in ladder_value.items():
            for rung in rungs:
                assert rung["model"] not in {"hermes_cli", "provider_router"}, (
                    task, rung["model"]
                )
        # sanity: at least one recognized name survives so this test cannot
        # pass vacuously against an emptied ladder
        all_models = {r["model"] for rungs in ladder_value.values() for r in rungs}
        assert all_models & known

    def test_kernel_module_docstring_declares_two_planes(self, kernel_src):
        head = kernel_src[:2000]
        assert "KERNEL" in head.upper()
        assert "inference_router" in head or "ProviderRouter" in head

    def test_kernel_comment_says_do_not_unify(self, kernel_src):
        assert "do not unify" in kernel_src.lower()

    def test_kernel_cites_measured_latency_finding(self, kernel_src):
        assert ("11.9" in kernel_src and "p50" in kernel_src) or (
            "hermes_latency_2026-08-26.md" in kernel_src
        )

    def test_complete_walks_ladder_not_router(self, kernel_src):
        m = re.search(r"async def complete\(.*?\n(?=    @|async def |def |\Z)",
                      kernel_src, re.S)
        if m is None:
            pytest.skip("complete() not found in kernel module")
        body = m.group(0)
        assert "MODEL_LADDER" in body
        assert "ProviderRouter(" not in body
        assert "hermes_cli" not in body

    def test_inference_reexports_model_ladder(self):
        import inference
        from inference_kernel import MODEL_LADDER
        assert inference.MODEL_LADDER is MODEL_LADDER

    def test_inference_module_hides_hermes_transport_from_kernel_api(self):
        import inference
        public = [n for n in dir(inference) if not n.startswith("_")]
        assert "hermes_cli" not in public
        assert not any("hermes" in n.lower() for n in public)


# ---------------------------------------------------------------------------
# Part 3 — CLI/pipeline plane: ProviderRouter + providers.yaml
# ---------------------------------------------------------------------------

class TestRouterPlane:
    def test_router_module_exists_and_defines_provider_router(self, router_src):
        assert "class ProviderRouter" in router_src

    def test_router_supports_hermes_cli_backend(self, router_src):
        # hermes_cli is legitimate HERE (and only here)
        assert 'backend == "hermes_cli"' in router_src

    def test_yaml_parses_with_providers_and_routing(self, providers_cfg):
        assert isinstance(providers_cfg["providers"], dict)
        assert isinstance(providers_cfg["routing"]["task_classes"], dict)

    def test_gpu1_backend_is_llama_cpp_server(self, providers_cfg):
        ep = providers_cfg["providers"]["gpu1"]
        assert ep["backend"] == "llama_cpp_server"
        assert ep["base_url"].startswith("http://")
        assert ep["context_tokens"] > 0

    def test_gpu1_fast_backend_is_llama_cpp_server(self, providers_cfg):
        ep = providers_cfg["providers"]["gpu1_fast"]
        assert ep["backend"] == "llama_cpp_server"

    def test_default_tier_is_a_real_llama_endpoint(self, providers_cfg):
        tier = providers_cfg["default_tier"]
        assert providers_cfg["providers"][tier]["backend"] == "llama_cpp_server"

    def test_openrouter_ox_is_openai_compat_env_backed(self, providers_cfg):
        ox = providers_cfg["providers"]["openrouter_ox"]
        assert ox["backend"] == "openai_compat"
        assert ox["base_url"] == "https://openrouter.ai/api/v1"
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"
        assert ox["model"] == "stealth/ox-alpha"
        assert "api_key" not in ox

    def test_no_secret_material_in_providers_yaml(self):
        raw = PROVIDERS_YAML.read_text(encoding="utf-8")
        assert "sk-or-v1-" not in raw
        assert "sk-" not in raw.replace("sk-or-v1-", "")
        for line in raw.splitlines():
            if re.search(r"api_key(?!_env)", line) and ":" in line:
                val = line.split(":", 1)[1].strip()
                assert val == "" , f"hardcoded api_key found: {line!r}"

    def test_task_class_chains_put_local_before_openrouter_ox(self, providers_cfg):
        chains = providers_cfg["routing"]["task_classes"]
        for name, chain in chains.items():
            if "gpu1" in chain and "openrouter_ox" in chain:
                assert chain.index("gpu1") < chain.index("openrouter_ox"), name

    def test_every_chain_ends_in_an_ox_fallback(self, providers_cfg):
        for name, chain in providers_cfg["routing"]["task_classes"].items():
            assert chain[-1] in {"ox_alpha", "ox_alpha_proxy", "openrouter_ox"}, name

    def test_planes_are_distinct_files(self):
        assert KERNEL.name != ROUTER.name
        assert KERNEL.is_file() and ROUTER.is_file()

    def test_kernel_does_not_import_inference_router(self, kernel_src):
        assert "from inference_router" not in kernel_src
        assert "import inference_router" not in kernel_src

    def test_router_does_not_touch_model_ladder(self, router_src):
        # MODEL_LADDER may appear only in the docstring/comment explaining the
        # two-plane split — never in router code.
        code_only = "\n".join(
            l for l in router_src.splitlines() if not l.lstrip().startswith("#")
        )
        code_no_doc = re.sub(r'""".*?"""', "", code_only, flags=re.S)
        assert "MODEL_LADDER" not in code_no_doc


# ---------------------------------------------------------------------------
# Part 4 — load_providers_config wiring
# ---------------------------------------------------------------------------

class TestProvidersConfigLoading:
    def test_load_providers_config_exposed_via_inference(self):
        import inference
        assert callable(inference.load_providers_config)

    def test_loaded_config_matches_yaml_file(self):
        import inference
        cfg = inference.load_providers_config()
        disk = yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))
        assert cfg.get("providers") == disk.get("providers")

    def test_loaded_config_has_at_least_one_provider(self):
        import inference
        cfg = inference.load_providers_config()
        assert len(cfg["providers"]) >= 3

    def test_backends_are_from_known_vocabulary(self, providers_cfg):
        known = {"llama_cpp_server", "openai_compat", "hermes_cli", "ollama"}
        for name, ep in providers_cfg["providers"].items():
            assert ep.get("backend") in known, (name, ep.get("backend"))


# ---------------------------------------------------------------------------
# Part 5 — supervisor: Hermes is agent RUNTIME, not transport; -m unchanged
# ---------------------------------------------------------------------------

class TestSupervisorRuntimeRole:
    @pytest.fixture(scope="class")
    def sup_src(self):
        if not SUPERVISOR.is_file():
            pytest.skip("nous-supervisor.sh absent")
        return SUPERVISOR.read_text(encoding="utf-8")

    def test_supervisor_launches_hermes_with_dash_m(self, sup_src):
        assert '-m "$MODEL"' in sup_src

    def test_supervisor_pins_ox_alpha_model(self, sup_src):
        assert "stealth/ox-alpha" in sup_src

    def test_supervisor_has_provider_switch_not_hardcode(self, sup_src):
        assert "CALLISTO_HERMES_PROVIDER" in sup_src
        assert '--provider "$PROVIDER"' in sup_src or '--provider "$PROVIDER"' in sup_src

    def test_supervisor_does_not_leak_secrets(self, sup_src):
        assert "sk-or-v1-" not in sup_src
        # key may be referenced by NAME but never printed raw
        assert not re.search(r'echo\s+"\$OPENROUTER_API_KEY"', sup_src)


# ---------------------------------------------------------------------------
# Part 6 — paper-trade hard gate (fail closed)
# ---------------------------------------------------------------------------

class TestPaperTradeHardGate:
    def test_statuses_exactly_paper_trading(self):
        from tools.signals import paper
        assert paper._PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_is_rejected(self):
        from tools.signals import paper
        assert paper.reject_non_paper("live") is True

    def test_paper_trading_passes_gate(self):
        from tools.signals import paper
        assert paper.reject_non_paper("paper_trading") is False

    def test_arbitrary_statuses_rejected(self):
        from tools.signals import paper
        for bad in ("LIVE", "Live", "", None, "live ", "production", "real_money"):
            assert paper.reject_non_paper(bad) is True

    def test_allowed_statuses_helper_agrees(self):
        from tools.signals import paper
        assert paper.allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_gate_definition_literal_on_disk(self):
        src = PAPER.read_text(encoding="utf-8")
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src

    def test_no_live_string_in_gate_assignment_anywhere(self):
        tree = ast.parse(PAPER.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = [getattr(t, "id", "") for t in node.targets]
                if "_PAPER_TRADE_SIGNAL_STATUSES" in targets:
                    call = node.value
                    assert isinstance(call, ast.Call)
                    args = [ast.literal_eval(a) for a in call.args]
                    assert "live" not in args
                    assert args == [{"paper_trading"}]

    def test_generate_paper_trade_signal_signature_untouched(self):
        from tools.backtest import BacktestEngine
        sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
        assert list(sig.parameters)[:1] == ["self"]

    def test_paper_module_never_mentions_widening_to_live(self):
        src = PAPER.read_text(encoding="utf-8")
        # The word live appears only inside the warning comment, never code.
        code_only = "\n".join(
            l for l in src.splitlines() if not l.lstrip().startswith("#")
        )
        assert '"live"' not in code_only
        assert "'live'" not in code_only


# ---------------------------------------------------------------------------
# Part 7 — cross-plane invariants (the characterization core)
# ---------------------------------------------------------------------------

class TestDualPlaneInvariants:
    def test_kernel_plane_is_live_path_for_complete(self):
        import inference
        assert hasattr(inference, "escalate_with_ladder")

    def test_unification_marker_absent(self, kernel_src):
        # No "unified" single-plane refactor markers allowed.
        assert "UNIFIED_PLANE" not in kernel_src
        assert "SINGLE_PLANE" not in kernel_src

    def test_latency_pin_blocks_sub10s_assumption(self, kernel_src):
        m = re.search(r"max\s*[≈=]\s*([\d.]+)s", kernel_src)
        if m:
            assert float(m.group(1)) > 20.0, "tail latency pin shrank?"

    def test_both_planes_reference_each_others_existence(self, kernel_src, router_src):
        # kernel documents the router plane...
        assert "inference_router" in kernel_src or "ProviderRouter" in kernel_src
        # and router's docstring acknowledges it is one of two planes.
        assert "TWO" in router_src[:1500].upper() or "kernel" in router_src[:1500]

    def test_openrouter_ox_absent_from_kernel_ladder(self, ladder_value):
        for rungs in ladder_value.values():
            for rung in rungs:
                assert rung["model"] != "openrouter_ox"
                assert "openrouter" not in rung["model"].lower()

    def test_gpu_endpoints_absent_from_kernel_ladder(self, ladder_value):
        for rungs in ladder_value.values():
            for rung in rungs:
                assert not rung["model"].startswith(("gpu", "localhost"))

    def test_hermes_mentioned_only_in_comments_of_kernel(self, kernel_src):
        code_lines = []
        for line in kernel_src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            code_lines.append(line)
        code_blob = "\n".join(code_lines)
        # hermes may appear only in strings that are comments-by-convention;
        # forbid it in actual identifiers/keys of code lines.
        assert "hermes_cli" not in code_blob

    def test_test_suite_itself_pins_duplication_deliberately(self):
        other = REPO / "tests" / "test_inference_planes.py"
        assert other.is_file()
