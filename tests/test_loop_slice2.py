"""Slice 2: AutonomousLoop injury/line-analysis helpers live in loop_edge.

Contract:
- tools/auto/loop.py keeps FunctionDef ``_run_injury_analysis_for_edge`` and
  ``_compute_line_analysis_signals`` (hasattr pins) as thin delegates.
- The nested bodies are in tools/auto/loop_edge.py (``self = loop``).
- loop_edge.py must not import tools.autonomous.
- Psychology helpers, ``_find_analysis_candidates``, and ``get_status`` stay
  on AutonomousLoop.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays out of this module.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "tools" / "auto" / "loop.py"
EDGE = ROOT / "tools" / "auto" / "loop_edge.py"
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


def _is_thin_delegate(fn: ast.FunctionDef, expected: set[str]) -> bool:
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
    return _call_name(call) in expected


def _method(name: str) -> ast.FunctionDef:
    cls = _class_methods(LOOP, "AutonomousLoop")
    return next(
        n
        for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


def test_loop_keeps_injury_and_line_analysis_methods():
    cls = _class_methods(LOOP, "AutonomousLoop")
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_run_injury_analysis_for_edge" in methods
    assert "_compute_line_analysis_signals" in methods


def test_run_injury_analysis_for_edge_is_thin_delegate():
    fn = _method("_run_injury_analysis_for_edge")
    assert _is_thin_delegate(
        fn,
        {
            "run_injury_analysis_for_edge",
            "loop_edge.run_injury_analysis_for_edge",
        },
    ), ast.dump(fn)
    src = ast.unparse(fn)
    assert "run_injury_analysis_for_edge" in src
    assert "No significant injuries" not in src
    assert "minutes_since_announced" not in src
    loop_src = LOOP.read_text(encoding="utf-8")
    assert "No significant injuries" not in loop_src
    assert "minutes_since_announced" not in loop_src
    assert "Key absences:" not in loop_src


def test_compute_line_analysis_signals_is_thin_delegate():
    fn = _method("_compute_line_analysis_signals")
    assert _is_thin_delegate(
        fn,
        {
            "compute_line_analysis_signals",
            "loop_edge.compute_line_analysis_signals",
        },
    ), ast.dump(fn)
    src = ast.unparse(fn)
    assert "compute_line_analysis_signals" in src
    assert "Dead number / key number analysis" not in src
    assert "detect_steam" not in src
    loop_src = LOOP.read_text(encoding="utf-8")
    assert "Dead number / key number analysis" not in loop_src
    assert "Public side estimation and contrarian value" not in loop_src
    assert "detect_steam(" not in loop_src


def test_loop_edge_defines_the_two_functions():
    names = _fn_names(EDGE)
    assert "run_injury_analysis_for_edge" in names
    assert "compute_line_analysis_signals" in names
    tree = ast.parse(EDGE.read_text(encoding="utf-8"))
    by_name = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }
    inj = by_name["run_injury_analysis_for_edge"]
    assert [a.arg for a in inj.args.args] == [
        "loop",
        "sport",
        "game_name",
        "team_name",
    ]
    line = by_name["compute_line_analysis_signals"]
    assert [a.arg for a in line.args.args] == [
        "loop",
        "sport",
        "edge",
        "market",
        "game",
        "team",
    ]
    src = EDGE.read_text(encoding="utf-8")
    assert src.count("self = loop") >= 2
    assert "No significant injuries" in src
    assert "Dead number / key number analysis" in src


def test_psychology_and_status_stay_on_loop():
    cls = _class_methods(LOOP, "AutonomousLoop")
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in (
        "_run_market_psychology",
        "_get_psychology_for_edge",
        "_find_analysis_candidates",
        "get_status",
        "get_psychology_report",
    ):
        assert name in methods, name
    edge_src = EDGE.read_text(encoding="utf-8")
    assert "def _run_market_psychology" not in edge_src
    assert "def get_status" not in edge_src
    assert "def find_analysis_candidates" not in edge_src


def test_loop_edge_does_not_import_autonomous():
    assert not _imports_autonomous(EDGE)
    assert not _imports_autonomous(LOOP)


def test_loop_line_count_dropped():
    n = LOOP.read_text(encoding="utf-8").count("\n")
    assert n < 500, n
    d = EDGE.read_text(encoding="utf-8").count("\n")
    assert d >= 180, d


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


def test_no_live_execute_gate_in_loop_edge():
    src = EDGE.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
