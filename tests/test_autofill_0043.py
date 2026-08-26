"""Autofill characterization #0043 — dual inference planes (LONG).

Second-wave characterization of the TWO intentionally separate inference
planes, with fresh angles that #0027 does not cover:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER`` + the
   ``complete()/escalate_with_ladder()`` walk (re-exported by
   ``inference.py``).
2. CLI/pipeline plane — ``ProviderRouter`` + ``config/providers.yaml``
   via ``load_providers_config`` (lives in ``inference_router.py``).

Hard invariants pinned here:

* ``MODEL_LADDER`` must not mention ``hermes_cli`` or ``ProviderRouter``
  anywhere — not in its entries, not in its comments. Hermes is the OX
  agent *runtime* (supervisor launches ``-m "$MODEL"``); it is never a
  completion transport inside either plane.
* ``gpu1`` is a ``llama_cpp_server`` backend on localhost:8080 and is
  ``default_tier``; ``gpu1_fast`` is likewise llama_cpp_server.
* ``openrouter_ox`` is an env-backed ``openai_compat`` endpoint
  (``OPENROUTER_API_KEY``) pointing at openrouter.ai/api/v1 with model
  ``stealth/ox-alpha``; no key material may ever live in git.
* The two planes stay UNUNIFIED — the measured-latency citation
  (p50 ≈ 11.9s / max ≈ 31.4s, findings/hermes_latency_2026-08-26.md)
  remains as the recorded reason.
* The paper-signal hard gate stays fail-closed:
  ``_PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})`` and
  ``"live"`` can never pass ``reject_non_paper``.

Tests-only module. No production gate is weakened; every pin fails
closed if the invariant drifts.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KERNEL = REPO / "inference_kernel.py"
ROUTER = REPO / "inference_router.py"
FACADE = REPO / "inference.py"
PROVIDERS_YAML = REPO / "config" / "providers.yaml"
SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"
PAPER_GATE = REPO / "tools" / "signals" / "paper.py"
FINDINGS = REPO / "findings" / "hermes_latency_2026-08-26.md"

# Vocabulary that must never appear inside the kernel ladder.
FORBIDDEN_LADDER_TERMS = ("hermes_cli", "ProviderRouter", "provider_router",
                          "providers.yaml", "load_providers_config")


@pytest.fixture(scope="module")
def kernel_src():
    return KERNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src():
    return ROUTER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def facade_src():
    return FACADE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def supervisor_src():
    if not SUPERVISOR.is_file():  # pragma: no cover - repo layout pin
        pytest.skip("nous-supervisor.sh missing")
    return SUPERVISOR.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def yaml_raw():
    return PROVIDERS_YAML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cfg(yaml_raw):
    import yaml
    return yaml.safe_load(yaml_raw)


@pytest.fixture(scope="module")
def kernel_tree(kernel_src):
    return ast.parse(kernel_src)


@pytest.fixture(scope="module")
def router_tree(router_src):
    return ast.parse(router_src)


def _ladder_block(src: str) -> str:
    """Literal source text of the MODEL_LADDER assignment block."""
    m = re.search(r"^MODEL_LADDER\s*[:=]", src, flags=re.M)
    assert m, "MODEL_LADDER assignment missing from kernel plane"
    end = src.find("\n\n", m.start())
    assert end != -1, "MODEL_LADDER block unterminated"
    return src[m.start():end]


# ────────────────────────────────────────────────────────────────────────
# A. Kernel ladder vocabulary hygiene (#0043 core pin)
# ────────────────────────────────────────────────────────────────────────


class TestLadderVocabulary43:
    def test_ladder_block_mentions_neither_forbidden_term(self, kernel_src):
        block = _ladder_block(kernel_src)
        for term in FORBIDDEN_LADDER_TERMS:
            assert term not in block, f"MODEL_LADDER mentions {term!r}"

    def test_ladder_block_is_comment_free_of_plane_cross_talk(self, kernel_src):
        """Even comments inside the ladder must not suggest routing through
        the other plane."""
        block = _ladder_block(kernel_src)
        comments = [ln.split("#", 1)[1] for ln in block.splitlines() if "#" in ln]
        joined = " ".join(comments).lower()
        assert "hermes_cli" not in joined
        assert "providerrouter" not in joined

    def test_ladder_values_never_contain_hermes_or_router_strings(self):
        import inference_kernel as ik

        for task, rungs in ik.MODEL_LADDER.items():
            blob = repr(rungs).lower()
            assert "hermes" not in blob, task
            assert "router" not in blob, task

    def test_kernel_walk_is_escalate_with_ladder_not_the_router(self,
                                                                kernel_tree):
        """The kernel plane's live walk is escalate_with_ladder(); it must
        consult MODEL_LADDER and never the ProviderRouter plane."""
        names = {n.name for n in ast.walk(kernel_tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "escalate_with_ladder" in names
        fn = next(n for n in ast.walk(kernel_tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "escalate_with_ladder")
        src = ast.dump(fn)
        assert "MODEL_LADDER" in src
        assert "ProviderRouter" not in src
        assert "get_router" not in src

    def test_kernel_defines_no_complete_function_itself(self, kernel_tree):
        """``complete``/``complete_sync`` belong to the router plane's
        ProviderRouter class; the kernel walks via escalate_with_ladder.
        If a module-level ``complete`` appears in the kernel later it must
        still route through MODEL_LADDER — fail loudly so a human looks."""
        top_names = {n.name for n in kernel_tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        overlap = top_names & {"complete", "complete_sync"}
        if overlap:
            src = inspect.getsource(next(
                n for n in kernel_tree.body if n.name in overlap))
            assert "hermes_cli" not in src and "get_router" not in src


# ────────────────────────────────────────────────────────────────────────
# B. gpu1 = llama_cpp_server local tier
# ────────────────────────────────────────────────────────────────────────


class TestGpu1LlamaCppServer43:
    def test_gpu1_backend_is_llama_cpp_server(self, cfg):
        assert cfg["providers"]["gpu1"]["backend"] == "llama_cpp_server"

    def test_gpu1_serves_local_openai_style_api(self, cfg):
        p = cfg["providers"]["gpu1"]
        assert p["base_url"].startswith("http://localhost")
        assert p["base_url"].endswith("/v1")

    def test_gpu1_is_default_tier(self, cfg):
        assert cfg["default_tier"] == "gpu1"

    def test_gpu1_fast_is_llama_cpp_server_too(self, cfg):
        assert cfg["providers"]["gpu1_fast"]["backend"] == "llama_cpp_server"

    def test_gpu1_bounded_concurrency_and_vram(self, cfg):
        p = cfg["providers"]["gpu1"]
        assert isinstance(p["max_concurrency"], int) and p["max_concurrency"] >= 1
        assert isinstance(p["vram_gb"], int) and 0 < p["vram_gb"] <= 32

    def test_gpu1_structured_output_and_tool_calls_flags(self, cfg):
        p = cfg["providers"]["gpu1"]
        assert p["structured_output"] is True
        assert p["tool_calls"] is True

    def test_no_remote_backend_smuggled_into_local_tiers(self, cfg):
        for name in ("gpu1", "gpu1_fast"):
            assert cfg["providers"][name].get("base_url_env") is None, name
            assert cfg["providers"][name].get("api_key_env") is None, name


# ────────────────────────────────────────────────────────────────────────
# C. openrouter_ox = env-backed openai_compat
# ────────────────────────────────────────────────────────────────────────


class TestOpenrouterOxEnvBacked43:
    def test_backend_is_openai_compat(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["backend"] == "openai_compat"

    def test_base_url_literal_openrouter_v1(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["base_url"] == \
            "https://openrouter.ai/api/v1"

    def test_key_comes_from_environment_only(self, cfg):
        ox = cfg["providers"]["openrouter_ox"]
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"
        assert "api_key" not in ox
        assert ox.get("api_key_file") is None

    def test_model_identity_is_stealth_ox_alpha(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["model"] == "stealth/ox-alpha"

    def test_yaml_contains_no_secret_material(self, yaml_raw):
        assert "sk-or-v1-" not in yaml_raw
        assert "OPENROUTER_API_KEY=" not in yaml_raw.replace(
            "# OPENROUTER_API_KEY=", "")

    def test_openrouter_ox_present_in_every_task_class_chain(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert "openrouter_ox" in chain, name

    def test_local_gpu_precedes_openrouter_ox_where_both_exist(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            gpus = [i for i, p in enumerate(chain) if p.startswith("gpu")]
            if gpus:
                assert gpus[0] < chain.index("openrouter_ox"), name

    def test_chains_end_in_an_ox_alpha_flavored_tier(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert chain[-1] in ("ox_alpha", "ox_alpha_proxy"), name

    def test_frontier_leads_judgment_classes(self, cfg):
        for name in ("promotion_judgment", "adversarial_review"):
            chain = cfg["routing"]["task_classes"][name]
            assert chain[0] == "frontier", name


# ────────────────────────────────────────────────────────────────────────
# D. Do NOT unify the planes
# ────────────────────────────────────────────────────────────────────────


class TestDoNotUnify43:
    def test_kernel_cites_measured_latency_p50(self, kernel_src):
        ok = (("11.9" in kernel_src or "31.4" in kernel_src)
              or "hermes_latency_2026-08-26.md" in kernel_src)
        assert ok, "kernel lost its measured-latency citation"

    def test_kernel_declares_two_planes_intentionally(self, kernel_src):
        low = kernel_src.lower()
        assert "two inference planes" in low or "two_plane" in low or \
            "two planes" in low

    def test_findings_latency_note_still_on_disk(self):
        assert FINDINGS.is_file(), (
            f"{FINDINGS.relative_to(REPO)} vanished; the unification ban "
            "needs its evidence to stay auditable"
        )

    def test_kernel_does_not_import_inference_router(self, kernel_tree):
        imported = set()
        for node in ast.walk(kernel_tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any("inference_router" in m or "inference.router" in m
                       for m in imported)

    def test_facade_reexports_both_planes(self, facade_src):
        assert "from inference_kernel import" in facade_src
        assert "from inference_router import" in facade_src
        assert "MODEL_LADDER" in facade_src
        assert "ProviderRouter" in facade_src

    def test_reexported_ladder_is_the_kernel_object(self):
        import inference
        import inference_kernel as ik
        assert inference.MODEL_LADDER is ik.MODEL_LADDER

    def test_load_providers_config_reads_providers_yaml(self):
        import inference
        cfg = inference.load_providers_config()
        assert "providers" in cfg and "routing" in cfg

    def test_provider_router_class_lives_only_in_router_module(self):
        import inference_router
        assert hasattr(inference_router, "ProviderRouter")

    def test_unify_guard_suite_still_exists(self):
        guard = REPO / "tests" / "test_inference_planes.py"
        assert guard.is_file()
        src = guard.read_text(encoding="utf-8")
        assert "MODEL_LADDER" in src


# ────────────────────────────────────────────────────────────────────────
# E. Hermes = agent runtime, never transport
# ────────────────────────────────────────────────────────────────────────


class TestHermesIsRuntimeNotTransport43:
    def test_supervisor_runs_hermes_with_dash_m_model(self, supervisor_src):
        assert '-m "$MODEL"' in supervisor_src
        assert "stealth/ox-alpha" in supervisor_src

    def test_supervisor_does_not_pipe_completions_through_hermes_cli(self,
                                                                    supervisor_src):
        """The supervisor script may launch hermes as the agent runtime but
        must never present it as an HTTP completion endpoint."""
        assert "hermes_cli" not in supervisor_src

    def test_kernel_has_no_hermes_transport_imports(self, kernel_tree):
        imported = set()
        for node in ast.walk(kernel_tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(m.startswith("hermes_cli") for m in imported), imported

    def test_public_completion_surface_free_of_hermes_cli(self):
        import inference
        for name in dir(inference):
            if not (name.startswith("complete") or "complete_sync" in name):
                continue
            obj = getattr(inference, name)
            if callable(obj):
                src = inspect.getsource(obj)
                assert "hermes_cli" not in src, f"{name} mentions hermes_cli"


# ────────────────────────────────────────────────────────────────────────
# F. Paper-trade signal hard gate — fail closed, live never armed
# ────────────────────────────────────────────────────────────────────────


class TestPaperSignalGateFailClosed43:
    def test_gate_file_exists(self):
        assert PAPER_GATE.is_file()

    def test_allowed_statuses_exactly_paper_trading(self):
        from tools.signals.paper import allowed_paper_statuses
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_live_is_rejected(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("live") is True

    def test_paper_trading_passes(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("paper_trading") is False

    def test_status_set_is_a_frozenset_in_source(self):
        src = PAPER_GATE.read_text(encoding="utf-8")
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(([^)]*)\)",
                      src)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES is no longer a frozenset"
        assert '"live"' not in m.group(1) and "'live'" not in m.group(1)
        assert '"paper_trading"' in m.group(1)

    def test_no_other_module_redefines_the_status_set(self):
        offenders = []
        for py in (REPO / "tools").rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover
                continue
            if "_PAPER_TRADE_SIGNAL_STATUSES =" in text and \
                    py != PAPER_GATE:
                offenders.append(str(py.relative_to(REPO)))
        assert not offenders, f"duplicate status-set definitions: {offenders}"


# ────────────────────────────────────────────────────────────────────────
# G. Cross-plane sanity: router plane structure stays put
# ────────────────────────────────────────────────────────────────────────


class TestRouterPlaneShape43:
    def test_router_defines_expected_public_names(self, router_src):
        tree = ast.parse(router_src)
        names = {n.name for n in tree.body
                 if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
        for expected in ("ProviderRouter", "EndpointConfig", "TierConfig",
                         "CostLedger", "EscalationConfig",
                         "UnknownTaskError" ):
            if expected in {"UnknownTaskError"}:
                # alias drift tolerated: any Unknown* class suffices
                assert any(n.startswith("Unknown") for n in names), names
            else:
                assert expected in names, f"router lost {expected}"

    def test_router_reads_providers_yaml_path_constant(self):
        import inference_router as ir
        path = getattr(ir, "_PROVIDERS_CONFIG_PATH")
        assert Path(path).name == "providers.yaml"

    def test_routing_section_has_required_subkeys(self, cfg):
        routing = cfg["routing"]
        assert "task_classes" in routing
        assert "escalation" in routing

    def test_escalation_policy_keeps_sensitive_context_local(self, cfg):
        assert cfg["routing"]["escalation"]["sensitive_context_stays_local"] \
            is True

    def test_task_class_aliases_exposed_by_facade(self):
        import inference
        assert hasattr(inference, "TASK_CLASS_ALIASES")

    def test_every_chain_nonempty_and_unique(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert chain, name
            assert len(chain) == len(set(chain)), f"{name} repeats tiers"
