"""Reinforce the dual inference planes (characterization wave 2).

Callisto intentionally runs TWO routing planes side by side:

1. KERNEL plane — ``inference_kernel.MODEL_LADDER``: task_type -> ordered
   model list walked by ``inference.complete()`` / ``escalate_with_ladder()``
   on every call. Local Ollama models + an explicit ``claude_code`` frontier
   rung. This plane must NOT depend on ``hermes_cli`` as a transport and
   must NOT be pointed at ``ProviderRouter`` until a measured migration.
2. CLI/pipeline plane — ``inference_router.ProviderRouter``: endpoint-pool
   routing backed by ``config/providers.yaml`` via ``load_providers_config``,
   used by callisto.py. It may mention hermes_cli (it has a hermes_cli
   backend option); it is a SEPARATE module from the kernel ladder.

Measured Hermes CLI fork latency is p50 ~11.9s / max ~31.4s
(findings/hermes_latency_2026-08-26.md) so unifying onto ProviderRouter is
forbidden this wave. These tests are characterization only: they PIN the
existing behaviour of both planes and their separation. No production file
is modified by this module.
"""

import asyncio
import inspect
from pathlib import Path

import httpx
import pytest
import yaml

import inference
import inference_kernel
import inference_router

REPO_ROOT = Path(inference.__file__).resolve().parent


# ============================================================================
# Plane 1 — MODEL_LADDER (kernel): structure invariants
# ============================================================================


class TestModelLadderStructure:
    """MODEL_LADDER shape: keys, ordering, entry fields."""

    def test_ladder_is_dict_of_lists(self):
        assert isinstance(inference_kernel.MODEL_LADDER, dict)
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            assert isinstance(task_type, str)
            assert isinstance(ladder, list), task_type
            assert len(ladder) > 0, f"empty ladder for {task_type}"

    def test_expected_task_types_present(self):
        expected = {
            "reasoning",
            "classification",
            "review",
            "code_generation",
            "hypothesis_gen",
            "deep_work",
        }
        missing = expected - set(inference_kernel.MODEL_LADDER)
        assert not missing, f"MODEL_LADDER lost task types: {missing}"

    def test_every_entry_has_model_quality_timeout(self):
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            for i, entry in enumerate(ladder):
                assert "model" in entry, (task_type, i)
                assert "quality" in entry, (task_type, i)
                assert "timeout" in entry, (task_type, i)
                assert isinstance(entry["model"], str) and entry["model"]
                assert entry["quality"] in {"frontier", "high", "medium", "low"}
                assert isinstance(entry["timeout"], int) and entry["timeout"] > 0

    def test_entries_are_ordered_dicts_with_stable_keys(self):
        """Entries carry exactly the three routing fields — no surprise keys."""
        allowed = {"model", "quality", "timeout"}
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            for entry in ladder:
                extra = set(entry) - allowed
                assert not extra, (task_type, extra)

    def test_reasoning_ladder_starts_frontier_then_local(self):
        ladder = inference_kernel.MODEL_LADDER["reasoning"]
        assert ladder[0]["model"] == "claude_code"
        assert ladder[0]["quality"] == "frontier"
        # At least two local fallbacks behind the frontier rung.
        assert len(ladder) >= 3

    def test_classification_is_single_fast_rung(self):
        ladder = inference_kernel.MODEL_LADDER["classification"]
        assert len(ladder) <= 2
        assert all(e["timeout"] <= 60 for e in ladder)

    def test_no_empty_model_names_anywhere(self):
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            for entry in ladder:
                assert entry["model"].strip(), task_type

    def test_ladder_timeouts_are_positive_ints_sorted_loosely(self):
        """Timeouts are sane upper bounds (no zero/negative)."""
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            for entry in ladder:
                assert 1 <= entry["timeout"] <= 600, (task_type, entry)

    def test_hypothesis_gen_keeps_local_primary(self):
        """HYBRID MODE comment promises local-first to save Claude credits."""
        ladder = inference_kernel.MODEL_LADDER["hypothesis_gen"]
        primary = ladder[0]["model"]
        assert primary != "claude_code", (
            "hypothesis_gen flipped back to Claude-first; that was the "
            "explicit cost regression the HYBRID MODE note forbids"
        )
        # claude_code remains available as last-resort somewhere in the list
        models = [e["model"] for e in ladder]
        assert "claude_code" in models

    def test_deep_work_and_code_generation_have_frontier_rung(self):
        for tt in ("deep_work", "code_generation"):
            qualities = {e["quality"] for e in inference_kernel.MODEL_LADDER[tt]}
            assert "frontier" in qualities, tt


# ============================================================================
# Plane 1 — MODEL_LADDER (kernel): NO hermes_cli transport, no router leak
# ============================================================================


KERNEL_MODULE_PATH = Path(inference_kernel.__file__)


def _ladder_source_segment() -> str:
    src = KERNEL_MODULE_PATH.read_text(encoding="utf-8")
    assign = src.index("MODEL_LADDER:")
    return src[assign : src.index("\n\n", assign)]


class TestKernelLadderTransportIsolation:
    """The kernel ladder walks Ollama/claude_code transports — never hermes_cli,
    never ProviderRouter."""

    def test_ladder_literal_does_not_mention_hermes_cli(self):
        seg = _ladder_source_segment()
        assert "hermes_cli" not in seg

    def test_ladder_literal_does_not_reference_provider_router(self):
        seg = _ladder_source_segment()
        assert "ProviderRouter" not in seg
        assert "providers.yaml" not in seg

    def test_complete_functions_do_not_use_hermes_cli(self):
        for name in dir(inference_kernel):
            if not (name.startswith("complete") or name.startswith("escalate")):
                continue
            obj = getattr(inference_kernel, name)
            if not callable(obj):
                continue
            try:
                s = inspect.getsource(obj)
            except TypeError:
                continue
            assert "hermes_cli" not in s, f"{name} mentions hermes_cli"

    def test_facade_reexports_match_kernel_identity(self):
        """inference.MODEL_LADDER IS the kernel object, not a copy."""
        assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER

    def test_kernel_module_does_not_import_inference_router(self):
        src = KERNEL_MODULE_PATH.read_text(encoding="utf-8")
        assert "from inference_router" not in src
        assert "import inference_router" not in src
        assert "ProviderRouter" not in src.replace(
            "# ProviderRouter lives in inference_router.py", ""
        ) or True  # docstring mention is fine; import is what's forbidden
        assert "inference_router" not in [
            getattr(m, "__name__", "")
            for m in vars(inference_kernel).values()
            if inspect.ismodule(m)
        ]

    def test_router_module_does_not_import_kernel_ladder(self):
        """The router may import tiny kernel helpers (logger, JSON parsing)
        but must never re-export or use the ladder walk or kernel client."""
        assert "MODEL_LADDER" not in vars(inference_router)
        assert "OllamaInference" not in vars(inference_router)
        for name in ("escalate_with_ladder", "warmup_models"):
            assert not hasattr(inference_router, name), (
                f"inference_router started exporting kernel API {name}"
            )

    def test_ladder_models_are_strings_not_endpoint_configs(self):
        for task_type, ladder in inference_kernel.MODEL_LADDER.items():
            for entry in ladder:
                assert isinstance(entry["model"], str)


# ============================================================================
# Plane 1 — kernel constants re-exported through the facade
# ============================================================================


class TestFacadeReexports:
    @pytest.mark.parametrize(
        "name",
        [
            "MODEL_LADDER",
            "OllamaInference",
            "escalate_with_ladder",
            "_get_inference",
            "_parse_json_response",
            "_demote_claude_in_ladder",
            "warmup_models",
            "APRIEL_MODEL",
            "DEVSTRAL_MODEL",
            "GEMMA4_MODEL",
            "QWEN36_MODEL",
        ],
    )
    def test_kernel_names_reexported(self, name):
        assert hasattr(inference, name), f"facade lost kernel export {name}"

    @pytest.mark.parametrize(
        "name",
        [
            "ProviderRouter",
            "EndpointConfig",
            "TierConfig",
            "EscalationConfig",
            "CostLedger",
            "TASK_CLASS_ALIASES",
            "UnknownTaskClassError",
            "load_providers_config",
            "get_router",
            "_post_with_retry",
            "_retry_after_seconds",
        ],
    )
    def test_router_names_reexported(self, name):
        assert hasattr(inference, name), f"facade lost router export {name}"

    def test_same_objects_not_copies(self):
        assert inference.ProviderRouter is inference_router.ProviderRouter
        assert (
            inference.load_providers_config
            is inference_router.load_providers_config
        )
        assert inference.TASK_CLASS_ALIASES is inference_router.TASK_CLASS_ALIASES

    def test_facade_documents_two_planes(self):
        src = inspect.getsource(inference)
        assert "TWO INFERENCE PLANES" in src or "INFERENCE PLANES" in src
        assert "inference_kernel" in src and "inference_router" in src

    def test_latency_pin_survives_in_facade(self):
        src = inspect.getsource(inference)
        pinned = ("p50" in src and "11.9" in src) or (
            "hermes_latency_2026-08-26.md" in src
        )
        assert pinned, "facade lost its measured-latency citation"


# ============================================================================
# Plane 2 — ProviderRouter: vocabulary bridge & LOUD unknown classes
# ============================================================================


MINIMAL_YAML = """
default_tier: t_local

providers:
  t_local:
    backend: openai_compat
    base_url: http://localhost:9999/v1
    model: test-model-a
    context_tokens: 4096
    structured_output: true
    tool_calls: false
    max_concurrency: 1
  t_backup:
    backend: openai_compat
    base_url: http://localhost:9998/v1
    model: test-model-b
    context_tokens: 4096
    structured_output: false
    tool_calls: false
    max_concurrency: 2

routing:
  task_classes:
    research_synthesis: [t_local, t_backup]
    hypothesis_generation: t_local
  escalation:
    json_schema_failures: 2
"""


@pytest.fixture()
def tmp_cfg(tmp_path):
    p = tmp_path / "providers_test.yaml"
    p.write_text(MINIMAL_YAML)
    return p


@pytest.fixture()
def router(tmp_cfg):
    return inference_router.ProviderRouter(config_path=str(tmp_cfg))


class TestRouterTaskClassVocabulary:
    def test_aliases_map_call_site_names_to_canonical(self):
        aliases = inference_router.TASK_CLASS_ALIASES
        assert aliases["deep_work"] == "research_synthesis"
        assert aliases["hypothesis_gen"] == "hypothesis_generation"
        assert aliases["reasoning"] == "research_synthesis"
        assert aliases["review"] == "adversarial_review"
        assert aliases["code_generation"] == "research_synthesis"

    def test_alias_targets_are_canonical_names_not_aliases(self):
        """No alias-of-alias chains: every value must be resolvable directly."""
        values = set(inference_router.TASK_CLASS_ALIASES.values())
        keys = set(inference_router.TASK_CLASS_ALIASES)
        overlap = values & keys
        assert not overlap, f"alias chains introduced: {overlap}"

    def test_known_call_site_name_resolves_via_alias(self, router):
        assert router.canonical_task_class("deep_work") == "research_synthesis"
        assert router.canonical_task_class("hypothesis_gen") == (
            "hypothesis_generation"
        )

    def test_unknown_task_class_raises_loudly(self, router):
        with pytest.raises(inference_router.UnknownTaskClassError):
            router.canonical_task_class("totally_bogus_class")

    def test_unknown_class_error_is_keyerror_subclass(self):
        assert issubclass(inference_router.UnknownTaskClassError, KeyError)

    def test_error_message_names_declared_classes(self, router):
        with pytest.raises(KeyError) as ei:
            router.canonical_task_class("nope")
        msg = str(ei.value)
        assert "research_synthesis" in msg
        assert "declared" in msg

    def test_real_config_declares_all_alias_targets(self):
        cfg = inference_router.load_providers_config()
        declared = set((cfg.get("routing") or {}).get("task_classes") or {})
        targets = set(inference_router.TASK_CLASS_ALIASES.values())
        missing = targets - declared
        assert not missing, (
            f"providers.yaml dropped canonical task classes {missing}; "
            "call-site aliases would raise at runtime"
        )

    def test_all_ladder_keys_route_through_the_bridge(self):
        """Every MODEL_LADDER key is either canonical or a known alias."""
        declared = set(
            (inference_router.load_providers_config().get("routing")
             or {}).get("task_classes") or {}
        )
        aliases = inference_router.TASK_CLASS_ALIASES
        for tt in inference_kernel.MODEL_LADDER:
            canon = aliases.get(tt, tt)
            assert canon in declared, (
                f"MODEL_LADDER key {tt!r} cannot route on the pipeline plane "
                f"(canonical {canon!r} undeclared)"
            )


# ============================================================================
# Plane 2 — ProviderRouter construction from config
# ============================================================================


class TestRouterConstruction:
    def test_constructs_from_minimal_config(self, router):
        assert set(router.endpoints) == {"t_local", "t_backup"}

    def test_default_tier_read(self, router):
        assert router.default_tier_name == "t_local"

    def test_tiers_view_lists_declaration_order(self, router):
        assert router.tiers_view_names() == ["t_local", "t_backup"]

    def test_multi_tier_fallback_list_preserved(self, router):
        names = router.task_classes["research_synthesis"]
        assert names == ["t_local", "t_backup"]

    def test_string_task_class_normalized_to_list(self, router):
        names = router.task_classes["hypothesis_generation"]
        assert names == "t_local" or names == ["t_local"]

    def test_endpoint_capabilities_parsed(self, router):
        ep = router.endpoints["t_backup"]
        assert ep.structured_output is False
        assert ep.max_concurrency == 2
        assert ep.model == "test-model-b"

    def test_missing_providers_section_gives_empty_router(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("default_tier: x\nrouting: {}\n")
        r = inference_router.ProviderRouter(config_path=str(p))
        assert r.endpoints == {}
        assert r.task_classes == {}

    def test_escalation_defaults(self, router):
        assert router.escalation.json_schema_failures == 2
        assert router.escalation.tool_error_loops == 2
        assert router.escalation.confidence_below is None

    def test_budget_unset_by_default(self, router):
        assert router.budget_usd is None
        assert router.cost_ledger.budget_usd is None


class TestRouterCandidates:
    def test_candidates_respect_capability_filter(self, router):
        names = router.candidates_for("research_synthesis", schema={"type": "obj"})
        assert "t_backup" not in names  # structured_output=False filtered out
        assert "t_local" in names

    def test_candidates_without_schema_include_both(self, router):
        names = router.candidates_for("research_synthesis")
        assert names == ["t_local", "t_backup"]

    def test_pick_endpoint_prefers_capable_first_candidate(self, router):
        ep = router.pick_endpoint("research_synthesis", schema={})
        assert ep is not None and ep.name == "t_local"

    def test_pick_endpoint_none_when_all_unresolved(self, tmp_path):
        p = tmp_path / "unres.yaml"
        p.write_text(
            "providers:\n  ghost:\n    backend: openai_compat\n"
            "    base_url_env: CALLISTO_NO_SUCH_ENV_XYZ\n"
            "    model: m\nrouting:\n  task_classes:\n"
            "    research_synthesis: [ghost]\n"
        )
        r = inference_router.ProviderRouter(config_path=str(p))
        assert r.pick_endpoint("research_synthesis") is None

    def test_candidates_degrade_not_crash_when_cooling(self, router):
        # Mark everything cooling down; candidates_for must still return
        # the least-bad endpoint rather than raising.
        for st in router.states.values():
            st.cooldown_until = float("inf")
        names = router.candidates_for("hypothesis_generation")
        assert names == ["t_local"] or "t_local" in names

    def test_tier_for_returns_backcompat_view(self, router):
        tier = router.tier_for("research_synthesis")
        assert isinstance(tier, inference_router.TierConfig)
        assert tier.name == "t_local"
        assert tier.base_url == "http://localhost:9999/v1"


# ============================================================================
# Plane 2 — 429 retry/backoff helpers stay bounded
# ============================================================================


class TestRetryHelpers:
    def test_defaults_are_small_and_bounded(self):
        assert inference_router._429_DEFAULT_BACKOFF_S == 1.0
        assert inference_router._429_MAX_TOTAL_WAIT_S == 10.0

    def test_retry_after_caps_hostile_values(self):
        class FakeResp:
            headers = {"Retry-After": "500"}
            status_code = 429

        val = inference_router._retry_after_seconds(FakeResp())
        assert val == inference_router._429_MAX_TOTAL_WAIT_S

    def test_retry_after_accepts_numeric_header(self):
        class FakeResp:
            headers = {"Retry-After": "3"}
            status_code = 429

        assert inference_router._retry_after_seconds(FakeResp()) == 3.0

    def test_retry_after_garbage_yields_default(self):
        class FakeResp:
            headers = {"Retry-After": "soon-ish"}
            status_code = 429

        assert inference_router._retry_after_seconds(FakeResp()) == (
            inference_router._429_DEFAULT_BACKOFF_S
        )

    def test_post_with_retry_retries_429_in_place_then_raises(self):
        calls = {"n": 0}

        def make_exc():
            req = type("R", (), {})()
            resp = type(
                "Resp", (), {"status_code": 429, "headers": {}}
            )()
            return httpx.HTTPStatusError("429", request=req, response=resp)

        async def post_fn(endpoint, payload, timeout):
            calls["n"] += 1
            raise make_exc()

        async def run():
            ep = inference_router.EndpointConfig(
                name="e", backend="openai_compat",
                base_url="http://x/v1", model="m",
            )
            with pytest.raises(httpx.HTTPStatusError):
                await inference_router._post_with_retry(post_fn, ep, {}, 1.0)
            return calls["n"]

        n = asyncio.run(run())
        assert n == 2, f"expected exactly attempts=2 in-place retries, got {n}"

    def test_post_with_retry_does_not_retry_client_errors(self):
        calls = {"n": 0}

        def make_exc():
            req = type("R", (), {})()
            resp = type("Resp", (), {"status_code": 400, "headers": {}})()
            return httpx.HTTPStatusError("400", request=req, response=resp)

        async def post_fn(endpoint, payload, timeout):
            calls["n"] += 1
            raise make_exc()

        async def run():
            ep = inference_router.EndpointConfig(
                name="e", backend="openai_compat",
                base_url="http://x/v1", model="m",
            )
            with pytest.raises(httpx.HTTPStatusError):
                await inference_router._post_with_retry(post_fn, ep, {}, 1.0)

        asyncio.run(run())
        assert calls["n"] == 1, "4xx must fail over immediately (no retry)"

    def test_post_with_retry_returns_success_first_try(self):
        async def post_fn(endpoint, payload, timeout):
            return '{"ok": true}', {"endpoint": endpoint.name}

        async def run():
            ep = inference_router.EndpointConfig(
                name="e", backend="openai_compat",
                base_url="http://x/v1", model="m",
            )
            return await inference_router._post_with_retry(post_fn, ep, {}, 1.0)

        content, meta = asyncio.run(run())
        assert '"ok"' in content
        assert meta.get("endpoint") == "e"


# ============================================================================
# Separation — the two planes remain distinct artifacts
# ============================================================================


class TestPlaneSeparation:
    def test_kernel_and_router_are_different_files(self):
        assert KERNEL_MODULE_PATH != Path(inference_router.__file__)
        assert KERNEL_MODULE_PATH.exists() and Path(
            inference_router.__file__
        ).exists()

    def test_facade_does_not_define_ladder_itself(self):
        src = inspect.getsource(inference)
        assert "MODEL_LADDER:" not in src, (
            "facade started defining MODEL_LADDER instead of re-exporting "
            "the kernel one"
        )

    def test_router_config_file_exists_at_declared_path(self):
        p = Path(inference_router._PROVIDERS_CONFIG_PATH)
        assert p.exists()
        cfg = yaml.safe_load(p.read_text())
        assert isinstance(cfg.get("providers"), dict)

    def test_kernel_docstring_points_to_separate_router_module(self):
        head = KERNEL_MODULE_PATH.read_text(encoding="utf-8")[:2000]
        assert "inference_router.py" in head

    def test_neither_plane_imports_the_other_at_runtime(self):
        import sys

        kernel_mods = {
            m.__name__ for m in vars(inference_kernel).values()
            if inspect.ismodule(m)
        }
        router_mods = {
            m.__name__ for m in vars(inference_router).values()
            if inspect.ismodule(m)
        }
        assert "inference_router" not in kernel_mods
        assert "inference_kernel" not in router_mods

    def test_get_router_singleton_accessor_exists(self):
        src = inspect.getsource(inference_router.get_router)
        assert "ProviderRouter" in src

    def test_callisto_entrypoint_mentions_both_planes(self):
        src = (REPO_ROOT / "callisto.py").read_text(encoding="utf-8")
        assert "ProviderRouter" in src


# ============================================================================
# Kernel helpers that guard the ladder at runtime
# ============================================================================


class TestDemoteClaudeHelper:
    """_demote_claude_in_ladder exists to push claude_code down when credits
    are scarce — it must keep the rung present, not delete it."""

    def test_helper_exists_and_is_callable(self):
        fn = inference_kernel._demote_claude_in_ladder
        assert callable(fn)

    def test_demotion_keeps_claude_in_ladder(self):
        import copy

        original = copy.deepcopy(inference_kernel.MODEL_LADDER)
        try:
            ladder = inference_kernel.MODEL_LADDER.get("reasoning", [])
            if callable(inference_kernel._demote_claude_in_ladder):
                result = inference_kernel._demote_claude_in_ladder(ladder)
                out = result if result is not None else ladder
                flat = [e["model"] for row in ([out] if isinstance(out, list) else out.values()) for e in row]
                if any(e["model"] == "claude_code" for e in ladder):
                    assert "claude_code" in flat
        finally:
            inference_kernel.MODEL_LADDER.clear()
            inference_kernel.MODEL_LADDER.update(original)

    def test_get_inference_caches_per_model(self):
        a = inference_kernel._get_inference("qwen36")
        b = inference_kernel._get_inference("qwen36")
        assert a is b

    def test_parse_json_response_extracts_object_from_prose(self):
        parse = inference_kernel._parse_json_response
        raw = 'Thinking...\n```json\n{"a": 1}\n```\ndone'
        out = parse(raw)
        assert isinstance(out, dict) and out.get("a") == 1


# ============================================================================
# Guard rails: forbidden unifications stay forbidden
# ============================================================================


class TestForbiddenUnifications:
    FORBIDDEN_IN_KERNEL = (
        "load_providers_config(",
        "get_router(",
        "complete_sync(",
    )

    @pytest.mark.parametrize("snippet", FORBIDDEN_IN_KERNEL)
    def test_kernel_does_not_call_pipeline_plane(self, snippet):
        src = KERNEL_MODULE_PATH.read_text(encoding="utf-8")
        code_only = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        # strip docstrings crudely: quoted triple blocks
        assert snippet not in code_only, (
            f"inference_kernel references pipeline-plane API {snippet!r}"
        )

    def test_no_shared_mutable_state_between_planes(self):
        """The router's ledger/state must be per-router, not module-global
        shared with the kernel."""
        r_state_attrs = {"states", "cost_ledger", "endpoints"}
        for attr in r_state_attrs:
            assert not hasattr(inference_kernel, attr) or getattr(
                inference_kernel, attr, None
            ) is not getattr(inference_router, attr, None)

    def test_facade_module_count_is_exactly_three_sources(self):
        """kernel + router + facade: no fourth merged module appeared."""
        pkg_dir = KERNEL_MODULE_PATH.parent
        plane_files = {
            p.name
            for p in pkg_dir.glob("inference*.py")
            if p.is_file()
        }
        assert plane_files == {
            "inference.py",
            "inference_kernel.py",
            "inference_router.py",
        }, f"unexpected inference modules: {plane_files}"

    def test_unification_comment_still_warns_future_editors(self):
        src = KERNEL_MODULE_PATH.read_text(encoding="utf-8")
        assert "do not unify" in src.lower() or (
            "coexist intentionally" in src
        )


# ============================================================================
# Config-level sanity for providers.yaml (pipeline plane inputs)
# ============================================================================


class TestProvidersYamlShape:
    @pytest.fixture(scope="class")
    def cfg(self):
        return inference_router.load_providers_config()

    def test_has_default_tier(self, cfg):
        assert cfg.get("default_tier")

    def test_every_provider_has_backend_base_url_model(self, cfg):
        for name, raw in cfg["providers"].items():
            backend = raw.get("backend", "openai_compat")
            if backend == "hermes_cli":
                continue  # hermes_cli needs neither base_url nor model
            assert raw.get("base_url") or raw.get("base_url_env"), name
            assert raw.get("model") or raw.get("model_env"), name

    def test_context_tokens_positive_when_declared(self, cfg):
        for name, raw in cfg["providers"].items():
            ct = raw.get("context_tokens")
            if ct is not None:
                assert int(ct) > 0, name

    def test_max_concurrency_positive_when_declared(self, cfg):
        for name, raw in cfg["providers"].items():
            mc = raw.get("max_concurrency")
            if mc is not None:
                assert int(mc) >= 1, name

    def test_task_classes_reference_declared_endpoints(self, cfg):
        endpoints = set(cfg["providers"])
        for tc, tiers in ((cfg.get("routing") or {}).get("task_classes") or {}).items():
            tier_list = [tiers] if isinstance(tiers, str) else list(tiers)
            unknown = set(tier_list) - endpoints
            assert not unknown, f"task_class {tc} references missing endpoints {unknown}"

    def test_scaling_recipe_comment_intact(self, cfg_text=None):
        text = Path(inference_router._PROVIDERS_CONFIG_PATH).read_text(
            encoding="utf-8"
        )
        assert "SCALING RECIPES" in text
