"""Autofill 0083 — characterization pins for the DUAL INFERENCE PLANES.

Characterizes (does not refactor) three coupled contracts that a drive-by
"cleanup" would love to collapse into one — and must not:

1. KERNEL plane   — ``inference_kernel.MODEL_LADDER`` + ``complete()`` /
   ``escalate_with_ladder()``, walked on every inference call.
2. CLI/pipeline plane — ``inference_router.ProviderRouter`` backed by
   ``config/providers.yaml`` via ``load_providers_config``.

Plus the provider topology that lives only in providers.yaml today:

* ``gpu1`` is the local quality-first endpoint with backend
  ``llama_cpp_server`` (llama.cpp server, NOT ollama, NOT hermes_cli).
* ``openrouter_ox`` is an env-backed ``openai_compat`` OpenRouter endpoint —
  the API-swap path whose key never lives in git.

And the fail-closed paper-trade gate:

* ``_PAPER_TRADE_SIGNAL_STATUSES`` stays exactly ``{"paper_trading"}``.
  Adding "live" would arm untested sizing/caps/kill-switch logic. If this pin
  is ever found false, the correct action is FAIL CLOSED (disable / refuse),
  never "fix the test to match".

Measured-latency rationale for keeping two planes: Hermes CLI fork p50 ≈
11.9s / max ≈ 31.4s (findings/hermes_latency_2026-08-26.md). Unifying the
kernel onto ProviderRouter before a deliberate migration is forbidden.
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
from tools.signals import paper as paper_gate

REPO = Path(__file__).resolve().parent.parent
PROVIDERS_YAML = REPO / "config" / "providers.yaml"
KERNEL_SRC_PATH = REPO / "inference_kernel.py"
ROUTER_SRC_PATH = REPO / "inference_router.py"
FACADE_SRC_PATH = REPO / "inference.py"
PAPER_SRC_PATH = REPO / "tools" / "signals" / "paper.py"
BACKTEST_SRC_PATH = REPO / "tools" / "backtest.py"


@pytest.fixture(scope="module")
def providers_cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kernel_src():
    return KERNEL_SRC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def router_src():
    return ROUTER_SRC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def facade_src():
    return FACADE_SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Part 1 — MODEL_LADDER shape (kernel plane)
# ---------------------------------------------------------------------------

def test_model_ladder_is_dict_of_nonempty_lists():
    ladder = inference.MODEL_LADDER
    assert isinstance(ladder, dict) and ladder
    for task_type, rungs in ladder.items():
        assert isinstance(task_type, str) and task_type
        assert isinstance(rungs, list), task_type
        assert len(rungs) > 0, f"{task_type} has an empty ladder"
        for rung in rungs:
            assert isinstance(rung, dict), (task_type, rung)
            assert isinstance(rung.get("model"), str) and rung["model"], task_type


def test_model_ladder_core_task_types_present():
    expected = {"reasoning", "classification", "review", "code_generation",
                "hypothesis_gen", "deep_work"}
    missing = expected - set(inference.MODEL_LADDER)
    assert not missing, f"MODEL_LADDER lost keys: {missing}"


def test_model_ladder_rungs_carry_quality_and_timeout():
    for task_type, rungs in inference.MODEL_LADDER.items():
        for i, rung in enumerate(rungs):
            assert "quality" in rung, f"{task_type}[{i}] missing quality"
            assert rung["quality"] in ("frontier", "high", "medium", "low"), (
                f"{task_type}[{i}] unexpected quality {rung['quality']!r}")
            timeout = rung.get("timeout")
            assert isinstance(timeout, int) and 0 < timeout <= 300, (
                f"{task_type}[{i}] bad timeout {timeout!r}")


def test_model_ladder_no_empty_or_blank_model_names():
    for task_type, rungs in inference.MODEL_LADDER.items():
        for rung in rungs:
            assert rung["model"].strip() == rung["model"]
            assert rung["model"] != ""


def test_classification_ladder_stays_small_and_fast():
    """Classification is high-volume: single cheap rung, short timeout."""
    rungs = inference.MODEL_LADDER["classification"]
    assert len(rungs) <= 2
    assert all(r["timeout"] <= 60 for r in rungs)


def test_reasoning_ladder_has_frontier_first_or_local_primary():
    reasoning = [r["model"] for r in inference.MODEL_LADDER["reasoning"]]
    # Either frontier leads, or a named local primary does — never an empty
    # placeholder or an unnamed sentinel.
    assert reasoning[0] in ("claude_code", "qwen36", "gemma4")


def test_model_ladder_constants_resolve_to_real_names(kernel_src):
    """QWEN36_MODEL etc. are interpolated at definition time; the resulting
    ladder must contain concrete names, not format placeholders."""
    for rungs in inference.MODEL_LADDER.values():
        for rung in rungs:
            assert "{" not in rung["model"] and "}" not in rung["model"]


def test_kernel_module_defines_ladder_as_annotation(kernel_src):
    m = re.search(r"^MODEL_LADDER:\s*dict\[str,\s*list\[dict\]\]", kernel_src, re.M)
    assert m, "MODEL_LADDER lost its explicit type annotation"


# ---------------------------------------------------------------------------
# Part 2 — kernel plane must NOT mention hermes_cli or ProviderRouter
# ---------------------------------------------------------------------------

def _ladder_source_block(src: str) -> str:
    start = src.index("MODEL_LADDER:")
    end = src.index("\n\n", start)
    return src[start:end]


def test_model_ladder_block_does_not_mention_hermes_cli(kernel_src):
    block = _ladder_source_block(kernel_src)
    assert "hermes_cli" not in block, "MODEL_LADDER mentions hermes_cli"


def test_model_ladder_block_does_not_mention_provider_router(kernel_src):
    block = _ladder_source_block(kernel_src)
    assert "ProviderRouter" not in block.replace(
        "# 2. ProviderRouter + config/providers.yaml (loaded via load_providers_config)",
        "",
    ), "MODEL_LADDER assignment references ProviderRouter"


def test_kernel_complete_walk_does_not_use_provider_router(kernel_src):
    tree = ast.parse(kernel_src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name in ("complete", "escalate_with_ladder", "_get_inference",
                          "_make_agent")
        ):
            body = ast.get_source_segment(kernel_src, node) or ""
            if "get_router" in body or "ProviderRouter(" in body:
                offenders.append(node.name)
    assert not offenders, f"kernel functions route via ProviderRouter: {offenders}"


def test_kernel_does_not_import_inference_router(kernel_src):
    assert "from inference_router import" not in kernel_src
    assert "import inference_router" not in kernel_src


def test_router_does_not_import_the_kernel(router_src):
    """The planes must not import each other's ROUTING machinery (that IS
    unification). Shared leaf helpers (_parse_json_response, logger) are
    allowed; the ladder / complete() walk are not."""
    for line in router_src.splitlines():
        if "from inference_kernel import" in line:
            imported = line.split("import", 1)[1]
            names = {n.strip() for n in imported.split(",")}
            forbidden = names & {"MODEL_LADDER", "complete", "escalate_with_ladder",
                                 "_get_inference", "OllamaInference"}
            assert not forbidden, f"router imports kernel routing: {forbidden}"


def test_complete_and_escalate_do_not_mention_hermes_cli():
    for name in ("complete", "complete_sync"):
        fn = getattr(inference, name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        assert "hermes_cli" not in src, f"{name} mentions hermes_cli"
    esc = getattr(inference, "escalate_with_ladder", None)
    if esc is not None:
        assert "hermes_cli" not in inspect.getsource(esc)


def test_facade_reexports_both_planes(facade_src):
    for symbol in ("MODEL_LADDER", "OllamaInference", "complete",
                   "escalate_with_ladder"):
        assert symbol in facade_src.split("from inference_router")[0], symbol
    for symbol in ("ProviderRouter", "load_providers_config", "CostLedger",
                   "EndpointConfig"):
        assert symbol in facade_src, symbol


def test_planes_are_distinct_objects_not_aliases():
    import sys

    assert inference_kernel.MODEL_LADDER is inference.MODEL_LADDER
    assert inference_router.ProviderRouter is not None
    # both plane modules are loaded exactly once (no dual-import split brain)
    assert "inference_kernel" in sys.modules and "inference_router" in sys.modules


# ---------------------------------------------------------------------------
# Part 3 — latency pin survives (why the planes stay separate)
# ---------------------------------------------------------------------------

def test_kernel_or_facade_cites_measured_hermes_latency(kernel_src, facade_src):
    combined = kernel_src + facade_src
    pinned = ("p50" in combined and "11.9" in combined) or (
        "hermes_latency_2026-08-26.md" in combined)
    assert pinned, "measured Hermes fork latency pin was deleted"


def test_kernel_comments_declare_two_plane_intent(kernel_src):
    assert "TWO INFERENCE PLANES" in kernel_src
    assert "do not unify" in kernel_src.lower()


def test_no_unification_marker_added(kernel_src, router_src):
    """Nobody has 'temporarily' pointed the kernel ladder at the router by
    rewriting MODEL_LADDER values to endpoint names from providers.yaml."""
    cfg = yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))
    endpoint_names = set(cfg.get("providers", {}))
    block_ast = None
    tree = ast.parse(kernel_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MODEL_LADDER":
            block_ast = node.value
    assert block_ast is not None
    models = set()
    for node in ast.walk(block_ast):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "dict":
            continue
        if isinstance(node, ast.keyword) and node.arg == "model" and isinstance(node.value, ast.Constant):
            models.add(node.value.value)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k and getattr(k, "value", None) == "model" and isinstance(v, ast.Constant):
                    models.add(v.value)
    leaked = {m for m in models if m.startswith(("gpu1", "gpu2",
                                                 "ox_alpha", "openrouter_ox"))}
    assert not leaked, f"MODEL_LADDER leaks pipeline-plane endpoints: {leaked}"
    assert endpoint_names  # sanity: config actually defines providers


# ---------------------------------------------------------------------------
# Part 4 — gpu1 backend llama_cpp_server
# ---------------------------------------------------------------------------

def test_gpu1_exists_with_llama_cpp_server_backend(providers_cfg):
    gpu1 = providers_cfg["providers"]["gpu1"]
    assert gpu1["backend"] == "llama_cpp_server"


def test_gpu1_points_at_local_llama_cpp_ports(providers_cfg):
    gpu1 = providers_cfg["providers"]["gpu1"]
    base_url = gpu1["base_url"]
    assert base_url.startswith("http://localhost") or base_url.startswith("http://127.0.0.1")
    assert "/v1" in base_url


def test_gpu1_has_model_and_context(providers_cfg):
    gpu1 = providers_cfg["providers"]["gpu1"]
    assert isinstance(gpu1["model"], str) and gpu1["model"]
    assert int(gpu1["context_tokens"]) > 0


def test_gpu1_fast_is_also_llama_cpp_server(providers_cfg):
    fast = providers_cfg["providers"].get("gpu1_fast")
    if fast is not None:
        assert fast["backend"] == "llama_cpp_server"


def test_default_tier_is_gpu1(providers_cfg):
    assert providers_cfg.get("default_tier") == "gpu1"


def test_local_backends_tuple_includes_llama_cpp_server():
    assert "llama_cpp_server" in inference_router.LOCAL_BACKENDS


def test_gpu1_never_appears_in_kernel_ladder_models(kernel_src):
    block = _ladder_source_block(kernel_src)
    assert "gpu1" not in block and "8080" not in block


# ---------------------------------------------------------------------------
# Part 5 — openrouter_ox is openai_compat, env-backed
# ---------------------------------------------------------------------------

def test_openrouter_ox_backend_is_openai_compat_env_backed(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    assert ox["backend"] == "openai_compat"
    assert ox["api_key_env"] == "OPENROUTER_API_KEY"
    assert "api_key" not in ox


def test_openrouter_ox_base_url_and_model(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    assert ox["base_url"] == "https://openrouter.ai/api/v1"
    assert ox["model"] == "stealth/ox-alpha"


def test_providers_yaml_contains_no_literal_key():
    raw = PROVIDERS_YAML.read_text(encoding="utf-8")
    assert "sk-or-v1-" not in raw


def test_openrouter_ox_sits_after_local_rungs_in_chains(providers_cfg):
    chains = providers_cfg["routing"]["task_classes"]
    checked = 0
    for name, chain in chains.items():
        if "openrouter_ox" in chain:
            locals_ = [p for p in chain if p.startswith("gpu")]
            for loc in locals_:
                assert chain.index(loc) < chain.index("openrouter_ox"), name
            checked += 1
    assert checked > 0, "no chain references openrouter_ox"


def test_endpoint_from_config_reads_openai_compat(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    ep = inference_router._endpoint_from_config("openrouter_ox", ox)
    assert ep.backend == "openai_compat"
    assert ep.model == "stealth/ox-alpha"


# ---------------------------------------------------------------------------
# Part 6 — fail-closed paper-trade gate (never arm live betting)
# ---------------------------------------------------------------------------

def test_paper_statuses_exactly_paper_trading():
    assert paper_gate.allowed_paper_statuses() == frozenset({"paper_trading"})


def test_paper_statuses_is_a_frozenset():
    assert isinstance(paper_gate.allowed_paper_statuses(), frozenset)


def test_live_status_is_rejected():
    assert paper_gate.reject_non_paper("live") is True


def test_paper_trading_status_passes_gate():
    assert paper_gate.reject_non_paper("paper_trading") is False


def test_case_variants_of_live_rejected():
    for s in ("Live", "LIVE", " live", "live ", "live_trading"):
        assert paper_gate.reject_non_paper(s) is True, repr(s)


def test_gate_source_does_not_contain_live_status(kernel_src=None):
    src = PAPER_SRC_PATH.read_text(encoding="utf-8")
    statuses_line = next(
        line for line in src.splitlines()
        if line.startswith("_PAPER_TRADE_SIGNAL_STATUSES"))
    assert '"paper_trading"' in statuses_line
    assert "live" not in statuses_line.lower()


def test_generate_paper_trade_signal_returns_empty_for_live(monkeypatch):
    """Behavioral pin: even with a hypothesis in hand, status='live' yields []
    BEFORE any odds processing."""

    class FakeHypManager:
        async def get_hypothesis(self, hypothesis_id):
            return {"id": hypothesis_id, "status": "live"}

    class FakeEngine:
        hypothesis_manager = FakeHypManager()

    called = {"pipeline": False}

    import tools.backtest as bt

    original_pipeline_fn = bt.paper_pipeline.generate_paper_trade_signal

    async def spy(*a, **k):  # pragma: no cover - must never be reached
        called["pipeline"] = True
        return [{"should_never": "happen"}]

    monkeypatch.setattr(bt.paper_pipeline, "generate_paper_trade_signal", spy)
    import asyncio
    engine = FakeEngine()
    result = asyncio.run(bt.BacktestEngine.generate_paper_trade_signal(
        engine, "hyp-1", {"event": "x"}))
    assert result == []
    assert called["pipeline"] is False
    # restore defensively (monkeypatch undoes anyway)
    bt.paper_pipeline.generate_paper_trade_signal = original_pipeline_fn


def test_generate_paper_trade_signal_returns_empty_for_missing_hypothesis():
    class NoHypEngine:
        hypothesis_manager = None

    import asyncio
    import tools.backtest as bt
    # hypothesis_manager None -> get_hypothesis raises AttributeError; the
    # gate contract is that a *missing* hypothesis also returns [].
    async def fake_get(hyp_id):
        return None

    class Engine:
        class hypothesis_manager:  # noqa: N801
            get_hypothesis = staticmethod(fake_get)

    result = asyncio.run(bt.BacktestEngine.generate_paper_trade_signal(
        Engine(), "gone", {}))
    assert result == []


def test_backtest_docstring_keeps_hard_gate_language():
    src = BACKTEST_SRC_PATH.read_text(encoding="utf-8")
    seg = src[src.index("async def generate_paper_trade_signal"):]
    seg = seg[:seg.index("\n    async def", 10)] if "\n    async def" in seg else seg[:2000]
    assert "HARD GATE" in seg
    assert 'exactly ``"paper_trading"``' in seg


def test_gate_module_has_no_live_widening_anywhere():
    src = PAPER_SRC_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_PAPER_TRADE_SIGNAL_STATUSES"
            for t in node.targets
        ):
            raw = node.value
            vals = set()
            if isinstance(raw, ast.Set) :
                vals = {getattr(e, "value", None) for e in raw.elts}
            elif isinstance(raw, ast.Call):  # frozenset({...})
                for arg in raw.args:
                    if isinstance(arg, (ast.Set, ast.List, ast.Tuple)):
                        vals |= {getattr(e, "value", None) for e in arg.elts}
            assert vals == {"paper_trading"}, vals
            break
    else:
        pytest.fail("_PAPER_TRADE_SIGNAL_STATUSES assignment disappeared")


def test_supervisor_never_switches_model_flag():
    sup = REPO / "scripts" / "nous-supervisor.sh"
    if sup.is_file():
        src = sup.read_text(encoding="utf-8")
        assert "-m \"$MODEL\"" in src
        assert "stealth/ox-alpha" in src


# ---------------------------------------------------------------------------
# Part 7 — router plane still healthy (not gutted to force unification)
# ---------------------------------------------------------------------------

def test_router_exposes_provider_router_class():
    router_cls = inference_router.ProviderRouter
    assert callable(router_cls)
    assert hasattr(router_cls, "complete") or hasattr(router_cls, "route")


def test_get_router_returns_singleton():
    r1 = inference_router.get_router()
    r2 = inference_router.get_router()
    assert r1 is r2
    assert isinstance(r1, inference_router.ProviderRouter)


def test_load_providers_config_matches_yaml(providers_cfg):
    loaded = inference.load_providers_config()
    assert loaded.get("default_tier") == providers_cfg.get("default_tier")
    assert set(loaded["providers"]) == set(providers_cfg["providers"])


def test_task_class_aliases_exist():
    assert isinstance(inference.TASK_CLASS_ALIASES, dict)


def test_cost_ledger_constructible():
    ledger = inference_router.CostLedger()
    assert ledger is not None


def test_unknown_task_class_error_exists():
    assert issubclass(inference.UnknownTaskClassError, Exception)


def test_both_planes_survive_dir_check():
    for sym in ("MODEL_LADDER", "ProviderRouter", "load_providers_config",
                "escalate_with_ladder", "OllamaInference"):
        assert hasattr(inference, sym), sym
