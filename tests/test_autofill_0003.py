"""Autofill characterization #0003 — DUAL INFERENCE PLANES.

Callisto intentionally runs TWO independent inference routing planes and
this module is a large characterization suite pinning their separation:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER`` (task_type -> ordered
   model list), walked by ``inference.complete()`` on every call. It must
   never mention ``hermes_cli`` as a transport and must never reference
   ``ProviderRouter`` / ``providers.yaml`` machinery.
2. CLI/PIPELINE plane — ``inference_router.ProviderRouter`` backed by
   ``config/providers.yaml`` via ``load_providers_config``. This plane
   knows about ``hermes_cli`` (the ox_alpha endpoint), ``llama_cpp_server``
   (gpu1 / gpu1_fast), and ``openai_compat`` HTTP endpoints.

The two planes must NOT be unified: the measured Hermes CLI fork latency
is p50 ~11.9s / max ~31.4s (findings/hermes_latency_2026-08-26.md), so
collapsing the kernel ladder onto ProviderRouter — or vice versa — would
silently change every completion's latency profile. These tests fail
closed if either plane disappears, drifts into naming the other plane's
machinery, or loses its measured-latency citation.

Also pinned here:
* gpu1 stays backend=llama_cpp_server (the local-first contract).
* openrouter_ox stays an env-backed openai_compat endpoint with the key
  only ever referenced by env name, never inline.
* Every task_class chain keeps a local GPU rung ahead of openrouter_ox.
* The paper-trade status gate is untouched: 'live' must never appear as
  an armed signal status via this wave's changes.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def providers_raw():
    return PROVIDERS_YAML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def providers_cfg(providers_raw):
    return yaml.safe_load(providers_raw)


@pytest.fixture(scope="module")
def ladder_block(kernel_src):
    """The MODEL_LADDER dict assignment text, extracted from the kernel."""
    m = re.search(r"^MODEL_LADDER:\s*dict\[.*?\]\s*=\s*\{.*?^\}", kernel_src,
                  re.MULTILINE | re.DOTALL)
    assert m, "MODEL_LADDER assignment not found in inference_kernel.py"
    return m.group(0)


# ---------------------------------------------------------------------------
# Plane 1: the kernel MODEL_LADDER exists and has the expected shape
# ---------------------------------------------------------------------------


class TestKernelLadderShape:
    def test_model_ladder_is_a_dict(self):
        import inference
        assert isinstance(inference.MODEL_LADDER, dict)

    def test_model_ladder_expected_keys(self):
        import inference
        expected = {"reasoning", "classification", "review"}
        missing = expected - set(inference.MODEL_LADDER)
        assert not missing, f"MODEL_LADDER lost keys: {missing}"

    def test_reasoning_rung_nonempty(self):
        import inference
        ladder = inference.MODEL_LADDER["reasoning"]
        assert isinstance(ladder, list) and len(ladder) >= 4

    def test_classification_is_fast_single_rung(self):
        import inference
        ladder = inference.MODEL_LADDER["classification"]
        assert len(ladder) <= 2, (
            "classification should stay a tiny fast ladder, got "
            f"{len(ladder)} rungs"
        )
        for rung in ladder:
            assert rung["timeout"] <= 60

    def test_every_rung_has_model_quality_timeout(self):
        import inference
        for task_type, ladder in inference.MODEL_LADDER.items():
            assert ladder, f"{task_type} ladder is empty"
            for i, rung in enumerate(ladder):
                assert isinstance(rung, dict), f"{task_type}[{i}] not a dict"
                assert "model" in rung, f"{task_type}[{i}] lacks model"
                assert "quality" in rung, f"{task_type}[{i}] lacks quality"
                assert "timeout" in rung, f"{task_type}[{i}] lacks timeout"
                assert rung["timeout"] > 0, f"{task_type}[{i}] bad timeout"

    def test_timeouts_are_monotone_nondecreasing_by_position_not_required(
        self,
    ):
        """Characterization: timeouts vary per rung but are all bounded."""
        import inference
        for task_type, ladder in inference.MODEL_LADDER.items():
            for rung in ladder:
                assert rung["timeout"] <= 180, (
                    f"{task_type} rung {rung['model']} timeout > 180s"
                )

    def test_at_least_one_frontier_head(self):
        """Some ladders lead with a frontier rung; that's intentional hybrid
        mode. Characterize which ones do so a silent flip is visible."""
        import inference
        frontier_heads = sorted(
            tt for tt, lad in inference.MODEL_LADDER.items()
            if lad[0]["quality"] == "frontier"
        )
        # reasoning / code_generation / deep_work historically lead frontier.
        assert set(frontier_heads) >= {"reasoning", "code_generation",
                                       "deep_work"}, frontier_heads


# ---------------------------------------------------------------------------
# Plane 1 hygiene: the kernel must NOT name the other plane's machinery
# ---------------------------------------------------------------------------


class TestKernelPlanePurity:
    def test_ladder_does_not_mention_hermes_cli(self, ladder_block):
        assert "hermes_cli" not in ladder_block, (
            "MODEL_LADDER names hermes_cli — that transport belongs to the "
            "ProviderRouter plane only"
        )

    def test_complete_entrypoint_state(self):
        """Characterization: post-split, the facade exposes the ladder walk
        as ``escalate_with_ladder``; a top-level ``complete()`` is optional.
        Whatever exists must stay hermes_cli-free."""
        import inference
        fn = getattr(inference, "complete", None)
        if fn is not None:
            assert "hermes_cli" not in inspect.getsource(fn)
        walk = getattr(inference, "escalate_with_ladder", None)
        assert walk is not None, (
            "inference.escalate_with_ladder vanished — kernel ladder is "
            "unreachable"
        )
        assert "hermes_cli" not in inspect.getsource(walk), (
            "escalate_with_ladder mentions hermes_cli"
        )

    def test_complete_sync_family_does_not_mention_hermes_cli(self):
        import inference
        offenders = []
        for name in dir(inference):
            obj = getattr(inference, name)
            if callable(obj) and (
                name.startswith("complete") or "complete_sync" in name
            ):
                try:
                    s = inspect.getsource(obj)
                except (OSError, TypeError):
                    continue
                if "hermes_cli" in s:
                    offenders.append(name)
        assert not offenders, f"{offenders} mention hermes_cli"

    def test_kernel_module_does_not_import_provider_router(self, kernel_src):
        assert re.search(
            r"^\s*(from\s+inference_router|import\s+inference_router)\b",
            kernel_src, re.MULTILINE,
        ) is None, (
            "inference_kernel imports ProviderRouter — the planes must not "
            "be unified from the kernel side"
        )

    def test_kernel_module_does_not_instantiate_provider_router(
        self, kernel_src,
    ):
        assert "ProviderRouter(" not in kernel_src.replace(
            "# ProviderRouter(", ""
        ) or kernel_src.count("ProviderRouter(") == 0, (
            "kernel constructs a ProviderRouter instance"
        )

    def test_kernel_docstring_declares_two_planes(self, kernel_src):
        doc = kernel_src[:4000]
        assert "TWO INFERENCE PLANES" in doc or "two planes" in doc.lower()

    def test_kernel_comments_do_not_wire_hermes_cli_into_the_ladder(
        self, ladder_block,
    ):
        # Even comments inside the ladder block shouldn't suggest hermes_cli
        # as a rung — future edits copy comments into code.
        assert "hermes_cli" not in ladder_block

    def test_facade_reexports_model_ladder(self, facade_src):
        assert re.search(r"\bMODEL_LADDER\b", facade_src), (
            "inference.py facade stopped re-exporting MODEL_LADDER"
        )

    def test_facade_reexports_load_providers_config(self, facade_src):
        assert "load_providers_config" in facade_src, (
            "facade lost load_providers_config (plane 2 entry point)"
        )

    def test_facade_keeps_planes_separate_in_docs(self, facade_src):
        head = facade_src[:3000]
        assert "KERNEL" in head.upper() or "kernel plane" in head.lower()


# ---------------------------------------------------------------------------
# Plane 2: ProviderRouter + providers.yaml
# ---------------------------------------------------------------------------


class TestProviderRouterPlane:
    def test_router_module_defines_provider_router(self, router_src):
        assert "class ProviderRouter" in router_src

    def test_router_defines_load_providers_config(self, router_src):
        assert "def load_providers_config" in router_src

    def test_router_knows_hermes_cli_backend(self, router_src):
        # The PIPELINE plane legitimately handles hermes_cli transports;
        # that's exactly why it can't be merged into the kernel.
        assert '"hermes_cli"' in router_src or "'hermes_cli'" in router_src

    def test_unknown_task_class_error_exists(self, router_src):
        assert "class UnknownTaskClassError" in router_src, (
            "loud-fail on typo'd task_class was removed"
        )

    def test_providers_yaml_parses_with_default_tier(self, providers_cfg):
        assert providers_cfg.get("default_tier") == "gpu1"

    def test_providers_yaml_has_multiple_providers(self, providers_cfg):
        provs = providers_cfg.get("providers")
        assert isinstance(provs, dict) and len(provs) >= 4

    def test_task_classes_declared_for_all_canonical_names(self, providers_cfg):
        classes = providers_cfg["routing"]["task_classes"]
        expected = {
            "hypothesis_generation", "research_synthesis", "screening",
            "extraction", "classification", "backtest_interpretation",
            "promotion_judgment", "adversarial_review",
        }
        missing = expected - set(classes)
        assert not missing, f"task_classes lost entries: {missing}"

    def test_every_chain_ends_with_ox_alpha_last_resort(self, providers_cfg):
        classes = providers_cfg["routing"]["task_classes"]
        for name, chain in classes.items():
            assert chain[-1] == "ox_alpha", (
                f"{name} no longer ends at ox_alpha last-resort: {chain}"
            )


# ---------------------------------------------------------------------------
# gpu1: local llama_cpp_server first
# ---------------------------------------------------------------------------


class TestGpu1LocalFirst:
    def test_gpu1_backend_is_llama_cpp_server(self, providers_cfg):
        assert providers_cfg["providers"]["gpu1"]["backend"] == \
            "llama_cpp_server"

    def test_gpu1_fast_backend_is_llama_cpp_server(self, providers_cfg):
        assert providers_cfg["providers"]["gpu1_fast"]["backend"] == \
            "llama_cpp_server"

    def test_gpu1_points_at_local_http(self, providers_cfg):
        url = providers_cfg["providers"]["gpu1"]["base_url"]
        assert url.startswith("http://localhost:") or url.startswith(
            "http://127.0.0.1:"
        ), f"gpu1 drifted off localhost: {url}"

    def test_gpu1_declares_vram_budget(self, providers_cfg):
        assert providers_cfg["providers"]["gpu1"]["vram_gb"] == 16

    def test_local_rungs_precede_openrouter_ox_everywhere(
        self, providers_cfg,
    ):
        classes = providers_cfg["routing"]["task_classes"]
        for name, chain in classes.items():
            local = [p for p in chain if p.startswith("gpu")]
            if local:
                assert chain.index(local[0]) < chain.index("openrouter_ox"), (
                    f"{name}: hosted openrouter_ox jumped ahead of local GPU"
                )

    def test_screening_class_leads_with_fast_local(self, providers_cfg):
        chain = providers_cfg["routing"]["task_classes"]["screening"]
        assert chain[0] == "gpu1_fast"

    def test_judgment_classes_lead_with_frontier(self, providers_cfg):
        classes = providers_cfg["routing"]["task_classes"]
        for name in ("promotion_judgment", "adversarial_review"):
            assert classes[name][0] == "frontier", (
                f"{name} should still prefer the frontier judgment tier"
            )


# ---------------------------------------------------------------------------
# openrouter_ox: env-backed openai_compat API swap path
# ---------------------------------------------------------------------------


class TestOpenrouterOxEndpoint:
    def test_openrouter_ox_present(self, providers_cfg):
        assert "openrouter_ox" in providers_cfg["providers"]

    def test_backend_openai_compat(self, providers_cfg):
        assert providers_cfg["providers"]["openrouter_ox"]["backend"] == \
            "openai_compat"

    def test_base_url_is_openrouter_api(self, providers_cfg):
        assert providers_cfg["providers"]["openrouter_ox"]["base_url"] == \
            "https://openrouter.ai/api/v1"

    def test_key_is_env_backed(self, providers_cfg):
        ox = providers_cfg["providers"]["openrouter_ox"]
        assert ox["api_key_env"] == "OPENROUTER_API_KEY"

    def test_model_identity(self, providers_cfg):
        assert providers_cfg["providers"]["openrouter_ox"]["model"] == \
            "stealth/ox-alpha"

    def test_no_inline_key_material(self, providers_raw):
        assert "sk-or-v1-" not in providers_raw
        assert not re.search(r"OPENROUTER_API_KEY\s*[:=]\s*\S", providers_raw)

    def test_openrouter_ox_in_every_chain_before_proxy_and_cli(
        self, providers_cfg,
    ):
        classes = providers_cfg["routing"]["task_classes"]
        for name, chain in classes.items():
            assert "openrouter_ox" in chain, name
            oroutes = [i for i, p in enumerate(chain)
                       if p in ("ox_alpha_proxy", "ox_alpha")]
            assert oroutes, f"{name} lost the ox_alpha tail"
            assert chain.index("openrouter_ox") < min(oroutes), (
                f"{name}: openrouter_ox should sit ahead of the Hermes-CLI "
                f"transports, got {chain}"
            )

    def test_openrouter_ox_never_appears_in_kernel_plane(
        self, ladder_block,
    ):
        assert "openrouter_ox" not in ladder_block

    def test_structured_output_honesty(self, providers_cfg):
        # JSON-in-text upstream: structured_output must stay honestly False.
        assert providers_cfg["providers"]["openrouter_ox"][
            "structured_output"
        ] is False


# ---------------------------------------------------------------------------
# Cross-plane separation: neither plane swallows the other
# ---------------------------------------------------------------------------


class TestPlaneSeparation:
    def test_kernel_models_are_plain_labels_not_endpoint_names(self):
        import inference
        endpoint_names = {
            "gpu1", "gpu1_fast", "frontier", "ox_alpha", "ox_alpha_proxy",
            "openrouter_ox",
        }
        for task_type, ladder in inference.MODEL_LADDER.items():
            for rung in ladder:
                assert rung["model"] not in endpoint_names - {"claude_code"}, (
                    f"{task_type}: MODEL_LADDER rung names a ProviderRouter "
                    f"endpoint ({rung['model']}) — planes are bleeding together"
                )

    def test_kernel_file_smaller_concern_than_router(self):
        # Sanity sizing: both files exist and are non-trivial modules.
        assert KERNEL.stat().st_size > 5000
        assert ROUTER.stat().st_size > 5000

    def test_two_plane_note_cites_the_pinning_test(self, kernel_src):
        assert "test_inference_planes.py" in kernel_src, (
            "the TWO INFERENCE PLANES comment lost its pointer to the "
            "characterization tests"
        )

    def test_latency_measurement_still_cited(self, kernel_src):
        assert "hermes_latency_2026-08-26.md" in kernel_src and (
            "11.9" in kernel_src
        ), "kernel lost the measured fork-latency citation"

    def test_latency_pin_in_facade_too(self, facade_src, kernel_src):
        combined = facade_src + kernel_src
        assert "p50" in combined or "hermes_latency_2026-08-26.md" in combined

    def test_migration_gate_wording_survives(self, kernel_src):
        low = kernel_src.lower()
        assert "until a deliberate migration lands" in low or (
            "do not unify" in low
        ), "the explicit do-not-unify gate wording was removed"

    def test_router_kernel_import_is_shared_helpers_only(self, router_src):
        """Characterization: the dependency edge runs router -> kernel and
        only for tiny shared helpers (JSON parsing, logger). The router
        must never import the MODEL_LADDER or the ladder walk."""
        imports = re.findall(
            r"^from inference_kernel import (.+)$", router_src, re.MULTILINE,
        )
        assert imports, (
            "expected at least the existing shared-helper import edge"
        )
        imported = set()
        for line in imports:
            imported.update(n.strip() for n in line.split(","))
        allowed = {"_parse_json_response", "logger"}
        bad = imported - allowed
        assert not bad, f"inference_router imports kernel names: {bad}"
        assert "MODEL_LADDER" not in imported
        assert "escalate_with_ladder" not in imported

    def test_kernel_does_not_import_router(self, kernel_src):
        assert re.search(
            r"^\s*(from\s+inference_router|import\s+inference_router)\b",
            kernel_src, re.MULTILINE,
        ) is None, (
            "inference_kernel imports ProviderRouter — the planes must not "
            "be unified from the kernel side"
        )


# ---------------------------------------------------------------------------
# Fail-closed guardrails (tests-only; production gates untouched)
# ---------------------------------------------------------------------------


class TestFailClosedGuardrails:
    def test_paper_trade_signal_statuses_have_no_live(self):
        """Hard safety rail of the whole repo: this characterization wave
        must never arm live betting, and the armed-status set must not
        contain 'live'. Fails closed if someone widened it."""
        import re as _re
        import tools.signals.paper as paper_mod
        statuses = set(paper_mod._PAPER_TRADE_SIGNAL_STATUSES)
        assert "live" not in statuses, (
            "tools/signals/paper.py arms live betting: "
            f"{sorted(statuses)}"
        )
        assert any("paper" in s for s in statuses), (
            f"paper status lost entirely: {sorted(statuses)}"
        )

    def test_paper_status_source_unchanged_shape(self):
        """Fail-closed: pin the literal armed-status definition so any edit
        toward 'live' shows up as a characterization break."""
        src = (REPO / "tools" / "signals" / "paper.py").read_text(
            encoding="utf-8")
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\("
                      r"\{(.+?)\}\)", src, re.DOTALL)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES definition changed shape"
        body = m.group(1)
        statuses = re.findall(r"[\"']([a-z_]+)[\"']", body)
        assert "live" not in statuses

    def test_empirical_routing_off_by_default(self, providers_cfg):
        er = providers_cfg["routing"].get("empirical_routing", {})
        assert er.get("enabled") is False, (
            "empirical routing flipped on without measurements — fail closed"
        )

    def test_budget_cap_present_and_positive(self, providers_cfg):
        usd = providers_cfg["routing"]["budget"]["usd"]
        assert usd and usd > 0

    def test_sensitive_context_stays_local_pin(self, providers_cfg):
        esc = providers_cfg["routing"]["escalation"]
        assert esc.get("sensitive_context_stays_local") is True

    def test_health_checks_enabled(self, providers_cfg):
        assert providers_cfg["routing"]["health_checks"]["enabled"] is True

    def test_escalation_thresholds_are_tight(self, providers_cfg):
        esc = providers_cfg["routing"]["escalation"]
        assert esc["json_schema_failures"] >= 1
        assert esc["tool_error_loops"] >= 1
        assert 0 < esc["confidence_below"] < 1

    def test_this_wave_added_only_tests(self):
        """Characterization: the exclusive deliverable of task #0003 is the
        test module itself. If production files changed under us, surface
        it loudly instead of characterizing moving ground."""
        out = subprocess_git_status()
        dirty_prod = [
            p for p in out
            if p not in ("tests/test_autofill_0003.py", "OX_DONE.md")
            and not p.startswith(".hermes/")
        ]
        assert not dirty_prod, (
            f"unexpected production changes in worktree: {dirty_prod}"
        )


def subprocess_git_status() -> list[str]:
    import subprocess
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    lines = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            lines.append(parts[1].strip('"').removeprefix("-> ").strip())
    return lines
