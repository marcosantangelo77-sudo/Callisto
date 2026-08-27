"""Slice 7: DeferredQueueMixin body lives in tools.auto.deferred.

Contract:
- tools/auto/research.py re-exports DeferredQueueMixin (slice3 hasattr pin).
- _process_drained_item keeps GATE POLICY REFUSED (never lower threshold).
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays in tools/autonomous.py.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
- deferred.py must not import tools.autonomous.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFERRED = ROOT / "tools" / "auto" / "deferred.py"
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


def test_deferred_mixin_defined_in_deferred_module():
    assert "DeferredQueueMixin" in _class_names(DEFERRED)
    assert "DeferredQueueMixin" not in _class_names(RESEARCH)
    tree = ast.parse(RESEARCH.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.auto.deferred":
            if any(a.name == "DeferredQueueMixin" for a in node.names):
                found = True
    assert found, "research.py must re-export DeferredQueueMixin"


def test_process_drained_item_keeps_gate_policy():
    src = DEFERRED.read_text(encoding="utf-8")
    assert "GATE POLICY REFUSED" in src
    assert "MIN_EDGE_THRESHOLD_FLOOR" in src
    assert "MAX_EDGE_THRESHOLD_CEILING" in src
    tree = ast.parse(src)
    mixin = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DeferredQueueMixin"
    )
    methods = {
        n.name
        for n in mixin.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_drain_deferred_queue" in methods
    assert "_process_drained_item" in methods


def test_deferred_does_not_import_autonomous():
    assert not _imports_autonomous(DEFERRED)
    assert not _imports_autonomous(RESEARCH)


def test_live_execute_gate_stays_on_facade():
    src = AUTO.read_text(encoding="utf-8")
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in DEFERRED.read_text(encoding="utf-8")


def test_deferred_does_not_name_model_ladder():
    tree = ast.parse(DEFERRED.read_text(encoding="utf-8"))
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


def test_research_line_count_dropped_again():
    n = RESEARCH.read_text(encoding="utf-8").count("\n")
    assert n < 900, n
    d = DEFERRED.read_text(encoding="utf-8").count("\n")
    assert d >= 250, d
