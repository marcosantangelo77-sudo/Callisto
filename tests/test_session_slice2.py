"""Pin: step_escalate_to_claude body lives in tools.orch.session_escalate.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. session_steps keeps a thin
async def step_escalate_to_claude wrapper (or ImportFrom re-export).
step_synthesize / step_manager_review / run_session_flow / execute_tool stay here.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ROOT / "tools" / "orch" / "session_steps.py"
ESCALATE = ROOT / "tools" / "orch" / "session_escalate.py"
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


def _has_escalate_name(path: Path) -> bool:
    """FunctionDef/AsyncFunctionDef wrapper or ImportFrom re-export."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "step_escalate_to_claude":
            return True
        if isinstance(n, ast.ImportFrom):
            for alias in n.names:
                if alias.name == "step_escalate_to_claude" or alias.asname == "step_escalate_to_claude":
                    return True
    return False


def test_session_escalate_defines_async_step_escalate_to_claude():
    asyncs = _top_level_async(ESCALATE)
    assert "step_escalate_to_claude" in asyncs
    fn = asyncs["step_escalate_to_claude"]
    assert isinstance(fn, ast.AsyncFunctionDef)
    # Body is the real implementation, not a hop.
    src = ESCALATE.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "cites_verified_url" in body
    assert "ESCALATION_THRESHOLD" in body
    assert "deep_work" in body
    assert "claude_code_available" in body


def test_session_steps_still_has_escalate_name():
    assert _has_escalate_name(STEPS)
    # Thin: wrapper or re-export, not the original body.
    src = STEPS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "step_escalate_to_claude":
            body = "\n".join(src.splitlines()[n.lineno - 1 : n.end_lineno])
            assert "session_escalate" in body
            assert "cites_verified_url" not in body
            assert "ESCALATION_THRESHOLD" not in body
            assert body.count("return") == 1
        elif isinstance(n, ast.ImportFrom):
            names = {a.name for a in n.names} | {a.asname for a in n.names if a.asname}
            if "step_escalate_to_claude" in names:
                assert n.module and "session_escalate" in n.module


def test_other_steps_not_extracted_this_slice():
    steps_names = _top_level_func_names(STEPS)
    escalate_names = _top_level_func_names(ESCALATE)
    for name in ("step_synthesize", "step_manager_review", "run_session_flow", "execute_tool"):
        assert name in steps_names, name
        assert name not in escalate_names, name


def test_session_escalate_does_not_import_autonomous():
    assert not _imports_autonomous(ESCALATE)
    assert not _imports_autonomous(STEPS)


def test_line_counts():
    steps_n = STEPS.read_text(encoding="utf-8").count("\n")
    escalate_n = ESCALATE.read_text(encoding="utf-8").count("\n")
    assert steps_n < 680, steps_n
    assert escalate_n >= 120, escalate_n


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
    for path in (ESCALATE, STEPS):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "live":
                raise AssertionError(f"live status literal in {path}")
