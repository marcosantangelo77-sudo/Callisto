"""Slice 1: AutonomousLoop._find_analysis_candidates lives in loop_candidates.

Contract:
- tools/auto/loop.py keeps FunctionDef ``_find_analysis_candidates`` (slice2
  hasattr pin) as a thin delegate to ``find_analysis_candidates``.
- The nested candidate-scan body is in tools/auto/loop_candidates.py.
- loop_candidates.py must not import tools.autonomous.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays out of this module.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "tools" / "auto" / "loop.py"
CANDIDATES = ROOT / "tools" / "auto" / "loop_candidates.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _class_methods(path: Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{class_name} missing in {path}")


def _fn_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


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


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        cur = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _is_thin_delegate_to_find_analysis_candidates(fn: ast.FunctionDef) -> bool:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    call = None
    if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
        call = stmt.value
    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
    if call is None:
        return False
    name = _call_name(call)
    return name in {
        "find_analysis_candidates",
        "loop_candidates.find_analysis_candidates",
    }


def test_loop_keeps_find_analysis_candidates_method():
    cls = _class_methods(LOOP, "AutonomousLoop")
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_find_analysis_candidates" in methods


def test_find_analysis_candidates_is_thin_delegate():
    cls = _class_methods(LOOP, "AutonomousLoop")
    fn = next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_find_analysis_candidates"
    )
    assert _is_thin_delegate_to_find_analysis_candidates(fn), ast.dump(fn)
    src = ast.unparse(fn)
    assert "find_analysis_candidates" in src
    # Heavy nested scan logic must not remain inlined on the class method.
    assert "Cross-book divergence edges" not in src
    assert "best_soft_edge" not in src
    assert "implied_range" not in src
    loop_src = LOOP.read_text(encoding="utf-8")
    assert "Cross-book divergence edges" not in loop_src
    assert "Look up KL divergence metrics for this game" not in loop_src


def test_loop_candidates_defines_find_analysis_candidates():
    names = _fn_names(CANDIDATES)
    assert "find_analysis_candidates" in names
    tree = ast.parse(CANDIDATES.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "find_analysis_candidates"
    )
    assert [a.arg for a in fn.args.args] == ["loop"]
    src = CANDIDATES.read_text(encoding="utf-8")
    assert "Cross-book divergence edges" in src
    assert "self = loop" in src


def test_loop_candidates_does_not_import_autonomous():
    assert not _imports_autonomous(CANDIDATES)
    assert not _imports_autonomous(LOOP)


def test_loop_line_count_dropped():
    n = LOOP.read_text(encoding="utf-8").count("\n")
    assert n < 750, n
    d = CANDIDATES.read_text(encoding="utf-8").count("\n")
    assert d >= 400, d


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


def test_no_live_execute_gate_in_loop_candidates():
    src = CANDIDATES.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
