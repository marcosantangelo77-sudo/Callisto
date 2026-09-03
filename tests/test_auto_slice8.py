"""Slice 8: ProgressMixin and CorrelationMixin bodies live in dedicated modules.

Contract:
- tools/auto/research.py re-exports ProgressMixin and CorrelationMixin
  (slice3 hasattr pin). MaintenanceMixin stays defined in research.py.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays in tools/autonomous.py.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
- progress.py and correlation.py must not import tools.autonomous.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = ROOT / "tools" / "auto" / "progress.py"
CORRELATION = ROOT / "tools" / "auto" / "correlation.py"
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


def _reexport_found(module: str, name: str) -> bool:
    tree = ast.parse(RESEARCH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if any(a.name == name for a in node.names):
                return True
    return False


def test_progress_mixin_defined_in_progress_module():
    assert "ProgressMixin" in _class_names(PROGRESS)
    assert "ProgressMixin" not in _class_names(RESEARCH)
    assert _reexport_found("tools.auto.progress", "ProgressMixin"), (
        "research.py must re-export ProgressMixin"
    )


def test_correlation_mixin_defined_in_correlation_module():
    assert "CorrelationMixin" in _class_names(CORRELATION)
    assert "CorrelationMixin" not in _class_names(RESEARCH)
    assert _reexport_found("tools.auto.correlation", "CorrelationMixin"), (
        "research.py must re-export CorrelationMixin"
    )


def test_progress_keeps_evaluate_and_spinning_json_prompt():
    src = PROGRESS.read_text(encoding="utf-8")
    assert "evaluate_progress_window" in src
    assert "RESPOND WITH JSON:" in src
    assert '"root_cause"' in src
    assert '"evidence"' in src
    assert '"fix"' in src
    tree = ast.parse(src)
    mixin = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "ProgressMixin"
    )
    methods = {
        n.name
        for n in mixin.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_check_progress" in methods
    assert "_run_spinning_diagnosis" in methods


def test_correlation_keeps_pairwise_jaccard_matrix():
    src = CORRELATION.read_text(encoding="utf-8")
    assert "Jaccard" in src
    assert "CALLISTO_CORR_TTL_SECONDS" in src
    tree = ast.parse(src)
    mixin = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "CorrelationMixin"
    )
    methods = {
        n.name: n
        for n in mixin.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_build_correlation_matrix" in methods
    assert "_hyp_signals_n_map" in methods
    dump = ast.dump(methods["_build_correlation_matrix"])
    assert "union" in dump
    assert "corr" in dump


def test_extracted_modules_do_not_import_autonomous():
    assert not _imports_autonomous(PROGRESS)
    assert not _imports_autonomous(CORRELATION)
    assert not _imports_autonomous(RESEARCH)


def test_live_execute_gate_stays_on_facade():
    src = AUTO.read_text(encoding="utf-8")
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in PROGRESS.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in CORRELATION.read_text(encoding="utf-8")


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


def test_research_line_count_dropped_below_650():
    n = RESEARCH.read_text(encoding="utf-8").count("\n")
    assert n < 650, n
    assert PROGRESS.read_text(encoding="utf-8").count("\n") >= 150
    assert CORRELATION.read_text(encoding="utf-8").count("\n") >= 80


def test_maintenance_mixin_stays_in_research():
    assert "MaintenanceMixin" in _class_names(RESEARCH)
    assert "MaintenanceMixin" not in _class_names(PROGRESS)
    assert "MaintenanceMixin" not in _class_names(CORRELATION)
    src = RESEARCH.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_THRESHOLD_MIGRATION" in src
