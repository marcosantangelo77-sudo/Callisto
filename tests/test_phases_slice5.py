"""Pin: backtest + validate phases moved into tools.loop.phases.backtest_run.

Does NOT import tools.autonomous (that module hangs this environment).
Does NOT arm live betting. Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools" / "loop" / "phases_impl.py"
BT = ROOT / "tools" / "loop" / "phases" / "backtest_run.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED = ("phase_backtest", "phase_validate")


def _async_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def test_moved_defs_live_in_backtest_run_not_phases_impl():
    impl_defs = _async_defs(IMPL)
    bt_defs = _async_defs(BT)
    for name in MOVED:
        assert name in bt_defs, name
        assert name not in impl_defs, name
    assert "phase_live_execute" in impl_defs
    assert "phase_live_execute" not in bt_defs


def test_phases_impl_reexports_backtest_run_names():
    from tools.loop import phases_impl
    from tools.loop.phases import backtest_run

    for name in MOVED:
        assert getattr(phases_impl, name) is getattr(backtest_run, name)
        assert getattr(phases_impl, name).__module__ == "tools.loop.phases.backtest_run"
    assert phases_impl.phase_live_execute.__module__ == "tools.loop.phases_impl"


def test_phases_impl_line_count_dropped_again():
    n = IMPL.read_text(encoding="utf-8").count("\n")
    assert n < 1000, n
    moved_n = BT.read_text(encoding="utf-8").count("\n")
    assert moved_n >= 500, moved_n


def test_neither_backtest_run_nor_impl_imports_autonomous():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        IMPL, BT, pkg / "__init__.py", pkg / "collect_eval.py",
        pkg / "hypgen.py", pkg / "pre_live.py", pkg / "post_live.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or ""), path


def test_backtest_lists_draft_not_live_signal():
    src = BT.read_text(encoding="utf-8")
    assert 'status="draft"' in src
    assert "generate_paper_trade_signal" not in src
    assert 'status="live"' not in src


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
