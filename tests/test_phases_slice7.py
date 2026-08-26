"""Pin: cadence/wiki/regime helpers moved into tools.loop.phases.shared.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. phase_live_execute stays in phases_impl
with CALLISTO_ALLOW_LIVE_EXECUTE as the first executable after
self=loop / docstring / import os.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools" / "loop" / "phases_impl.py"
SHARED = ROOT / "tools" / "loop" / "phases" / "shared.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

HELPER_NAMES = (
    "BACKTEST_BATCH_SIZE",
    "BACKTEST_GAP_DAYS",
    "CLAUDE_ESCALATION_COOLDOWN",
    "DATA_COLLECTION_INTERVAL",
    "DEFAULT_TRAINING_WINDOW_DAYS",
    "HYPOTHESIS_GEN_INTERVAL",
    "MAX_EDGE_THRESHOLD_CEILING",
    "MIN_EDGE_THRESHOLD_FLOOR",
    "MIN_GAMES_FOR_HYPOTHESIS",
    "REGIME_ANALYSIS_INTERVAL",
    "RESEARCH_CYCLE_INTERVAL",
    "RESEARCH_SPORTS",
    "SPORT_PRIORITY",
    "SYSTEM_IMPROVEMENT_INTERVAL",
    "_LRUCache",
    "_fetch_wiki_priors",
    "_regime_cache",
    "_render_wiki_priors_block",
    "_wiki_in_loop_enabled",
    "get_regime_for_team",
    "logger",
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
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
    return names


def _async_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def test_helpers_live_in_shared_not_defined_in_phases_impl():
    shared_names = _top_level_names(SHARED)
    impl_tree = ast.parse(IMPL.read_text(encoding="utf-8"))
    impl_defined = set()
    for n in impl_tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            impl_defined.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    impl_defined.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            impl_defined.add(n.target.id)
    for name in HELPER_NAMES:
        if name == "logger":
            continue
        assert name in shared_names, name
        assert name not in impl_defined, name
    leftover = {n for n in _async_defs(IMPL) if n.startswith("phase_")}
    assert leftover == {"phase_live_execute"}, leftover
    assert "phase_live_execute" not in _async_defs(SHARED)


def test_phases_impl_reexports_shared_helpers_as_same_objects():
    from tools.loop import phases_impl
    from tools.loop.phases import shared

    for name in HELPER_NAMES:
        assert getattr(phases_impl, name) is getattr(shared, name), name
    assert phases_impl.phase_live_execute.__module__ == "tools.loop.phases_impl"
    assert shared._fetch_wiki_priors.__module__ == "tools.loop.phases.shared"
    assert shared.get_regime_for_team.__module__ == "tools.loop.phases.shared"


def test_phases_impl_is_now_reexports_plus_live_execute():
    n = IMPL.read_text(encoding="utf-8").count("\n")
    assert n < 360, n
    shared_n = SHARED.read_text(encoding="utf-8").count("\n")
    assert shared_n >= 150, shared_n


def test_neither_shared_nor_impl_imports_autonomous_or_cycle():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        IMPL, SHARED, pkg / "__init__.py", pkg / "repair.py",
        pkg / "backtest_run.py", pkg / "collect_eval.py", pkg / "hypgen.py",
        pkg / "pre_live.py", pkg / "post_live.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "autonomous" not in mod, path
    shared_tree = ast.parse(SHARED.read_text(encoding="utf-8"))
    for node in ast.walk(shared_tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod != "tools.loop.phases_impl", "shared must not import phases_impl"
            assert not mod.startswith("tools.loop.phases.") or mod == "tools.loop.phases.shared", mod
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "tools.loop.phases_impl"
                assert not a.name.startswith("tools.loop.phases.")


def test_paper_signal_still_paper_trading_only():
    src = PAPER.read_text(encoding="utf-8")
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    assigned = None
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES":
                    assigned = node.value
    assert assigned is not None
    dump = ast.dump(assigned)
    assert "paper_trading" in dump
    assert "live" not in dump


def test_live_execute_gate_untouched():
    tree = ast.parse(IMPL.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "phase_live_execute"
    )
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "self" for t in body[0].targets)
    ):
        body = body[1:]
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if body and isinstance(body[0], ast.Import):
        body = body[1:]
    assert body and isinstance(body[0], ast.If)
    dump = ast.dump(body[0].test)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dump
    assert "1" in dump


def test_runtime_gate_closed_and_regime_cache_identity():
    import asyncio
    from tools.loop import phases_impl
    from tools.loop.phases import shared

    class _Loop:
        hypothesis_manager = object()

    assert asyncio.run(phases_impl.phase_live_execute(_Loop())) is None
    assert phases_impl._regime_cache is shared._regime_cache
    assert isinstance(phases_impl.RESEARCH_SPORTS, list)
    assert "basketball_nba" in phases_impl.RESEARCH_SPORTS
