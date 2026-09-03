"""Pin: step_synthesize and step_manager_review bodies live in session_synth.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. session_steps keeps thin
async def wrappers. execute_tool stays in session_steps.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ROOT / "tools" / "orch" / "session_steps.py"
SYNTH = ROOT / "tools" / "orch" / "session_synth.py"
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


def _is_thin(fn: ast.AsyncFunctionDef, module_substr: str) -> bool:
    src_mod = ast.unparse(fn)
    return module_substr in src_mod and src_mod.count("return") == 1


def test_session_synth_defines_both_steps():
    asyncs = _top_level_async(SYNTH)
    assert "step_synthesize" in asyncs
    assert "step_manager_review" in asyncs
    assert "execute_tool" not in asyncs
    assert "run_session_flow" not in asyncs
    src = SYNTH.read_text(encoding="utf-8")
    syn = asyncs["step_synthesize"]
    body = "\n".join(src.splitlines()[syn.lineno - 1 : syn.end_lineno])
    assert "escalate_with_ladder" in body
    assert "EMPTY_SYNTHESIS_MARKER" in body
    assert syn.end_lineno - syn.lineno + 1 >= 80
    mgr = asyncs["step_manager_review"]
    mbody = "\n".join(src.splitlines()[mgr.lineno - 1 : mgr.end_lineno])
    assert "CONTRADICTION_PENALTY" in mbody
    assert "adjusted_confidence" in mbody
    assert mgr.end_lineno - mgr.lineno + 1 >= 60


def test_session_steps_keeps_thin_wrappers():
    asyncs = _top_level_async(STEPS)
    assert "step_synthesize" in asyncs
    assert "step_manager_review" in asyncs
    assert "execute_tool" in asyncs
    syn = asyncs["step_synthesize"]
    assert _is_thin(syn, "session_synth")
    src = STEPS.read_text(encoding="utf-8")
    wrapper = "\n".join(src.splitlines()[syn.lineno - 1 : syn.end_lineno])
    assert "escalate_with_ladder" not in wrapper
    assert "EMPTY_SYNTHESIS_MARKER" not in wrapper
    mgr = asyncs["step_manager_review"]
    assert _is_thin(mgr, "session_synth")
    mwrap = "\n".join(src.splitlines()[mgr.lineno - 1 : mgr.end_lineno])
    assert "CONTRADICTION_PENALTY" not in mwrap
    assert "adjusted_confidence" not in mwrap


def test_execute_tool_stays_in_session_steps():
    steps_names = _top_level_func_names(STEPS)
    synth_names = _top_level_func_names(SYNTH)
    assert "execute_tool" in steps_names
    assert "execute_tool" not in synth_names
    assert "run_session_flow" in steps_names
    assert "run_session_flow" not in synth_names


def test_session_synth_does_not_import_autonomous():
    assert not _imports_autonomous(SYNTH)
    assert not _imports_autonomous(STEPS)
    assert not _imports_autonomous(FLOW)


def test_line_counts():
    steps_n = STEPS.read_text(encoding="utf-8").count("\n")
    synth_n = SYNTH.read_text(encoding="utf-8").count("\n")
    assert steps_n < 360, steps_n
    assert synth_n >= 180, synth_n


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
    for path in (SYNTH, STEPS):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
