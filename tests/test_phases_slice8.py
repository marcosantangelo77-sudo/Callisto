"""Pin: phase_claude_deep_work moved into tools.loop.phases.claude_deep.

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
POST = ROOT / "tools" / "loop" / "phases" / "post_live.py"
CLAUDE = ROOT / "tools" / "loop" / "phases" / "claude_deep.py"
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


def test_claude_deep_defines_async_phase():
    assert "phase_claude_deep_work" in _async_defs(CLAUDE)
    fn = next(
        n
        for n in ast.parse(CLAUDE.read_text(encoding="utf-8")).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "phase_claude_deep_work"
    )
    assert fn.args.args and fn.args.args[0].arg == "loop"


def test_post_live_still_exposes_name():
    names = _top_level_names(POST)
    assert "phase_claude_deep_work" in names
    tree = ast.parse(POST.read_text(encoding="utf-8"))
    exposed = False
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "phase_claude_deep_work":
            exposed = True
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if (a.asname or a.name) == "phase_claude_deep_work":
                    exposed = True
    assert exposed


def test_neither_claude_deep_nor_post_live_imports_autonomous():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        CLAUDE, POST, IMPL, pkg / "__init__.py", pkg / "repair.py",
        pkg / "backtest_run.py", pkg / "collect_eval.py", pkg / "hypgen.py",
        pkg / "pre_live.py", pkg / "shared.py",
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
    claude_n = CLAUDE.read_text(encoding="utf-8").count("\n")
    assert post_n < 900, post_n
    assert claude_n >= 350, claude_n


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


def test_live_execute_not_introduced_into_claude_deep():
    src = CLAUDE.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
    assert "phase_live_execute" not in _async_defs(CLAUDE)


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
    assert "phase_live_execute" not in _async_defs(CLAUDE)


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
    from tools.loop.phases import claude_deep

    class _Loop:
        hypothesis_manager = object()

    assert asyncio.run(phases_impl.phase_live_execute(_Loop())) is None
    assert phases_impl.phase_claude_deep_work is post_live.phase_claude_deep_work
    assert phases_impl.phase_claude_deep_work.__module__ == "tools.loop.phases.post_live"
    assert claude_deep.phase_claude_deep_work.__module__ == "tools.loop.phases.claude_deep"
    assert claude_deep.phase_claude_deep_work is not post_live.phase_claude_deep_work
