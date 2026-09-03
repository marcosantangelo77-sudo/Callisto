"""Pin: phase_system_watchdog and phase_integrity_check moved into
tools.loop.phases.post_live_watch.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. phase_live_execute stays in phases_impl
with CALLISTO_ALLOW_LIVE_EXECUTE as the first executable after
self=loop / docstring / import os.

post_live is now a thin facade of async delegates for all post-live
phases. Remaining god-module work is outside this package (loop.py,
api.py lifespan, MaintenanceMixin).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools" / "loop" / "phases_impl.py"
POST = ROOT / "tools" / "loop" / "phases" / "post_live.py"
WATCH = ROOT / "tools" / "loop" / "phases" / "post_live_watch.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"
AUTONOMOUS = ROOT / "tools" / "autonomous.py"


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


def test_post_live_watch_defines_both_async_phases():
    defs = _async_defs(WATCH)
    assert "phase_system_watchdog" in defs
    assert "phase_integrity_check" in defs
    tree = ast.parse(WATCH.read_text(encoding="utf-8"))
    for name in ("phase_system_watchdog", "phase_integrity_check"):
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == name
        )
        assert fn.args.args and fn.args.args[0].arg == "loop"


def test_post_live_still_exposes_names():
    names = _top_level_names(POST)
    assert "phase_system_watchdog" in names
    assert "phase_integrity_check" in names
    tree = ast.parse(POST.read_text(encoding="utf-8"))
    exposed = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name in ("phase_system_watchdog", "phase_integrity_check"):
                exposed.add(n.name)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                nm = a.asname or a.name
                if nm in ("phase_system_watchdog", "phase_integrity_check"):
                    exposed.add(nm)
    assert exposed == {"phase_system_watchdog", "phase_integrity_check"}


def test_neither_post_live_watch_nor_post_live_imports_autonomous():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        WATCH, POST, IMPL, pkg / "__init__.py", pkg / "repair.py",
        pkg / "backtest_run.py", pkg / "collect_eval.py", pkg / "hypgen.py",
        pkg / "pre_live.py", pkg / "shared.py", pkg / "claude_deep.py",
        pkg / "system_improve.py", pkg / "regime_granger.py",
        pkg / "post_live_review.py", pkg / "post_live_wiki.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or ""), path


def test_line_counts():
    post_n = POST.read_text(encoding="utf-8").count("\n")
    watch_n = WATCH.read_text(encoding="utf-8").count("\n")
    assert post_n < 90, post_n
    assert post_n >= 60, post_n
    assert watch_n >= 140, watch_n


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


def test_live_execute_not_introduced_into_post_live_watch():
    src = WATCH.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
    assert "phase_live_execute" not in _async_defs(WATCH)


def test_phase_live_execute_stays_on_master_home():
    impl_defs = _async_defs(IMPL)
    auto_src = AUTONOMOUS.read_text(encoding="utf-8")
    auto_tree = ast.parse(auto_src)
    auto_async = {
        n.name for n in auto_tree.body if isinstance(n, ast.AsyncFunctionDef)
    }
    assert "phase_live_execute" in impl_defs or "phase_live_execute" in auto_async
    assert "phase_live_execute" in impl_defs
    leftover = {n for n in impl_defs if n.startswith("phase_")}
    assert leftover == {"phase_live_execute"}, leftover
    assert "phase_live_execute" not in _async_defs(POST)
    assert "phase_live_execute" not in _async_defs(WATCH)


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


def test_runtime_gate_closed_and_delegate_identity():
    import asyncio
    from tools.loop import phases_impl
    from tools.loop.phases import post_live
    from tools.loop.phases import post_live_watch

    class _Loop:
        hypothesis_manager = object()

    assert asyncio.run(phases_impl.phase_live_execute(_Loop())) is None
    assert phases_impl.phase_system_watchdog is post_live.phase_system_watchdog
    assert phases_impl.phase_integrity_check is post_live.phase_integrity_check
    assert phases_impl.phase_system_watchdog.__module__ == "tools.loop.phases.post_live"
    assert phases_impl.phase_integrity_check.__module__ == "tools.loop.phases.post_live"
    assert post_live_watch.phase_system_watchdog.__module__ == "tools.loop.phases.post_live_watch"
    assert post_live_watch.phase_integrity_check.__module__ == "tools.loop.phases.post_live_watch"
    assert post_live_watch.phase_system_watchdog is not post_live.phase_system_watchdog
    assert post_live_watch.phase_integrity_check is not post_live.phase_integrity_check
