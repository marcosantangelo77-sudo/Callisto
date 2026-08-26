"""Pin: ProviderRouter internals live in tools.infrouter.

Does NOT import tools.autonomous (that module hangs this environment).
Does NOT arm live betting. Does NOT add live to paper-signal.
Does NOT point MODEL_LADDER at ProviderRouter.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "inference_router.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"
INFROUTER = ROOT / "tools" / "infrouter"

MOVED_MODULES = (
    INFROUTER / "config.py",
    INFROUTER / "retry.py",
    INFROUTER / "local_only.py",
    INFROUTER / "state.py",
    INFROUTER / "empirical.py",
)

# Names that must remain importable from inference_router (and be the
# same object as tools.infrouter).
REEXPORTS = (
    "TASK_CLASS_ALIASES",
    "EndpointConfig",
    "EscalationConfig",
    "TierConfig",
    "UnknownTaskClassError",
    "_PROVIDERS_CONFIG_PATH",
    "_endpoint_from_config",
    "load_providers_config",
    "LOCAL_BACKENDS",
    "endpoint_is_hosted",
    "local_only_enabled",
    "strip_hosted_for_local_only",
    "_429_DEFAULT_BACKOFF_S",
    "_429_MAX_TOTAL_WAIT_S",
    "_post_with_retry",
    "_retry_after_seconds",
    "CostLedger",
    "_EndpointState",
)


def _top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _imports_autonomous(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if "autonomous" in a.name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if "autonomous" in (node.module or ""):
                return True
    return False


def test_infrouter_package_exists():
    assert (INFROUTER / "__init__.py").is_file()
    for p in MOVED_MODULES:
        assert p.is_file(), p


def test_moved_defs_live_in_infrouter_not_facade():
    facade = _top_level_names(FACADE)
    assert "ProviderRouter" in facade
    assert "get_router" in facade
    assert "_post_with_retry" not in facade
    assert "load_providers_config" not in facade
    assert "strip_hosted_for_local_only" not in facade
    assert "CostLedger" not in facade
    assert "EmpiricalRoutingMixin" not in facade
    assert "_retry_after_seconds" in _top_level_names(INFROUTER / "retry.py")
    assert "load_providers_config" in _top_level_names(INFROUTER / "config.py")
    assert "strip_hosted_for_local_only" in _top_level_names(
        INFROUTER / "local_only.py")
    assert "CostLedger" in _top_level_names(INFROUTER / "state.py")
    assert "EmpiricalRoutingMixin" in _top_level_names(
        INFROUTER / "empirical.py")


def test_facade_reexports_same_objects():
    import inference_router
    import tools.infrouter as infrouter

    for name in REEXPORTS:
        assert getattr(inference_router, name) is getattr(infrouter, name), name


def test_facade_line_count_dropped():
    n = FACADE.read_text(encoding="utf-8").count("\n")
    assert n < 700, n
    moved = sum(p.read_text(encoding="utf-8").count("\n") for p in MOVED_MODULES)
    assert moved >= 400, moved


def test_neither_module_imports_autonomous():
    paths = [FACADE, INFROUTER / "__init__.py", *MOVED_MODULES]
    for path in paths:
        assert not _imports_autonomous(path), path


def test_local_only_strip_before_availability_in_candidates_for():
    """CALLISTO_LOCAL_ONLY strip must run in candidates_for before health."""
    src = ast.parse(FACADE.read_text(encoding="utf-8"))
    fn = next(
        n for n in src.body
        if isinstance(n, ast.ClassDef) and n.name == "ProviderRouter"
    )
    method = next(
        n for n in fn.body
        if isinstance(n, ast.FunctionDef) and n.name == "candidates_for"
    )
    dump = ast.dump(method)
    strip_pos = dump.find("strip_hosted_for_local_only")
    avail_pos = dump.find("available")
    assert strip_pos != -1
    assert avail_pos != -1
    assert strip_pos < avail_pos


def test_complete_still_uses_candidates_for_not_raw_hosted_list():
    """complete() must go through candidates_for (which strips hosted)."""
    src = ast.parse(FACADE.read_text(encoding="utf-8"))
    cls = next(n for n in src.body if isinstance(n, ast.ClassDef)
               and n.name == "ProviderRouter")
    method = next(
        n for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "complete"
    )
    dump = ast.dump(method)
    assert "candidates_for" in dump
    assert "hermes_cli" in dump  # last-resort CLI tier stays quarantined here


def test_planes_not_unified_in_infrouter():
    for path in MOVED_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "MODEL_LADDER", path
            elif isinstance(node, ast.Attribute):
                assert node.attr != "MODEL_LADDER", path


def test_paper_signal_statuses_unchanged():
    src = PAPER.read_text(encoding="utf-8")
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    tree = ast.parse(src)
    assigned = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES":
                    assigned = node.value
    assert assigned is not None
    dump = ast.dump(assigned)
    assert "paper_trading" in dump
    assert "live" not in dump


def test_facade_still_mentions_two_planes():
    src = FACADE.read_text(encoding="utf-8")
    assert "TWO INFERENCE PLANES" in src
    assert "Do NOT unify" in src
    assert "hermes_latency_2026-08-26.md" in src
    assert "hermes_cli" in src
