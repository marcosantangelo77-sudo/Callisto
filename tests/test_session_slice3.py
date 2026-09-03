"""Pin: run_session_flow body lives in tools.orch.session_flow.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. session_steps keeps a thin
async def run_session_flow wrapper (or ImportFrom re-export).
step_synthesize / step_manager_review / execute_tool stay here.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ROOT / "tools" / "orch" / "session_steps.py"
FLOW = ROOT / "tools" / "orch" / "session_flow.py"
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


def _has_flow_name(path: Path) -> bool:
    """FunctionDef/AsyncFunctionDef wrapper or ImportFrom re-export."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_session_flow":
            return True
        if isinstance(n, ast.ImportFrom):
            for alias in n.names:
                if alias.name == "run_session_flow" or alias.asname == "run_session_flow":
                    return True
    return False


def test_session_flow_defines_async_run_session_flow():
    asyncs = _top_level_async(FLOW)
    assert "run_session_flow" in asyncs
    fn = asyncs["run_session_flow"]
    assert isinstance(fn, ast.AsyncFunctionDef)
    # Body is the real implementation, not a hop.
    src = FLOW.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "_active_sessions" in body
    assert "seal_refused_reason" in body
    assert "SESSION_CLOSE" in body
    assert "step_synthesize" in body
    assert "step_manager_review" in body


def test_session_steps_still_has_flow_name():
    assert _has_flow_name(STEPS)
    # Thin: wrapper or re-export, not the original body.
    src = STEPS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_session_flow":
            body = "\n".join(src.splitlines()[n.lineno - 1 : n.end_lineno])
            assert "session_flow" in body
            assert "_active_sessions" not in body
            assert "seal_refused_reason" not in body
            assert "SESSION_CLOSE" not in body
            assert body.count("return") == 1
        elif isinstance(n, ast.ImportFrom):
            names = {a.name for a in n.names} | {a.asname for a in n.names if a.asname}
            if "run_session_flow" in names:
                assert n.module and "session_flow" in n.module


def test_other_steps_not_extracted_this_slice():
    steps_names = _top_level_func_names(STEPS)
    flow_names = _top_level_func_names(FLOW)
    for name in ("step_synthesize", "step_manager_review", "execute_tool"):
        assert name in steps_names, name
        assert name not in flow_names, name


def test_session_flow_does_not_import_autonomous():
    assert not _imports_autonomous(FLOW)
    assert not _imports_autonomous(STEPS)


def test_line_counts():
    steps_n = STEPS.read_text(encoding="utf-8").count("\n")
    flow_n = FLOW.read_text(encoding="utf-8").count("\n")
    assert steps_n < 520, steps_n
    assert flow_n >= 150, flow_n


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
    for path in (FLOW, STEPS):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "live":
                raise AssertionError(f"live status literal in {path}")
