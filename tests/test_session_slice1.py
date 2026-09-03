"""Pin: step_collect_evidence body lives in tools.orch.session_collect.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. session_steps keeps a thin
async def step_collect_evidence wrapper (or ImportFrom re-export).
run_session_flow / step_escalate_to_claude / step_synthesize stay here.
execute_tool stays in session_steps.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ROOT / "tools" / "orch" / "session_steps.py"
COLLECT = ROOT / "tools" / "orch" / "session_collect.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _top_level_async(path: Path) -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def _top_level_func_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def _has_collect_name(path: Path) -> bool:
    """FunctionDef/AsyncFunctionDef wrapper or ImportFrom re-export."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "step_collect_evidence":
            return True
        if isinstance(n, ast.ImportFrom):
            for alias in n.names:
                if alias.name == "step_collect_evidence" or alias.asname == "step_collect_evidence":
                    return True
    return False


def test_session_collect_defines_async_step_collect_evidence():
    asyncs = _top_level_async(COLLECT)
    assert "step_collect_evidence" in asyncs
    fn = asyncs["step_collect_evidence"]
    assert isinstance(fn, ast.AsyncFunctionDef)
    # Body is the real implementation, not a hop.
    src = COLLECT.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "ProvenanceLedger" in body
    assert "wiki_in_loop" in body
    assert "relabel_evidence" in body


def test_session_steps_still_has_collect_name():
    assert _has_collect_name(STEPS)
    # Thin: wrapper or re-export, not the original body.
    src = STEPS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "step_collect_evidence":
            body = "\n".join(src.splitlines()[n.lineno - 1 : n.end_lineno])
            assert "session_collect" in body
            assert "wiki_in_loop" not in body
            assert body.count("return") == 1
        elif isinstance(n, ast.ImportFrom):
            names = {a.name for a in n.names} | {a.asname for a in n.names if a.asname}
            if "step_collect_evidence" in names:
                assert n.module and "session_collect" in n.module


def test_other_steps_not_extracted_this_slice():
    steps_names = _top_level_func_names(STEPS)
    collect_names = _top_level_func_names(COLLECT)
    for name in ("step_escalate_to_claude", "step_synthesize", "run_session_flow", "execute_tool"):
        assert name in steps_names, name
        assert name not in collect_names, name


def test_session_collect_does_not_import_autonomous():
    assert not _imports_autonomous(COLLECT)
    assert not _imports_autonomous(STEPS)


def test_line_counts():
    steps_n = STEPS.read_text(encoding="utf-8").count("\n")
    collect_n = COLLECT.read_text(encoding="utf-8").count("\n")
    assert steps_n < 850, steps_n
    assert collect_n >= 180, collect_n


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


def test_no_live_betting_flags_added():
    for path in (COLLECT, STEPS):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "live":
                raise AssertionError(f"live status literal in {path}")
