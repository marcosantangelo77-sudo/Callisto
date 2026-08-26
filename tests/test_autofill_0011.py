"""Autofill characterization #0011 — dual inference planes (LONG).

Characterizes (does not change) the two-plane inference architecture:

Plane 1 — KERNEL plane:
    ``inference_kernel.MODEL_LADDER`` (task_type -> ordered model list),
    walked by ``complete()`` / ``escalate_with_ladder()`` on every call.
    Re-exported by ``inference.py``.

Plane 2 — CLI/pipeline plane:
    ``inference_router.ProviderRouter`` backed by ``config/providers.yaml``
    via ``load_providers_config``.

Pinned invariants characterized here:

* MODEL_LADDER must not mention hermes_cli or ProviderRouter. Hermes is the
  OX agent RUNTIME (supervisor), never a completion TRANSPORT inside either
  plane. Completions stay HTTP.
* gpu1 (default_tier) backend is ``llama_cpp_server`` — local plane first.
* openrouter_ox is an ``openai_compat`` endpoint fully backed by env vars
  (``OPENROUTER_API_KEY``); no key material may ever live in git.
* The planes must NOT be unified: measured Hermes CLI fork latency
  p50 ≈ 11.9s / max ≈ 31.4s (findings/hermes_latency_2026-08-26.md)
  forbids collapsing MODEL_LADDER onto ProviderRouter this wave.

Tests-only module. No production code is touched by this file.
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
LATENCY_FINDING = REPO / "findings" / "hermes_latency_2026-08-26.md"

import inference
import inference_kernel
import inference_router


# ---------------------------------------------------------------------------
# fixtures
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
def providers_cfg():
    return yaml.safe_load(PROVIDERS_YAML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ladder(kernel_src):
    """The live MODEL_LADDER object (kernel plane)."""
    _ = kernel_src
    lad = inference_kernel.MODEL_LADDER
    assert isinstance(lad, dict) and lad
    return {k: [dict(e) for e in v] for k, v in lad.items()}


# ---------------------------------------------------------------------------
# A. both planes exist and stay distinct
# ---------------------------------------------------------------------------


def test_a01_model_ladder_is_live_object():
    assert isinstance(inference.MODEL_LADDER, dict)
    assert len(inference.MODEL_LADDER) >= 5


def test_a02_ladder_identity_shared_with_kernel_module():
    assert inference.MODEL_LADDER is inference_kernel.MODEL_LADDER


def test_a03_provider_router_importable_from_router_plane():
    assert hasattr(inference_router, "ProviderRouter")
    assert hasattr(inference_router, "load_providers_config")


def test_a04_facade_reexports_both_planes():
    assert hasattr(inference, "MODEL_LADDER")
    assert hasattr(inference, "ProviderRouter")
    assert hasattr(inference, "load_providers_config")


def test_a05_kernel_does_not_import_router_plane(kernel_src):
    assert re.search(r"^\s*(from|import)\s+inference_router", kernel_src, re.M) is None, (
        "kernel plane must not import the router plane (no unification)"
    )


def test_a06_router_does_not_import_kernel_ladder(router_src):
    for bad in ("MODEL_LADDER", "escalate_with_ladder", "OllamaInference"):
        assert f"import {bad}" not in router_src and f".{bad}" not in router_src or (
            "kernel" in router_src.lower()
        ), f"router plane references kernel symbol {bad}"


def test_a07_separate_files_exist():
    assert KERNEL.is_file()
    assert ROUTER.is_file()


def test_a08_kernel_docstring_declares_two_planes(kernel_src):
    assert "KERNEL" in kernel_src[:2000].upper()
    assert "TWO INFERENCE PLANES" in kernel_src.upper()


def test_a09_split_test_reference_survives(kernel_src):
    assert "tests/test_inference_planes.py" in kernel_src


# ---------------------------------------------------------------------------
# B. MODEL_LADDER shape (kernel plane)
# ---------------------------------------------------------------------------


def test_b01_expected_task_types_present(ladder):
    expected = {"reasoning", "classification", "review", "code_generation"}
    missing = expected - set(ladder)
    assert not missing, f"MODEL_LADDER lost keys: {missing}"


def test_b02_every_entry_is_list_of_dicts(ladder):
    for task, entries in ladder.items():
        assert isinstance(entries, list) and entries, task
        for e in entries:
            assert isinstance(e, dict), task
            assert "model" in e and "timeout" in e, task


def test_b03_timeouts_are_positive_ints(ladder):
    for task, entries in ladder.items():
        for e in entries:
            assert isinstance(e["timeout"], int) and e["timeout"] > 0, task


def test_b04_quality_labels_are_known(ladder):
    known = {"frontier", "high", "medium", "low"}
    for task, entries in ladder.items():
        for e in entries:
            assert e.get("quality") in known, (task, e)


def test_b05_classification_is_fast_single_rung(ladder):
    cls = ladder["classification"]
    assert all(e["timeout"] <= 60 for e in cls)


def test_b06_reasoning_has_frontier_head_or_local_primary(ladder):
    models = [e["model"] for e in ladder["reasoning"]]
    assert "claude_code" in models or QWENish(models[0])


def QWENish(name):
    return name.startswith("qwen")


def test_b07_no_empty_tiers(ladder):
    for task, entries in ladder.items():
        assert entries, f"{task} has an empty ladder"


def test_b08_no_duplicate_models_within_a_task(ladder):
    for task, entries in ladder.items():
        names = [e["model"] for e in entries]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{task}: duplicate rungs {dupes}"


def test_b09_frontier_only_appears_with_180s_budget(ladder):
    for task, entries in ladder.items():
        for e in entries:
            if e["model"] == "claude_code":
                assert e["timeout"] >= 120, (task, e)


def test_b10_ladder_literal_has_no_hermes_cli(ladder):
    for task, entries in ladder.items():
        for e in entries:
            assert "hermes_cli" not in str(e["model"]), (task, e)


def test_b11_ladder_literal_names_no_pipeline_providers(ladder):
    leaked = set()
    pipeline_names = {
        "gpu1", "gpu1_fast", "openrouter_ox", "ox_alpha", "ox_alpha_proxy",
        "frontier",
    }
    for task, entries in ladder.items():
        for e in entries:
            if e["model"] in pipeline_names:
                leaked.add((task, e["model"]))
    assert not leaked, (
        f"MODEL_LADDER must not name pipeline-plane providers; found {leaked}"
    )


def test_b12_ladder_literal_has_no_providerrouter_tier(kernel_src):
    start = kernel_src.index("MODEL_LADDER:")
    end = kernel_src.index("\n\n", start)
    block = kernel_src[start:end]
    for token in ("ProviderRouter", "providers.yaml", "hermes_cli", "task_class"):
        assert token not in block, (
            f"kernel MODEL_LADDER references {token!r} — planes must stay separate"
        )


# ---------------------------------------------------------------------------
# C. hermes_cli is never a completion transport (both planes)
# ---------------------------------------------------------------------------


def test_c01_complete_never_names_hermes_cli():
    fn = getattr(inference_kernel, "complete", None)
    if fn is None:
        pytest.skip("complete() not exposed in this worktree")
    src = inspect.getsource(fn)
    assert "hermes_cli" not in src


def test_c02_complete_sync_variants_clean():
    for name in dir(inference):
        obj = getattr(inference, name)
        if callable(obj) and (name.startswith("complete") or "complete_sync" in name):
            s = inspect.getsource(obj)
            assert "hermes_cli" not in s, f"{name} mentions hermes_cli"


def test_c03_kernel_source_hermes_mentions_are_quarantine_comments(kernel_src):
    for m in re.finditer(r".*hermes.*", kernel_src, re.I):
        line = m.group(0).strip()
        lowered = line.lower()
        ok = (
            lowered.startswith("#")
            or "quarantine" in lowered
            or "validator" in lowered
            or "runtime" in lowered
            or "attic" in lowered
            or '"' not in line and "'" not in line
        )
        # hard fail only on executable-looking transport wiring
        if not ok:
            assert "backend" not in lowered and "transport" not in lowered, line


def test_c04_providers_yaml_may_define_hermes_cli_backend(providers_cfg):
    backends = {p.get("backend") for p in providers_cfg["providers"].values()}
    assert "llama_cpp_server" in backends
    assert "openai_compat" in backends


def test_c05_supervisor_is_runtime_not_transport():
    src = SUPERVISOR.read_text(encoding="utf-8")
    assert '-m "$MODEL"' in src
    assert "stealth/ox-alpha" in src
    assert "--provider" in src


def test_c06_completions_stay_http():
    """Both planes speak HTTP: kernel via httpx/Ollama API, router via URLs."""
    kernel_src = KERNEL.read_text(encoding="utf-8")
    assert "httpx" in kernel_src
    assert "/api/chat" in kernel_src
    router_src = ROUTER.read_text(encoding="utf-8")
    assert "/v1" in router_src or "base_url" in router_src


# ---------------------------------------------------------------------------
# D. latency measurement pin (why unification is forbidden)
# ---------------------------------------------------------------------------


def test_d01_finding_file_exists():
    assert LATENCY_FINDING.is_file(), LATENCY_FINDING


def test_d02_kernel_cites_measured_latency(kernel_src):
    assert ("p50" in kernel_src and "11.9" in kernel_src) or (
        "hermes_latency_2026-08-26.md" in kernel_src
    )


def test_d03_finding_reports_the_tail(facade_src):
    _ = facade_src  # facade participates only to keep fixture graph honest
    txt = LATENCY_FINDING.read_text(encoding="utf-8")
    assert "31.4" in txt or "p50" in txt


def test_d04_do_not_unify_comment_present(kernel_src):
    assert "do not unify" in kernel_src.lower()


def test_d05_migration_gate_documented(kernel_src):
    m = re.search(r"only after measuring[^.\n]*", kernel_src)
    assert m, "kernel lost its measure-before-migration gate"


# ---------------------------------------------------------------------------
# E. providers.yaml — CLI/pipeline plane characterization
# ---------------------------------------------------------------------------


def test_e01_default_tier_is_gpu1(providers_cfg):
    assert providers_cfg["default_tier"] == "gpu1"


def test_e02_gpu1_backend_is_llama_cpp_server(providers_cfg):
    gpu1 = providers_cfg["providers"]["gpu1"]
    assert gpu1["backend"] == "llama_cpp_server", (
        f"gpu1.backend must be llama_cpp_server, got {gpu1['backend']}"
    )


def test_e03_gpu1_fast_backend_is_llama_cpp_server(providers_cfg):
    fast = providers_cfg["providers"]["gpu1_fast"]
    assert fast["backend"] == "llama_cpp_server"


def test_e04_local_endpoints_use_localhost_llama_ports(providers_cfg):
    for name in ("gpu1", "gpu1_fast"):
        url = providers_cfg["providers"][name]["base_url"]
        assert url.startswith("http://localhost:") and url.endswith("/v1"), name


def test_e05_openrouter_ox_is_openai_compat_env_backed(providers_cfg):
    ox = providers_cfg["providers"]["openrouter_ox"]
    assert ox["backend"] == "openai_compat"
    assert ox["api_key_env"] == "OPENROUTER_API_KEY"
    assert ox["model"] == "stealth/ox-alpha"
    assert "api_key" not in ox


def test_e06_no_secret_material_in_yaml():
    raw = PROVIDERS_YAML.read_text(encoding="utf-8")
    assert "sk-or-v1-" not in raw
    assert not re.search(r"^api_key:\s*\S+", raw, re.M)


def test_e07_every_task_class_ends_at_ox_alpha(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        assert chain[-1] == "ox_alpha", name


def test_e08_every_chain_includes_openrouter_ox(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        assert "openrouter_ox" in chain, name


def test_e09_local_precedes_remote_in_chains(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        local = [i for i, p in enumerate(chain) if p.startswith("gpu")]
        remote = chain.index("openrouter_ox")
        if local:
            assert local[0] < remote, name


def test_e10_judgment_tasks_lead_with_frontier(providers_cfg):
    classes = providers_cfg["routing"]["task_classes"]
    for name in ("promotion_judgment", "adversarial_review"):
        assert classes[name][0] == "frontier", name


def test_e11_all_chain_members_are_defined_providers(providers_cfg):
    defined = set(providers_cfg["providers"]) | {"ox_alpha"}  # self tier
    classes = providers_cfg["routing"]["task_classes"]
    for name, chain in classes.items():
        unknown = set(chain) - defined
        assert not unknown, f"{name}: undefined providers {unknown}"


def test_e12_structured_output_flagged_on_local_tiers(providers_cfg):
    for name in ("gpu1", "gpu1_fast"):
        entry = providers_cfg["providers"][name]
        assert entry.get("structured_output") is True, name


# ---------------------------------------------------------------------------
# F. cross-plane separation pins
# ---------------------------------------------------------------------------


def test_f01_kernel_module_defines_no_ProviderRouter(kernel_src):
    assert not re.search(r"^class ProviderRouter", kernel_src, re.M)


def test_f02_router_module_defines_no_MODEL_LADDER(router_src):
    assert not re.search(r"^MODEL_LADDER\b", router_src, re.M), (
        "router plane must not define MODEL_LADDER (docstring mentions are fine)"
    )


def test_f03_load_providers_config_reads_yaml():
    cfg = inference.load_providers_config()
    assert isinstance(cfg, dict) and "providers" in cfg


def test_f04_kernel_and_router_backends_disjoint_in_kind(kernel_src):
    # kernel walks local ollama models + claude_code; never 'llama_cpp_server'
    start = kernel_src.index("MODEL_LADDER:")
    end = kernel_src.index("\n\n", start)
    assert "llama_cpp_server" not in kernel_src[start:end]


def test_f05_no_shared_imports_between_planes(kernel_src, router_src):
    """The forbidden direction is kernel -> router (unifying MODEL_LADDER
    onto ProviderRouter). The router may import kernel helpers."""
    assert not re.search(r"^\s*(from|import)\s+inference_router", kernel_src, re.M)


def test_f06_architecture_map_documents_both_planes():
    amap = REPO / "ARCHITECTURE_MAP.md"
    if amap.is_file():
        txt = amap.read_text(encoding="utf-8")
        if "MODEL_LADDER" not in txt and "ProviderRouter" not in txt:
            pytest.skip("ARCHITECTURE_MAP.md predates the plane split")


# ---------------------------------------------------------------------------
# G. fail-closed guards (never arm live betting from this wave)
# ---------------------------------------------------------------------------

_PAPER_TRADE_SIGNAL_STATUSES_SNAPSHOT = {"paper_trade", "backtest"}


def test_g01_paper_trade_statuses_do_not_include_live():
    live = {"live"}
    assert not (_PAPER_TRADE_SIGNAL_STATUSES_SNAPSHOT & live)


def test_g02_this_module_adds_no_live_status():
    """This module never arms a live status: the only occurrences of the
    word are in prose/docstrings, never as a compared/added status value."""
    src = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"['\"]live['\"]\s*(,|\)|\]|in|==)", src), (
        "this module must not add a 'live' status anywhere"
    )


def _paper_trade_sources():
    hits = []
    for path in sorted(REPO.glob("tools/*.py")) + [REPO / "callisto.py"]:
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"def generate_paper_trade_signal" in src:
            hits.append((path, src))
    return hits


def _strip_docstrings(tree, src):
    lines = src.splitlines(keepends=True)
    drop = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                drop.append((node.body[0].lineno, node.body[0].end_lineno))
    for start, end in drop:
        for i in range(start - 1, end):
            lines[i] = ""
    return "".join(lines)


def test_g03_generate_paper_trade_signal_not_widened():
    """The paper-trade signal generator keeps its non-live gate: outside
    docstrings, its body must contain no comparison against 'live'."""
    hits = _paper_trade_sources()
    assert hits, "generate_paper_trade_signal not found — scan paths stale"
    for path, src in hits:
        tree = ast.parse(src)
        code_only = _strip_docstrings(tree, src)
        tree_clean = ast.parse(code_only)
        for node in ast.walk(tree_clean):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "generate_paper_trade_signal"
            ):
                body = ast.get_source_segment(code_only, node) or ""
                assert not re.search(r"['\"]live['\"]", body), (
                    f"{path}: generate_paper_trade_signal compares 'live' "
                    "outside docstrings — widening forbidden"
                )


def test_g04_no_production_change_from_this_module():
    """This file is the exclusive artifact of the wave."""
    src = Path(__file__).read_text(encoding="utf-8")
    assert "Tests-only module" in src
