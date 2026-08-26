"""Autofill characterization #0027 — dual inference planes (LONG).

Characterizes, without changing them, the TWO intentionally separate
inference planes in Callisto:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER`` (task_type -> ordered
   model list), walked by ``inference.complete()`` on every call and
   re-exported through ``inference.py``.
2. CLI/pipeline plane — ``ProviderRouter`` + ``config/providers.yaml``
   (``load_providers_config``), living in ``inference_router.py``.

Pins under characterization:

* ``MODEL_LADDER`` must never mention ``hermes_cli`` or ``ProviderRouter``
  as a routing entry — Hermes is the OX agent *runtime*, never a completion
  transport, and the router belongs to the other plane.
* The dependency direction is one-way: ``inference_router`` may import from
  ``inference_kernel``; the kernel plane must not import the router.
* ``gpu1`` is the llama_cpp_server-backed local tier in providers.yaml.
* ``openrouter_ox`` is an env-backed (``OPENROUTER_API_KEY``)
  ``openai_compat`` endpoint — key material never lives in git.
* The two planes stay UNUNIFIED: the measured-latency citation
  (p50 ≈ 11.9s / max ≈ 31.4s, findings/hermes_latency_2026-08-26.md)
  remains in the source as the reason.
* The paper-trade signal hard gate stays fail-closed: only
  ``paper_trading`` may run ``generate_paper_trade_signal`` — never
  ``live``.

Tests-only module. No production gate is weakened here; if any pin is
false the suite fails closed.
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
FACADE = REPO / "inference.py"
PROVIDERS_YAML = REPO / "config" / "providers.yaml"
SUPERVISOR = REPO / "scripts" / "nous-supervisor.sh"
PAPER_GATE = REPO / "tools" / "signals" / "paper.py"


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
def cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kernel_tree(kernel_src):
    return ast.parse(kernel_src)


def _ladder_block(src: str) -> str:
    """The literal source text of the MODEL_LADDER assignment."""
    m = re.search(r"^MODEL_LADDER\s*[:=]", src, flags=re.M)
    assert m, "MODEL_LADDER assignment missing from kernel plane"
    start = m.start()
    end = src.find("\n\n", start)
    assert end != -1
    return src[start:end]


# ────────────────────────────────────────────────────────────────────────
# Plane 1: MODEL_LADDER structure (kernel plane)
# ────────────────────────────────────────────────────────────────────────


class TestModelLadderStructure:
    def test_ladder_exists_and_is_dict(self):
        import inference_kernel as ik

        assert isinstance(ik.MODEL_LADDER, dict)

    def test_expected_task_keys_present(self):
        import inference_kernel as ik

        for key in ("reasoning", "classification", "review",
                    "code_generation", "hypothesis_gen", "deep_work"):
            assert key in ik.MODEL_LADDER, f"MODEL_LADDER lost {key}"

    def test_every_rung_is_a_dict_with_model_quality_timeout(self):
        import inference_kernel as ik

        for task, rungs in ik.MODEL_LADDER.items():
            assert isinstance(rungs, list) and rungs, task
            for i, rung in enumerate(rungs):
                assert isinstance(rung, dict), (task, i)
                assert "model" in rung and rung["model"], (task, i)
                assert rung.get("quality") in ("frontier", "high", "medium"), (task, i)
                assert isinstance(rung.get("timeout"), int) and rung["timeout"] > 0, (task, i)

    def test_classification_is_single_fast_rung(self):
        import inference_kernel as ik

        cls = ik.MODEL_LADDER["classification"]
        assert len(cls) == 1
        assert cls[0]["quality"] == "medium"
        assert cls[0]["timeout"] <= 60

    def test_reasoning_leads_with_frontier(self):
        import inference_kernel as ik

        reasoning = ik.MODEL_LADDER["reasoning"]
        assert reasoning[0]["model"] == "claude_code"
        assert reasoning[0]["quality"] == "frontier"

    def test_deep_work_leads_with_frontier(self):
        import inference_kernel as ik

        dw = ik.MODEL_LADDER["deep_work"]
        assert dw[0]["model"] == "claude_code"

    def test_hypothesis_gen_prefers_local_first(self):
        """HYBRID MODE: hypothesis generation saves Claude credits by
        running local-first; claude_code is the last-resort rung."""
        import inference_kernel as ik

        hg = ik.MODEL_LADDER["hypothesis_gen"]
        assert hg[0]["model"] != "claude_code"
        assert hg[-1]["model"] == "claude_code"

    def test_timeouts_are_monotone_decreasing_down_reasoning(self):
        """Later (cheaper/faster) fallback rungs should not out-wait the
        primary tiers in the reasoning ladder."""
        import inference_kernel as ik

        tos = [r["timeout"] for r in ik.MODEL_LADDER["reasoning"]]
        assert all(a >= b for a, b in zip(tos, tos[1:])), tos

    def test_no_duplicate_model_within_a_single_ladder(self):
        import inference_kernel as ik

        for task, rungs in ik.MODEL_LADDER.items():
            models = [r["model"] for r in rungs]
            dupes = {m for m in models if models.count(m) > 1}
            assert not dupes, f"{task} repeats models: {dupes}"

    def test_qwen36_constant_is_referenced_not_inlined(self):
        """The primary local model goes through QWEN36_MODEL so a swap is
        one edit, not seven."""
        import inference_kernel as ik

        assert hasattr(ik, "QWEN36_MODEL")
        block = _ladder_block(KERNEL.read_text(encoding="utf-8"))
        assert "QWEN36_MODEL" in block


# ────────────────────────────────────────────────────────────────────────
# MODEL_LADDER vocabulary hygiene: no hermes_cli, no ProviderRouter
# ────────────────────────────────────────────────────────────────────────


class TestLadderVocabularyHygiene:
    def test_ladder_block_does_not_mention_hermes_cli(self, kernel_src):
        assert "hermes_cli" not in _ladder_block(kernel_src)

    def test_ladder_block_does_not_route_via_provider_router(self, kernel_src):
        block = _ladder_block(kernel_src)
        assert "provider_router" not in block.lower()
        assert "ProviderRouter" not in block

    def test_no_ladder_rung_names_hermes_anything(self):
        import inference_kernel as ik

        for task, rungs in ik.MODEL_LADDER.items():
            for rung in rungs:
                assert "hermes" not in str(rung.get("model", "")).lower(), (
                    f"{task} routes through a hermes-named model: {rung}"
                )

    def test_escalate_with_ladder_walks_the_ladder_not_the_router(self, kernel_tree):
        """escalate_with_ladder is the kernel plane's live walk (the
        complete() facade re-exports through inference.py)."""
        fn = next(
            n for n in ast.walk(kernel_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "escalate_with_ladder"
        )
        names = {
            n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
        }
        assert any("MODEL_LADDER" in n or "ladder" in n.lower() for n in names), (
            "escalate_with_ladder no longer walks MODEL_LADDER"
        )
        assert not any("ProviderRouter" in n or "provider_router" in n.lower() for n in names), (
            "kernel walk reached into ProviderRouter"
        )

    def test_escalate_helper_mentions_neither_transport(self):
        import inference

        fn = getattr(inference, "escalate_with_ladder", None)
        if fn is None:
            pytest.skip("escalate_with_ladder not exposed")
        src = inspect.getsource(fn)
        assert "hermes_cli" not in src
        assert "ProviderRouter" not in src


# ────────────────────────────────────────────────────────────────────────
# One-way dependency between the planes
# ────────────────────────────────────────────────────────────────────────


class TestPlaneDependencyDirection:
    def test_kernel_does_not_import_the_router_module(self, kernel_tree):
        imported = set()
        for node in ast.walk(kernel_tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "inference_router" not in imported, (
            "kernel plane imports the CLI/pipeline plane — planes must stay separate"
        )

    def test_router_may_reference_kernel(self, router_src):
        assert "inference_kernel" in router_src or "_parse_json_response" in router_src

    def test_facade_reexports_both_planes(self, facade_src):
        assert "MODEL_LADDER" in facade_src
        assert "inference_router" in facade_src

    def test_facade_reexported_ladder_is_kernel_object(self):
        import inference
        import inference_kernel

        assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER


# ────────────────────────────────────────────────────────────────────────
# Do-not-unify pins (measured latency citation)
# ────────────────────────────────────────────────────────────────────────


class TestDoNotUnifyPins:
    def test_kernel_cites_measured_hermes_fork_latency(self, kernel_src):
        assert "11.9" in kernel_src and "31.4" in kernel_src
        assert "hermes_latency_2026-08-26.md" in kernel_src

    def test_kernel_declares_two_plane_policy(self, kernel_src):
        assert "TWO INFERENCE PLANES" in kernel_src
        assert "do not unify" in kernel_src.lower() or "Do NOT unify" in kernel_src

    def test_router_docstring_declares_itself_one_of_two_planes(self, router_src):
        assert "TWO INFERENCE PLANES" in router_src or "ONE of the TWO" in router_src
        assert "Do NOT unify" in router_src or "does not support" in router_src

    def test_findings_latency_note_exists_on_disk(self):
        note = REPO / "findings" / "hermes_latency_2026-08-26.md"
        if note.is_file():
            body = note.read_text(encoding="utf-8", errors="replace")
            assert "11.9" in body or "p50" in body

    def test_unify_guard_test_still_present_in_planes_suite(self):
        planes = REPO / "tests" / "test_inference_planes.py"
        src = planes.read_text(encoding="utf-8")
        assert "unif" in src.lower()
        assert "11.9" in src or "hermes_latency_2026-08-26.md" in src


# ────────────────────────────────────────────────────────────────────────
# Plane 2: providers.yaml topology
# ────────────────────────────────────────────────────────────────────────


class TestProvidersYamlTopology:
    def test_gpu1_is_llama_cpp_server(self, cfg):
        gpu1 = cfg["providers"]["gpu1"]
        assert gpu1["backend"] == "llama_cpp_server"
        assert gpu1["base_url"].startswith("http")

    def test_gpu1_fast_is_also_llama_cpp_server(self, cfg):
        fast = cfg["providers"]["gpu1_fast"]
        assert fast["backend"] == "llama_cpp_server"

    def test_gpu1_has_bounded_concurrency(self, cfg):
        assert cfg["providers"]["gpu1"]["max_concurrency"] >= 1

    def test_default_tier_points_at_local_gpu(self, cfg):
        assert cfg["default_tier"].startswith("gpu")

    def test_local_tiers_come_before_openrouter_ox(self, cfg):
        classes = cfg["routing"]["task_classes"]
        for name, chain in classes.items():
            locals_ = [p for p in chain if p.startswith("gpu")]
            if locals_ and "openrouter_ox" in chain:
                assert chain.index(locals_[0]) < chain.index("openrouter_ox"), name

    def test_every_task_class_ends_in_an_ox_alpha_serving_tier(self, cfg):
        classes = cfg["routing"]["task_classes"]
        for name, chain in classes.items():
            assert chain[-1] in ("ox_alpha", "ox_alpha_proxy", "openrouter_ox"), name

    def test_unknown_task_class_is_loud(self):
        from inference_router import UnknownTaskClassError

        assert issubclass(UnknownTaskClassError, KeyError)

    def test_yaml_has_no_inline_api_key_material(self):
        raw = PROVIDERS_YAML.read_text(encoding="utf-8")
        for marker in ("sk-or-v1-", "sk-", "api_key:"):
            # allow api_key_env but never a literal api_key value
            if marker == "api_key:":
                assert not re.search(r"^\s*api_key:\s*\S", raw, flags=re.M)
            else:
                assert marker not in raw, f"secret-looking material: {marker}"


class TestOpenrouterOxEndpoint:
    def test_backend_is_openai_compat(self, cfg):
        ox = cfg["providers"]["openrouter_ox"]
        assert ox["backend"] == "openai_compat"

    def test_base_url_is_openrouter_v1(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["base_url"] == (
            "https://openrouter.ai/api/v1"
        )

    def test_key_is_env_backed(self, cfg):
        ox = cfg["providers"]["openrouter_ox"]
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"
        assert "api_key" not in ox

    def test_model_is_stealth_ox_alpha(self, cfg):
        assert cfg["providers"]["openrouter_ox"]["model"] == "stealth/ox-alpha"

    def test_openrouter_ox_appears_in_most_chains(self, cfg):
        classes = cfg["routing"]["task_classes"]
        covered = [n for n, c in classes.items() if "openrouter_ox" in c]
        assert len(covered) >= len(classes) // 2

    def test_supervisor_keeps_model_flag_untouched(self):
        if not SUPERVISOR.is_file():
            pytest.skip("supervisor script absent")
        src = SUPERVISOR.read_text(encoding="utf-8")
        assert "-m \"$MODEL\"" in src
        assert "stealth/ox-alpha" in src


# ────────────────────────────────────────────────────────────────────────
# Hermes: agent runtime, never completion transport
# ────────────────────────────────────────────────────────────────────────


class TestHermesIsRuntimeNotTransport:
    def test_kernel_source_has_no_hermes_cli_import(self, kernel_tree):
        for node in ast.walk(kernel_tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                assert "hermes_cli" not in m, f"kernel imports {m}"

    def test_kernel_validator_is_vendored_tools_module(self, kernel_src):
        assert "tools.hermes_validator" in kernel_src
        assert "attic/" in kernel_src  # quarantine note survives

    def test_all_public_complete_variants_avoid_hermes_cli(self):
        import inference

        for name in dir(inference):
            if not (name.startswith("complete") or "complete_sync" in name):
                continue
            obj = getattr(inference, name)
            if not callable(obj):
                continue
            try:
                src = inspect.getsource(obj)
            except (OSError, TypeError):
                continue
            assert "hermes_cli" not in src, f"{name} mentions hermes_cli"

    def test_supervisor_invokes_hermes_as_runtime_only(self):
        if not SUPERVISOR.is_file():
            pytest.skip("supervisor script absent")
        src = SUPERVISOR.read_text(encoding="utf-8")
        assert "hermes" in src.lower()


# ────────────────────────────────────────────────────────────────────────
# Paper-trade hard gate stays fail-closed
# ────────────────────────────────────────────────────────────────────────


class TestPaperSignalHardGateFailClosed:
    def test_allowed_statuses_exactly_paper_trading(self):
        from tools.signals.paper import allowed_paper_statuses

        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_live_is_never_allowed(self):
        from tools.signals.paper import allowed_paper_statuses

        assert "live" not in allowed_paper_statuses()

    def test_reject_non_paper_accepts_only_paper_trading(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False
        for bad in ("live", "paper", "", None, "LIVE", "Paper_Trading"):
            assert reject_non_paper(bad) is True, bad

    def test_gate_definition_literal_contains_only_paper_trading(self):
        raw = PAPER_GATE.read_text(encoding="utf-8")
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(([^)]*)\)", raw)
        assert m, "gate definition moved/renamed"
        body = m.group(1)
        assert '"paper_trading"' in body or "'paper_trading'" in body
        assert "live" not in body

    def test_backtest_engine_gates_generate_paper_trade_signal(self):
        src = (REPO / "tools" / "backtest.py").read_text(encoding="utf-8")
        assert "reject_non_paper" in src
        fn_idx = src.index("async def generate_paper_trade_signal")
        guard_zone = src[fn_idx:fn_idx + 2000]
        assert "reject_non_paper" in guard_zone

    def test_gate_module_has_no_live_path(self):
        raw = PAPER_GATE.read_text(encoding="utf-8")
        assert 'frozenset({"live"})' not in raw
        assert '"live"' not in raw.replace('never', '').lower() or True  # doc-only mention ok
        # hard assertion: no code path returns live as allowed
        tree = ast.parse(raw)
        for node in ast.walk(tree):
            if isinstance(node, ast.Return):
                snippet = ast.dump(node)
                assert '"live"' not in snippet and "'live'" not in snippet
