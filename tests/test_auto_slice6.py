"""Slice 6: CycleLoopMixin body lives in tools.auto.cycle.

Contract:
- tools/auto/research.py re-exports CycleLoopMixin (slice3 hasattr pin).
- tools/autonomous.py still composes ResearchLoop from the five mixins.
- _loop still iterates sequencer PHASES then PERIODIC_PHASES.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays in tools/autonomous.py.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
- cycle.py must not import tools.autonomous.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYCLE = ROOT / "tools" / "auto" / "cycle.py"
RESEARCH = ROOT / "tools" / "auto" / "research.py"
AUTO = ROOT / "tools" / "autonomous.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _class_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef)}


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


def test_cycle_mixin_defined_in_cycle_module():
    assert "CycleLoopMixin" in _class_names(CYCLE)
    assert "CycleLoopMixin" not in _class_names(RESEARCH)
    tree = ast.parse(RESEARCH.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.auto.cycle":
            if any(a.name == "CycleLoopMixin" for a in node.names):
                found = True
    assert found, "research.py must re-export CycleLoopMixin"


def test_loop_iterates_sequencer_tables():
    tree = ast.parse(CYCLE.read_text(encoding="utf-8"))
    mixin = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "CycleLoopMixin"
    )
    methods = {
        n.name: n
        for n in mixin.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_loop" in methods
    assert "_quant_scan_loop" in methods
    dump = ast.dump(methods["_loop"])
    assert "PHASES" in dump
    assert "PERIODIC_PHASES" in dump
    assert "_check_progress" in dump


def test_cycle_does_not_import_autonomous():
    assert not _imports_autonomous(CYCLE)
    # research.py also must not import the facade (cycle already existed).
    assert not _imports_autonomous(RESEARCH)


def test_live_execute_gate_stays_on_facade():
    src = AUTO.read_text(encoding="utf-8")
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
    cycle_src = CYCLE.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in cycle_src


def test_cycle_does_not_name_model_ladder():
    tree = ast.parse(CYCLE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "MODEL_LADDER"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "MODEL_LADDER"


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


def test_research_line_count_dropped():
    n = RESEARCH.read_text(encoding="utf-8").count("\n")
    assert n < 1200, n
    c = CYCLE.read_text(encoding="utf-8").count("\n")
    assert c >= 200, c
