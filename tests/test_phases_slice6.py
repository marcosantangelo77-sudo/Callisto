"""Pin: repair/diagnose/refresh phases moved into tools.loop.phases.repair.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. Signal-refresh write path stays gated.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools" / "loop" / "phases_impl.py"
REPAIR = ROOT / "tools" / "loop" / "phases" / "repair.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED = ("phase_self_repair", "phase_self_diagnose", "phase_refresh_signals")


def _async_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def test_moved_defs_live_in_repair_not_phases_impl():
    impl_defs = _async_defs(IMPL)
    repair_defs = _async_defs(REPAIR)
    for name in MOVED:
        assert name in repair_defs, name
        assert name not in impl_defs, name
    assert "phase_live_execute" in impl_defs
    assert "phase_live_execute" not in repair_defs
    leftover = {n for n in impl_defs if n.startswith("phase_")}
    assert leftover == {"phase_live_execute"}, leftover


def test_phases_impl_reexports_repair_names():
    from tools.loop import phases_impl
    from tools.loop.phases import repair

    for name in MOVED:
        assert getattr(phases_impl, name) is getattr(repair, name)
        assert getattr(phases_impl, name).__module__ == "tools.loop.phases.repair"
    assert phases_impl.phase_live_execute.__module__ == "tools.loop.phases_impl"


def test_phases_impl_is_now_helpers_plus_live_execute():
    n = IMPL.read_text(encoding="utf-8").count("\n")
    assert n < 550, n
    moved_n = REPAIR.read_text(encoding="utf-8").count("\n")
    assert moved_n >= 350, moved_n


def test_signal_refresh_write_path_still_gated():
    src = REPAIR.read_text(encoding="utf-8")
    assert 'os.getenv("CALLISTO_ALLOW_SIGNAL_REFRESH") == "1"' in src
    fn = next(
        n for n in ast.parse(src).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "phase_refresh_signals"
    )
    dump = ast.dump(fn)
    assert "CALLISTO_ALLOW_SIGNAL_REFRESH" in dump


def test_neither_repair_nor_impl_imports_autonomous():
    pkg = ROOT / "tools" / "loop" / "phases"
    for path in (
        IMPL, REPAIR, pkg / "__init__.py", pkg / "backtest_run.py",
        pkg / "collect_eval.py", pkg / "hypgen.py", pkg / "pre_live.py",
        pkg / "post_live.py", pkg / "shared.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or ""), path


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
