"""Pin: phase_granger_analysis and phase_regime_analysis moved into
tools.loop.phases.regime_granger.

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
REGIME = ROOT / "tools" / "loop" / "phases" / "regime_granger.py"
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


def test_regime_granger_defines_both_async_phases():
    defs = _async_defs(REGIME)
    assert "phase_granger_analysis" in defs
    assert "phase_regime_analysis" in defs
    tree = ast.parse(REGIME.read_text(encoding="utf-8"))
    for name in ("phase_granger_analysis", "phase_regime_analysis"):
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == name
        )
        assert fn.args.args and fn.args.args[0].arg == "loop"


def test_post_live_still_exposes_names():
    names = _top_level_names(POST)
    assert "phase_granger_analysis" in names
    assert "phase_regime_analysis" in names
    tree = ast.parse(POST.read_text(encoding="utf-8"))
    exposed = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.name in ("phase_granger_analysis", "phase_regime_analysis"):
                exposed.add(n.name)
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                nm = a.asname or a.name
                if nm in ("phase_granger_analysis", "phase_regime_analysis"):
                    exposed.add(nm)
    assert exposed == {"phase_granger_analysis", "phase_regime_analysis"}


def test_neither_regime_granger_nor_post_live_imports_autonomous():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        REGIME, POST, IMPL, pkg / "__init__.py", pkg / "repair.py",
        pkg / "backtest_run.py", pkg / "collect_eval.py", pkg / "hypgen.py",
        pkg / "pre_live.py", pkg / "shared.py", pkg / "claude_deep.py",
        pkg / "system_improve.py", pkg / "post_live_review.py",
        pkg / "post_live_wiki.py", pkg / "post_live_watch.py",
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
    regime_n = REGIME.read_text(encoding="utf-8").count("\n")
    assert post_n < 500, post_n
    assert regime_n >= 180, regime_n


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


def test_live_execute_not_introduced_into_regime_granger():
    src = REGIME.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
    assert "phase_live_execute" not in _async_defs(REGIME)


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
    assert "phase_live_execute" not in _async_defs(REGIME)


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
    from tools.loop.phases import regime_granger

    class _Loop:
        hypothesis_manager = object()

    assert asyncio.run(phases_impl.phase_live_execute(_Loop())) is None
    assert phases_impl.phase_granger_analysis is post_live.phase_granger_analysis
    assert phases_impl.phase_regime_analysis is post_live.phase_regime_analysis
    assert phases_impl.phase_granger_analysis.__module__ == "tools.loop.phases.post_live"
    assert phases_impl.phase_regime_analysis.__module__ == "tools.loop.phases.post_live"
    assert regime_granger.phase_granger_analysis.__module__ == "tools.loop.phases.regime_granger"
    assert regime_granger.phase_regime_analysis.__module__ == "tools.loop.phases.regime_granger"
    assert regime_granger.phase_granger_analysis is not post_live.phase_granger_analysis
    assert regime_granger.phase_regime_analysis is not post_live.phase_regime_analysis
