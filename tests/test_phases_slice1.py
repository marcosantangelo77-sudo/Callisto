"""Pin: post-live ResearchLoop phases moved into tools.loop.phases.post_live.

Does NOT import tools.autonomous (that module hangs this environment).
Does NOT arm live betting. Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools" / "loop" / "phases_impl.py"
POST = ROOT / "tools" / "loop" / "phases" / "post_live.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

MOVED = (
    "phase_review_live",
    "phase_narrative_edges",
    "phase_claude_deep_work",
    "phase_granger_analysis",
    "phase_regime_analysis",
    "phase_knowledge_compile",
    "phase_knowledge_lint",
    "phase_system_improvement",
    "phase_system_watchdog",
    "phase_integrity_check",
)


def _async_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def test_moved_defs_live_in_post_live_not_phases_impl():
    impl_defs = _async_defs(IMPL)
    post_defs = _async_defs(POST)
    for name in MOVED:
        assert name in post_defs, name
        assert name not in impl_defs, name
    assert "phase_live_execute" in impl_defs
    assert "phase_live_execute" not in post_defs


def test_phases_impl_reexports_moved_names():
    from tools.loop import phases_impl
    from tools.loop.phases import post_live

    for name in MOVED:
        assert getattr(phases_impl, name) is getattr(post_live, name)
        assert getattr(phases_impl, name).__module__ == "tools.loop.phases.post_live"
    assert phases_impl.phase_live_execute.__module__ == "tools.loop.phases_impl"


def test_phases_impl_line_count_dropped():
    n = IMPL.read_text(encoding="utf-8").count("\n")
    assert n < 4000, n
    moved_n = POST.read_text(encoding="utf-8").count("\n")
    assert moved_n >= 400, moved_n


def test_neither_module_imports_autonomous():
    for path in (IMPL, POST, ROOT / "tools" / "loop" / "phases" / "__init__.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or ""), path


def test_live_execute_gate_stays_in_phases_impl():
    tree = ast.parse(IMPL.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "phase_live_execute"
    )
    body = fn.body
    # Facade uses `self = loop` then a docstring then `import os as _os`.
    if (
        body
        and isinstance(body[0], ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "self" for t in body[0].targets
        )
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
    assert any(isinstance(s, ast.Return) for s in body[0].body)


def test_paper_signal_still_paper_trading_only():
    src = PAPER.read_text(encoding="utf-8")
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    assert '"live"' not in src.split("_PAPER_TRADE_SIGNAL_STATUSES", 1)[1][:400]


def test_runtime_live_execute_gate_closed():
    import asyncio
    from tools.loop import phases_impl

    class _Loop:
        hypothesis_manager = object()

    assert asyncio.run(phases_impl.phase_live_execute(_Loop())) is None
