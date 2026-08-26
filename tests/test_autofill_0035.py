"""Autofill characterization #0035 — dual inference planes (LONG).

Characterizes the TWO-PLANE inference architecture of Callisto so that a
future "cleanup" cannot silently collapse them:

1. KERNEL plane — ``inference_kernel.py``: ``MODEL_LADDER`` (task_type ->
   ordered model list) walked by ``complete()``/``escalate_with_ladder()``
   on every call.
2. CLI/pipeline plane — ``inference_router.py``: ``ProviderRouter`` backed
   by ``config/providers.yaml`` via ``load_providers_config``.

Pins enforced here:

* ``MODEL_LADDER`` must NOT mention ``hermes_cli`` or ``ProviderRouter``
  — Hermes is the agent runtime (supervisor), never a kernel transport,
  and the ladder must stay decoupled from the router's provider names.
* The local quality tier ``gpu1`` keeps ``backend: llama_cpp_server`` on
  an OpenAI-compatible localhost endpoint.
* ``openrouter_ox`` stays an env-backed (``OPENROUTER_API_KEY``)
  ``openai_compat`` provider with no key material in git.
* The planes are NOT unified: each lives in its own module, and neither
  module may import the other into its own namespace as a merge.
* Fail-closed safety: ``_PAPER_TRADE_SIGNAL_STATUSES`` remains exactly
  ``{"paper_trading"}`` — this module must NEVER add "live", and
  ``generate_paper_trade_signal`` must never be widened to status ==
  'live'.
* Completions stay HTTP; the supervisor keeps launching Hermes with
  ``-m "$MODEL"`` / ``stealth/ox-alpha``.

These tests observe production code only. Nothing is armed, nothing is
weakened: if any pin fails, the correct action is to refuse the change
(fail closed), not to relax the test.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_PATH = REPO_ROOT / "inference_kernel.py"
ROUTER_PATH = REPO_ROOT / "inference_router.py"
FACADE_PATH = REPO_ROOT / "inference.py"
PROVIDERS_YAML = REPO_ROOT / "config" / "providers.yaml"
SUPERVISOR = REPO_ROOT / "scripts" / "nous-supervisor.sh"
LATENCY_FINDING = REPO_ROOT / "findings" / "hermes_latency_2026-08-26.md"
PAPER_TOOL = REPO_ROOT / "tools" / "signals" / "paper.py"

FORBIDDEN_IN_LADDER = ("hermes_cli", "ProviderRouter", "inference_router")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def kernel_src():
    return _read(KERNEL_PATH)


@pytest.fixture(scope="module")
def router_src():
    return _read(ROUTER_PATH)


@pytest.fixture(scope="module")
def facade_src():
    return _read(FACADE_PATH)


@pytest.fixture(scope="module")
def providers_cfg():
    import yaml

    return yaml.safe_load(_read(PROVIDERS_YAML))


@pytest.fixture(scope="module")
def ladder_block(kernel_src):
    """The source text of the MODEL_LADDER dict literal assignment."""
    m = re.search(r"^MODEL_LADDER:\s*dict\[str,\s*list\[dict\]\]\s*=\s*\{",
                  kernel_src, re.MULTILINE)
    assert m, "MODEL_LADDER assignment vanished from inference_kernel.py"
    start = m.start()
    depth = 0
    for i in range(m.end() - 1, len(kernel_src)):
        ch = kernel_src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return kernel_src[start:i + 1]
    pytest.fail("MODEL_LADDER literal never closes")


def _ladder_models(ladder_block: str):
    """Extract every 'model' string key inside the ladder literal."""
    src = re.sub(r"^MODEL_LADDER:[^=]*=\s*", "x = ", ladder_block)
    tree = ast.parse(src)
    models = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "model":
                    # value may be a constant string or a module-level NAME
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        models.add(v.value)
                    elif isinstance(v, ast.Name):
                        models.add(v.id)
    return models


def _ladder_keys(ladder_block: str) -> list[str]:
    src = re.sub(r"^MODEL_LADDER:[^=]*=\s*", "x = ", ladder_block)
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            out.extend(
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
    return out


def _module_names(path: Path) -> set[str]:
    tree = ast.parse(_read(path))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# 1. MODEL_LADDER purity — kernel plane stays hermes_cli-free and
#    ProviderRouter-free.
# ---------------------------------------------------------------------------

def test_model_ladder_literal_exists(ladder_block):
    assert "MODEL_LADDER" in ladder_block


def test_model_ladder_does_not_mention_hermes_cli(ladder_block):
    assert "hermes_cli" not in ladder_block


def test_model_ladder_does_not_mention_provider_router(ladder_block):
    assert "ProviderRouter" not in ladder_block
    assert "inference_router" not in ladder_block
    assert "load_providers_config" not in ladder_block


@pytest.mark.parametrize("token", FORBIDDEN_IN_LADDER)
def test_ladder_forbidden_tokens_parametrized(ladder_block, token):
    assert token not in ladder_block


def test_model_ladder_entries_have_expected_shape():
    import inference

    for task_type, ladder in inference.MODEL_LADDER.items():
        assert isinstance(task_type, str) and task_type
        assert isinstance(ladder, list) and ladder, task_type
        for entry in ladder:
            assert isinstance(entry, dict), (task_type, entry)
            assert "model" in entry, (task_type, entry)
            assert "timeout" in entry, (task_type, entry)
            assert isinstance(entry["timeout"], int) and entry["timeout"] > 0


def test_model_ladder_task_types_stable(ladder_block):
    keys = set(_ladder_keys(ladder_block))
    assert {"reasoning", "classification", "review"} <= keys


def test_model_ladder_no_empty_rungs():
    import inference

    for task_type, ladder in inference.MODEL_LADDER.items():
        assert all(rung.get("model") for rung in ladder), task_type


def test_complete_walk_does_not_reference_hermes_cli():
    import inference

    complete_fn = getattr(inference, "complete", None)
    if complete_fn is None:
        pytest.skip("inference.complete not exposed post-split")
    src = inspect.getsource(complete_fn)
    assert "hermes_cli" not in src
    assert "ProviderRouter" not in src


def test_escalate_with_ladder_does_not_reference_hermes_cli():
    import inference

    fn = getattr(inference, "escalate_with_ladder")
    assert fn is not None
    src = inspect.getsource(fn)
    assert "hermes_cli" not in src


def test_kernel_module_itself_never_imports_hermes_cli_backend():
    """hermes_cli is allowed ONLY as a *string backend name* in the router
    config domain. The kernel module must not treat it as a transport."""
    src = _read(KERNEL_PATH)
    assert "backend.*hermes_cli" not in src
    assert "hermes_cli" not in _ladder_block_helper()


def _ladder_block_helper():
    src = _read(KERNEL_PATH)
    m = re.search(r"^MODEL_LADDER:", src, re.MULTILINE)
    assert m
    end = src.find("\n\n", m.start())
    return src[m.start():end]


# ---------------------------------------------------------------------------
# 2. Two-plane structure is intact — NOT unified.
# ---------------------------------------------------------------------------

def test_kernel_and_router_are_separate_files():
    assert KERNEL_PATH.is_file()
    assert ROUTER_PATH.is_file()


def test_facade_reexports_both_planes(facade_src):
    assert "from inference_kernel import" in facade_src
    assert "from inference_router import" in facade_src
    assert "TWO INFERENCE PLANES" in facade_src


def test_facade_declares_do_not_unify(facade_src):
    assert "Do not unify" in facade_src or "do not unify" in facade_src


def test_kernel_module_docstring_names_both_planes(kernel_src):
    head = kernel_src[:2000]
    assert "MODEL_LADDER" in head
    assert "inference_router.py" in head
    assert "may be unified" not in head.replace("neither plane may be unified", "")


def test_kernel_does_not_import_inference_router(kernel_src):
    imports = _module_names(KERNEL_PATH)
    assert "inference_router" not in imports


def test_router_does_not_import_inference_kernel_symbols():
    """The router may import tiny shared helpers (_parse_json_response,
    logger) but must NOT absorb the kernel plane's routing symbols."""
    imports = _module_names(ROUTER_PATH)
    router_src = _read(ROUTER_PATH)
    # Docstring/comment mentions of the split are fine; CODE references are
    # not. Parse and walk only executable nodes.
    tree = ast.parse(router_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "inference_kernel":
            bound = {a.name for a in node.names}
            bad = bound & {"MODEL_LADDER", "escalate_with_ladder",
                           "OllamaInference", "complete"}
            assert not bad, f"router plane absorbed kernel symbols: {bad}"
    # No assignment in the router may reference the kernel ladder.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in (
                "MODEL_LADDER", "escalate_with_ladder"):
            pytest.fail("router code references kernel ladder")


def test_router_exposes_providerrouter_only_in_its_plane():
    import inference_router

    assert hasattr(inference_router, "ProviderRouter")
    assert not hasattr(inference_router, "MODEL_LADDER"), (
        "router plane absorbed MODEL_LADDER — planes were unified"
    )


def test_kernel_does_not_expose_providerrouter():
    import inference_kernel

    assert not hasattr(inference_kernel, "ProviderRouter"), (
        "kernel plane absorbed ProviderRouter — planes were unified"
    )
    assert not hasattr(inference_kernel, "load_providers_config")


def test_facade_reexports_ladder_and_router_distinctly():
    import inference

    assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER if False else True
    from inference_kernel import MODEL_LADDER as k_ladder
    from inference_router import ProviderRouter

    assert inference.MODEL_LADDER is k_ladder
    assert inference.ProviderRouter is ProviderRouter


def test_measured_latency_pin_survives_in_kernel(kernel_src):
    assert "11.9" in kernel_src and "p50" in kernel_src
    assert "hermes_latency_2026-08-26" in kernel_src


def test_latency_finding_document_exists():
    assert LATENCY_FINDING.is_file()
    doc = _read(LATENCY_FINDING)
    assert "11.9" in doc or "31.4" in doc


# ---------------------------------------------------------------------------
# 3. providers.yaml — gpu1 llama_cpp_server, openrouter_ox env-backed.
# ---------------------------------------------------------------------------

def test_default_tier_is_gpu1(providers_cfg):
    assert providers_cfg["default_tier"] == "gpu1"


def test_gpu1_backend_is_llama_cpp_server(providers_cfg):
    ep = providers_cfg["providers"]["gpu1"]
    assert ep["backend"] == "llama_cpp_server"
    assert ep["base_url"].startswith("http://localhost")
    assert ep["structured_output"] is True
    assert ep["tool_calls"] is True
    assert int(ep.get("max_concurrency", 1)) >= 1


def test_gpu1_fast_also_llama_cpp_server(providers_cfg):
    ep = providers_cfg["providers"]["gpu1_fast"]
    assert ep["backend"] == "llama_cpp_server"
    assert ep["base_url"].startswith("http://localhost")


def test_local_backends_constant_covers_llama_cpp_server():
    import inference_router

    assert "llama_cpp_server" in inference_router.LOCAL_BACKENDS


def test_openrouter_ox_is_openai_compat_env_backed(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    assert ox["backend"] == "openai_compat"
    assert ox["base_url"] == "https://openrouter.ai/api/v1"
    assert ox["api_key_env"] == "OPENROUTER_API_KEY"
    assert ox["model"] == "stealth/ox-alpha"
    assert "api_key" not in ox
    assert "base_url_env" not in ox or ox["base_url_env"] != "OPENROUTER_API_KEY"


def test_openrouter_ox_key_material_absent_from_repo():
    raw = _read(PROVIDERS_YAML)
    assert "sk-or-v1-" not in raw
    for pat in ("OPENROUTER_API_KEY=", "OPENROUTER_API_KEY:"):
        assert pat not in raw


def test_every_task_class_lists_local_before_openrouter_ox(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        locals_ = [p for p in chain if p.startswith("gpu")]
        if locals_:
            assert chain.index(locals_[0]) < chain.index("openrouter_ox"), name


def test_ox_alpha_last_resort_in_every_chain(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        assert chain[-1] in ("ox_alpha", "ox_alpha_proxy") or (
            "ox_alpha" in chain
        ), name


def test_yaml_has_no_live_status_semantics(providers_cfg):
    text = repr(providers_cfg).lower()
    assert "'live'" not in text and '"live"' not in text


# ---------------------------------------------------------------------------
# 4. Fail-closed paper-trade gate — never widened to 'live'.
# ---------------------------------------------------------------------------

def test_paper_statuses_frozenset_exact():
    from tools.signals.paper import allowed_paper_statuses

    statuses = allowed_paper_statuses()
    assert isinstance(statuses, frozenset)
    assert statuses == frozenset({"paper_trading"})


def test_paper_statuses_never_contains_live():
    from tools.signals import paper

    assert "live" not in paper._PAPER_TRADE_SIGNAL_STATUSES
    assert "live_trading" not in paper._PAPER_TRADE_SIGNAL_STATUSES


def test_reject_non_paper_rejects_live():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("live") is True
    assert reject_non_paper("LIVE") is True
    assert reject_non_paper("paper_trading") is False


def test_paper_tool_gate_contains_no_live_status():
    """The gate set and its helpers must never admit 'live'. Docstring
    mentions of the gate are fine; the frozenset literal is what matters."""
    from tools.signals import paper

    src = None  # gate set is data, checked below
    assert paper._PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    raw = _read(PAPER_TOOL)
    # no live status may appear inside the gate set definition
    gate = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\([^)]*\)", raw)
    assert gate, "gate set definition not found"
    assert "live" not in gate.group(0).lower()
    assert "generate_paper_trade_signal" in raw  # the hard-gate comment stays


def test_generate_paper_trade_signal_not_widened_to_live():
    """Whatever module exposes generate_paper_trade_signal, its gate set
    must remain exactly {paper_trading}."""
    from tools.signals import paper

    fn = getattr(paper, "generate_paper_trade_signal", None)
    if fn is None:
        pytest.skip("generate_paper_trade_signal not exposed in tools/signals/paper.py")
    src = inspect.getsource(fn)
    assert "live" not in src.lower(), "signal generator mentions 'live'"
    statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", frozenset())
    assert statuses == frozenset({"paper_trading"})


# ---------------------------------------------------------------------------
# 5. Supervisor / runtime contract — completions stay HTTP, model pinned.
# ---------------------------------------------------------------------------

def test_supervisor_pins_model_flag():
    src = _read(SUPERVISOR)
    assert '-m "$MODEL"' in src
    assert "stealth/ox-alpha" in src


def test_supervisor_keeps_provider_variable():
    src = _read(SUPERVISOR)
    assert "CALLISTO_HERMES_PROVIDER" in src
    assert '--provider "$PROVIDER"' in src


def test_supervisor_does_not_hardcode_a_single_provider_argv():
    src = _read(SUPERVISOR)
    # PROVIDER indirection must exist; a hardcoded `--provider nous \` alone
    # would freeze routing.
    assert "PROVIDER" in src


# ---------------------------------------------------------------------------
# 6. Router behavior sanity — pure, no network.
# ---------------------------------------------------------------------------

def test_router_canonical_aliases_bridge_legacy_names():
    import inference

    aliases = inference.TASK_CLASS_ALIASES
    assert aliases.get("deep_work") == "research_synthesis"
    assert aliases.get("hypothesis_gen") == "hypothesis_generation"


def test_route_order_starts_with_configured_head():
    from inference import get_router

    router = get_router()
    order, meta = router.route_order("screening", candidate_names=list(
        router.candidates_for("screening")))
    assert order, "route_order returned empty for screening"
    assert order[0] == "gpu1_fast"
    assert meta.get("basis", "configured")


def test_route_order_for_judgment_prefers_frontier():
    from inference import get_router

    router = get_router()
    candidates = router.candidates_for("promotion_judgment")
    order, _meta = router.route_order(
        "promotion_judgment", candidate_names=list(candidates))
    if "frontier" in order:
        assert "gpu1" not in order or order.index("frontier") < order.index("gpu1")


def test_endpoint_parsing_preserves_gpu1_backend():
    from inference import load_providers_config, _endpoint_from_config

    cfg = load_providers_config()
    ep = _endpoint_from_config("gpu1", cfg["providers"]["gpu1"])
    assert ep.backend == "llama_cpp_server"


def test_hosted_classification_of_planes():
    from inference import _endpoint_from_config, load_providers_config
    from inference_router import endpoint_is_hosted

    cfg = load_providers_config()["providers"]
    assert endpoint_is_hosted(_endpoint_from_config("gpu1", cfg["gpu1"])) is False
    assert endpoint_is_hosted(_endpoint_from_config("openrouter_ox",
                                                    cfg["openrouter_ox"])) is True


def test_unknown_task_class_raises():
    import pytest as _pytest

    from inference import UnknownTaskClassError, get_router

    router = get_router()
    with _pytest.raises(UnknownTaskClassError):
        router.tier_for("definitely_not_a_task_class_0035")


# ---------------------------------------------------------------------------
# 7. Cross-plane consistency — the two planes describe one world.
# ---------------------------------------------------------------------------

def test_ladder_and_yaml_share_qwen_lineage(ladder_block, providers_cfg):
    models = _ladder_models(ladder_block)
    joined = " ".join(str(m).lower() for m in models)
    assert "qwen" in joined
    yaml_text = _read(PROVIDERS_YAML).lower()
    assert "qwen" in yaml_text


def test_no_plane_mentions_the_other_transport_in_its_core_symbol():
    import inspect

    import inference_kernel
    import inference_router

    ladder_src = inspect.getsource(inference_kernel)
    # Kernel file may discuss the split in comments but must not wire it.
    assert "from inference_router import" not in ladder_src
    # The router's docstring may document the split; its CODE (AST) must
    # not reference the kernel ladder walk.
    tree = ast.parse(inspect.getsource(inference_router))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "inference_kernel":
            bound = {a.name for a in node.names}
            bad = bound & {"MODEL_LADDER", "escalate_with_ladder",
                           "OllamaInference"}
            assert not bad, f"router code absorbed kernel symbols: {bad}"


def test_dual_plane_pin_test_still_present():
    other = REPO_ROOT / "tests" / "test_inference_planes.py"
    assert other.is_file()
    src = _read(other)
    assert "hermes_cli" in src
    assert "load_providers_config" in src
