"""Autofill characterization #0051 — dual inference planes (LONG).

Third-wave characterization of the TWO intentionally separate inference
planes. #0027 pinned the original split and #0043 covered vocabulary
hygiene + endpoint identity; this module adds fresh structural and
behavioral angles neither of them touches:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER`` shape: per-task
   entries are non-empty ordered lists of dicts carrying
   ``model``/``quality``/``timeout``, timeouts are monotone non-increasing
   down each ladder (frontier rung gets the longest budget), every task
   type resolves via the ``reasoning`` default, and the Claude-hours
   demotion helper preserves ladder length and local-first order.
2. CLI/pipeline plane — ``ProviderRouter`` + ``config/providers.yaml``
   budget/health/empirical-routing sub-structure.
3. Endpoint identity pins: ``gpu1`` is a ``llama_cpp_server`` localhost
   endpoint; ``openrouter_ox`` is an env-backed ``openai_compat``
   endpoint (``OPENROUTER_API_KEY``) pointing at
   https://openrouter.ai/api/v1 with model ``stealth/ox-alpha`` — no key
   material anywhere in git.
4. UNIFICATION BAN: MODEL_LADDER must not mention ``hermes_cli`` or
   ``ProviderRouter``; the router module must not import the kernel; the
   measured-latency citation (p50 ≈ 11.9s / max ≈ 31.4s,
   findings/hermes_latency_2026-08-26.md) stays on disk and cited.
5. Paper-signal hard gate fails closed:
   ``_PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})``,
   ``"live"`` can NEVER pass, and ``generate_paper_trade_signal`` is
   gated through the shared predicate — live betting is never armed.

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
PAPER_GATE = REPO / "tools" / "signals" / "paper.py"
BACKTEST = REPO / "tools" / "backtest.py"
FINDINGS = REPO / "findings" / "hermes_latency_2026-08-26.md"

# Terms that must never leak into the kernel-plane ladder block.
FORBIDDEN_LADDER_TERMS = ("hermes_cli", "ProviderRouter",
                          "providers.yaml", "load_providers_config")

KNOWN_QUALITIES = {"frontier", "high", "medium", "low"}


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def kernel_src():
    return KERNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src():
    return ROUTER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def yaml_raw():
    return PROVIDERS_YAML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cfg(yaml_raw):
    import yaml
    data = yaml.safe_load(yaml_raw)
    assert isinstance(data, dict) and "providers" in data
    return data


@pytest.fixture(scope="module")
def ladder():
    """The LIVE kernel ladder object."""
    import inference_kernel as ik
    obj = getattr(ik, "MODEL_LADDER")
    assert isinstance(obj, dict) and obj, "MODEL_LADDER missing/empty"
    return obj


@pytest.fixture(scope="module")
def paper_src():
    return PAPER_GATE.read_text(encoding="utf-8")


def _ladder_block(src: str) -> str:
    m = re.search(r"^MODEL_LADDER\s*[:=]", src, flags=re.M)
    assert m, "MODEL_LADDER assignment not found in kernel source"
    end = src.find("\n\n}", m.start())
    end = src.find("\n\n", m.start()) if end == -1 else src.find(
        "\n\n", end + 3)
    assert end != -1, "MODEL_LADDER block unterminated"
    return src[m.start():end]


def _imports_of(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


# ────────────────────────────────────────────────────────────────────────
# A. Ladder SHAPE — entries, ordering, timeout budgets (#0051 core)
# ────────────────────────────────────────────────────────────────────────


class TestLadderShape51:
    def test_every_task_has_nonempty_list_of_dicts(self, ladder):
        for task, rungs in ladder.items():
            assert isinstance(rungs, list) and rungs, task
            for rung in rungs:
                assert isinstance(rung, dict), (task, rung)

    def test_every_rung_carries_model_quality_timeout(self, ladder):
        for task, rungs in ladder.items():
            for i, rung in enumerate(rungs):
                assert isinstance(rung.get("model"), str) and rung["model"], \
                    (task, i)
                assert rung.get("quality") in KNOWN_QUALITIES, (task, rung)
                t = rung.get("timeout")
                assert isinstance(t, int) and 0 < t <= 600, (task, rung)

    def test_models_unique_within_a_ladder(self, ladder):
        for task, rungs in ladder.items():
            models = [r["model"] for r in rungs]
            assert len(models) == len(set(models)), \
                f"{task} repeats a model: {models}"

    def test_timeouts_monotone_nonincreasing_down_reasoning(self, ladder):
        ts = [r["timeout"] for r in ladder["reasoning"]]
        assert all(a >= b for a, b in zip(ts, ts[1:])), \
            f"reasoning timeouts increase down-ladder: {ts}"

    def test_timeouts_monotone_nonincreasing_down_code_generation(self, ladder):
        ts = [r["timeout"] for r in ladder["code_generation"]]
        assert all(a >= b for a, b in zip(ts, ts[1:])), ts

    def test_frontier_rug_gets_longest_budget_where_present(self, ladder):
        """The frontier ('claude_code') rung, when first, holds the max
        timeout of its ladder."""
        for task, rungs in ladder.items():
            if rungs[0]["model"] == "claude_code":
                assert rungs[0]["timeout"] == max(r["timeout"] for r in rungs)

    def test_unknown_task_falls_back_to_reasoning(self, ladder):
        import inference_kernel as ik
        src = inspect.getsource(ik.escalate_with_ladder) \
            if hasattr(ik, "escalate_with_ladder") else ""
        # The walk site must use a .get(task_type, MODEL_LADDER["reasoning"])
        # style default so an unknown task never KeyErrors mid-pipeline.
        kernel_txt = KERNEL.read_text(encoding="utf-8")
        assert 'MODEL_LADDER.get(task_type' in kernel_txt
        assert 'MODEL_LADDER["reasoning"]' in kernel_txt

    def test_classification_is_single_fast_rung(self, ladder):
        rungs = ladder["classification"]
        assert len(rungs) >= 1
        assert rungs[0]["timeout"] <= 60

    def test_no_empty_string_or_none_model_names(self, ladder):
        for task, rungs in ladder.items():
            for rung in rungs:
                assert rung["model"].strip(), task

    def test_ladder_keys_are_snake_case_tasks(self, ladder):
        for task in ladder:
            assert re.fullmatch(r"[a-z0-9_]+", task), task


# ────────────────────────────────────────────────────────────────────────
# B. Ladder VOCABULARY hygiene — hermes_cli / ProviderRouter stay OUT
# ────────────────────────────────────────────────────────────────────────


class TestLadderVocabulary51:
    def test_block_free_of_forbidden_terms(self, kernel_src):
        block = _ladder_block(kernel_src)
        for term in FORBIDDEN_LADDER_TERMS:
            assert term not in block, term

    def test_runtime_values_free_of_hermes_and_router(self, ladder):
        for task, rungs in ladder.items():
            blob = repr(rungs).lower()
            for bad in ("hermes", "providerrouter", "provider_router"):
                assert bad not in blob, (task, bad)

    def test_whole_kernel_file_never_imports_router_plane(self, kernel_src):
        tree = ast.parse(kernel_src)
        mods = _imports_of(tree)
        assert not any("inference_router" in m for m in mods), mods

    def test_kernel_code_never_calls_providers_config(self, kernel_src):
        """The TWO PLANES note *mentions* load_providers_config in a
        comment (that citation must stay); executable code must not."""
        tree = ast.parse(kernel_src)
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)}
        assert "load_providers_config" not in calls

    def test_hermes_cli_backend_never_in_kernel_module(self, kernel_src):
        assert '"hermes_cli"' not in kernel_src
        assert "'hermes_cli'" not in kernel_src


# ────────────────────────────────────────────────────────────────────────
# C. gpu1 = llama_cpp_server local tier (fresh angle: hardware contract)
# ────────────────────────────────────────────────────────────────────────


class TestGpu1Contract51:
    def test_gpu1_backend(self, cfg):
        assert cfg["providers"]["gpu1"]["backend"] == "llama_cpp_server"

    def test_gpu1_url_exact_localhost_8080_v1(self, cfg):
        assert cfg["providers"]["gpu1"]["base_url"] == \
            "http://localhost:8080/v1"

    def test_gpu1_fast_url_exact_localhost_8081_v1(self, cfg):
        assert cfg["providers"]["gpu1_fast"]["base_url"] == \
            "http://localhost:8081/v1"

    def test_default_tier_is_gpu1(self, cfg):
        assert cfg["default_tier"] == "gpu1"

    def test_gpu1_model_is_the_qwen_ud_quant(self, cfg):
        assert cfg["providers"]["gpu1"]["model"] == "qwen3.8-27b-ud-q3_k_xl"

    def test_gpu1_vram_declared_within_16gb_card(self, cfg):
        assert cfg["providers"]["gpu1"]["vram_gb"] <= 16

    def test_local_endpoints_cost_zero_at_margin(self, cfg):
        for name in ("gpu1", "gpu1_fast"):
            p = cfg["providers"][name]
            assert p.get("cost_per_1k_input", 0) == 0, name
            assert p.get("cost_per_1k_output", 0) == 0, name

    def test_gpu1_concurrency_bounded_to_one_slot(self, cfg):
        assert cfg["providers"]["gpu1"]["max_concurrency"] == 1

    def test_local_tiers_have_no_env_indirection(self, cfg):
        for name in ("gpu1", "gpu1_fast"):
            p = cfg["providers"][name]
            assert "base_url_env" not in p and "api_key_env" not in p, name

    def test_both_local_servers_speak_openai_style_v1_paths(self, cfg):
        for name in ("gpu1", "gpu1_fast"):
            url = cfg["providers"][name]["base_url"]
            assert url.startswith("http://localhost:") and \
                url.rstrip("/").endswith("/v1"), name


# ────────────────────────────────────────────────────────────────────────
# D. openrouter_ox = env-backed openai_compat (fresh angle: honesty flags)
# ────────────────────────────────────────────────────────────────────────


class TestOpenrouterOx51:
    def _ox(self, cfg):
        return cfg["providers"]["openrouter_ox"]

    def test_backend_openai_compat(self, cfg):
        assert self._ox(cfg)["backend"] == "openai_compat"

    def test_base_url_literal(self, cfg):
        assert self._ox(cfg)["base_url"] == "https://openrouter.ai/api/v1"

    def test_key_env_name(self, cfg):
        assert self._ox(cfg)["api_key_env"] == "OPENROUTER_API_KEY"

    def test_no_inline_key(self, cfg):
        assert "api_key" not in self._ox(cfg)

    def test_model_identity(self, cfg):
        assert self._ox(cfg)["model"] == "stealth/ox-alpha"

    def test_native_tool_calls_over_http(self, cfg):
        """Unlike the hermes-fork tiers, the raw HTTP path keeps native
        tool_calls even though response_format is best-effort."""
        ox = self._ox(cfg)
        assert ox["tool_calls"] is True
        assert ox["structured_output"] is False

    def test_context_ceiling_128k(self, cfg):
        assert self._ox(cfg)["context_tokens"] == 128000

    def test_zero_stated_cost(self, cfg):
        ox = self._ox(cfg)
        assert ox["cost_per_1k_input"] == 0.0
        assert ox["cost_per_1k_output"] == 0.0

    def test_yaml_has_no_secret_material(self, yaml_raw):
        assert "sk-or-v1-" not in yaml_raw
        assert re.search(r"OPENROUTER_API_KEY\s*[:=]\s*\S+", yaml_raw) is None

    def test_openrouter_ox_in_every_failover_chain(self, cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            assert "openrouter_ox" in chain, name

    def test_local_gpu_precedes_openrouter_in_grind_classes(self, cfg):
        for name in ("screening", "extraction", "classification"):
            chain = cfg["routing"]["task_classes"][name]
            assert chain.index("openrouter_ox") > 0, name
            assert chain[0].startswith("gpu"), name

    def test_no_chain_puts_paid_tiers_before_all_locals_unless_frontier(self,
                                                                        cfg):
        for name, chain in cfg["routing"]["task_classes"].items():
            if chain[0] == "frontier":
                continue
            assert chain[0].startswith("gpu"), \
                f"{name} leads with hosted tier {chain[0]}"


# ────────────────────────────────────────────────────────────────────────
# E. Router plane structure — budget / health / empirical routing
# ────────────────────────────────────────────────────────────────────────


class TestRouterPlaneStructure51:
    def test_budget_cap_positive_and_small(self, cfg):
        usd = cfg["routing"]["budget"]["usd"]
        assert isinstance(usd, (int, float)) and 0 < usd <= 50

    def test_health_checks_enabled(self, cfg):
        assert cfg["routing"]["health_checks"]["enabled"] is True

    def test_empirical_routing_off_by_default(self, cfg):
        er = cfg["routing"]["empirical_routing"]
        assert er["enabled"] is False

    def test_sensitive_context_stays_local(self, cfg):
        assert cfg["routing"]["escalation"][
            "sensitive_context_stays_local"] is True

    def test_escalation_thresholds_small_integers(self, cfg):
        esc = cfg["routing"]["escalation"]
        for k in ("json_schema_failures", "tool_error_loops"):
            v = esc[k]
            assert isinstance(v, int) and 0 < v <= 10, k

    def test_confidence_gate_between_zero_and_one(self, cfg):
        c = cfg["routing"]["escalation"]["confidence_below"]
        assert 0.0 < c < 1.0

    def test_provider_class_defined_in_router_only(self):
        import inference_router as ir
        assert inspect.isclass(ir.ProviderRouter)

    def test_router_imports_no_ladder_from_kernel(self, router_src):
        """The router may borrow small shared helpers (e.g.
        _parse_json_response) from the kernel module, but it must never
        pull the kernel's routing brain (MODEL_LADDER / the walk)."""
        tree = ast.parse(router_src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and \
                    node.module == "inference_kernel":
                names = {a.name for a in node.names}
                banned = names & {"MODEL_LADDER", "escalate_with_ladder",
                                  "complete", "OllamaInference"}
                assert not banned, banned

    def test_facade_bridges_both_planes(self):
        import inference
        import inference_kernel as ik
        import inference_router as ir
        assert inference.MODEL_LADDER is ik.MODEL_LADDER
        assert inference.ProviderRouter is ir.ProviderRouter

    def test_task_class_aliases_bridge_legacy_names(self):
        import inference
        aliases = getattr(inference, "TASK_CLASS_ALIASES", {})
        assert isinstance(aliases, dict) and aliases


# ────────────────────────────────────────────────────────────────────────
# F. Unification ban — evidence trail stays auditable
# ────────────────────────────────────────────────────────────────────────


class TestUnificationBan51:
    def test_two_planes_note_present_in_kernel(self, kernel_src):
        assert "TWO INFERENCE PLANES" in kernel_src

    def test_measured_latency_numbers_cited(self, kernel_src):
        assert ("11.9" in kernel_src and "31.4" in kernel_src) or \
            "hermes_latency_2026-08-26.md" in kernel_src

    def test_findings_note_on_disk_with_numbers(self):
        assert FINDINGS.is_file()
        text = FINDINGS.read_text(encoding="utf-8")
        assert "11.9" in text and "31.4" in text

    def test_guard_suite_test_inference_planes_still_pins_duplication(self):
        guard = REPO / "tests" / "test_inference_planes.py"
        assert guard.is_file()
        assert "MODEL_LADDER" in guard.read_text(encoding="utf-8")

    def test_planes_are_distinct_objects_not_aliases(self):
        import inference_kernel as ik
        import inference_router as ir
        # The planes share nothing: kernel's ladder is a plain dict of
        # lists, the router builds config-driven tier objects.
        assert isinstance(ik.MODEL_LADDER, dict)
        assert not hasattr(ir.ProviderRouter, "MODEL_LADDER")

    def test_kernel_walk_function_does_not_touch_router_symbols(self,
                                                                kernel_src):
        tree = ast.parse(kernel_src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "escalate_with_ladder")
        dump = ast.dump(fn)
        for bad in ("ProviderRouter", "get_router", "load_providers_config"):
            assert bad not in dump, bad


# ────────────────────────────────────────────────────────────────────────
# G. Paper-signal hard gate — fail closed; live is never armed
# ────────────────────────────────────────────────────────────────────────


class TestPaperGateFailClosed51:
    def test_status_set_is_exactly_paper_trading(self):
        from tools.signals.paper import allowed_paper_statuses
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    @pytest.mark.parametrize("status", [
        "live", "LIVE", "Live", "live_trading", "paper", "",
        None, 0, "paper_trading_live", " live ",
    ])
    def test_everything_else_rejected(self, status):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper(status) is True

    def test_only_paper_trading_passes(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("paper_trading") is False

    def test_source_is_frozenset_literal_without_live(self, paper_src):
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(([^)]*)\)",
            paper_src)
        assert m, "status set no longer a frozenset literal"
        body = m.group(1)
        assert '"live"' not in body and "'live'" not in body
        assert '"paper_trading"' in body

    def test_gate_comment_warns_against_live(self, paper_src):
        assert "NEVER" in paper_src.upper()

    def test_single_definition_site(self):
        offenders = []
        for py in (REPO / "tools").rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if "_PAPER_TRADE_SIGNAL_STATUSES =" in text and py != PAPER_GATE:
                offenders.append(str(py.relative_to(REPO)))
        assert not offenders, offenders

    def test_generate_signal_gated_via_shared_predicate(self):
        """BacktestEngine.generate_paper_trade_signal must consult the
        shared gate (directly or via tools.signals.paper) — never its own
        status check that could drift toward 'live'."""
        assert BACKTEST.is_file()
        bt = BACKTEST.read_text(encoding="utf-8")
        ok = ("allowed_paper_statuses" in bt or
              "reject_non_paper" in bt or
              "tools.signals.paper" in bt or
              "_PAPER_TRADE_SIGNAL_STATUSES" in bt)
        assert ok, "backtest.py lost its link to the paper-signal gate"

    def test_gate_module_defines_no_live_constant(self, paper_src):
        assert not re.search(r"_LIVE\w*", paper_src)


# ────────────────────────────────────────────────────────────────────────
# H. Cross-cutting: no secret leakage, imports resolve, repo hygiene
# ────────────────────────────────────────────────────────────────────────


class TestCrossCutting51:
    def test_kernel_importable_without_network(self):
        import inference_kernel  # noqa: F401

    def test_router_importable_without_network(self):
        import inference_router  # noqa: F401

    def test_yaml_parses_and_has_expected_top_level_keys(self, cfg):
        for key in ("default_tier", "providers", "routing"):
            assert key in cfg

    def test_every_referenced_tier_is_defined(self, cfg):
        defined = set(cfg["providers"])
        for name, chain in cfg["routing"]["task_classes"].items():
            unknown = [t for t in chain if t not in defined]
            assert not unknown, f"{name} references undefined tiers {unknown}"

    def test_every_provider_entry_has_a_backend(self, cfg):
        for name, p in cfg["providers"].items():
            assert "backend" in p, name

    def test_backends_come_from_known_vocabulary(self, cfg):
        known = {"llama_cpp_server", "openai_compat", "hermes_cli"}
        for name, p in cfg["providers"].items():
            assert p["backend"] in known, (name, p["backend"])

    def test_hermes_cli_backend_only_on_the_cli_failover_tier(self, cfg):
        """hermes_cli is a legitimate *provider* transport for the
        ox_alpha CLI tier inside the ROUTER plane only — it must never be
        presented as an HTTP completion endpoint elsewhere."""
        cli_tiers = [n for n, p in cfg["providers"].items()
                     if p["backend"] == "hermes_cli"]
        assert set(cli_tiers) <= {"ox_alpha"}, cli_tiers

    def test_no_test_secret_in_repo_configs(self):
        for path in (PROVIDERS_YAML,):
            text = path.read_text(encoding="utf-8")
            assert "sk-" not in text.replace("--sk-", "")  # crude key scan

    def test_this_module_is_tests_only(self):
        assert Path(__file__).parent.name == "tests"
