"""Slice 3: AutonomousLoop psychology helpers live in loop_psych.

Contract:
- tools/auto/loop.py keeps FunctionDef ``_run_market_psychology``,
  ``_get_psychology_for_edge``, and ``get_psychology_report`` (hasattr pins)
  as thin delegates.
- The nested bodies are in tools/auto/loop_psych.py (``self = loop``).
- loop_psych.py must not import tools.autonomous.
- ``_loop``, ``_cleanup_dedup``, and ``get_status`` stay on AutonomousLoop.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays out of this module.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "tools" / "auto" / "loop.py"
PSYCH = ROOT / "tools" / "auto" / "loop_psych.py"
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
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def test_loop_keeps_psychology_methods():
    cls = _class_methods(LOOP, "AutonomousLoop")
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in (
        "_run_market_psychology",
        "_get_psychology_for_edge",
        "get_psychology_report",
        "_loop",
        "_cleanup_dedup",
        "get_status",
    ):
        assert name in methods, name


def test_psychology_methods_are_thin_delegates():
    fn = _method("_run_market_psychology")
    assert _is_thin_delegate(
        fn,
        {"run_market_psychology", "loop_psych.run_market_psychology"},
    ), ast.dump(fn)
    fn = _method("_get_psychology_for_edge")
    assert _is_thin_delegate(
        fn,
        {"get_psychology_for_edge", "loop_psych.get_psychology_for_edge"},
    ), ast.dump(fn)
    fn = _method("get_psychology_report")
    assert _is_thin_delegate(
        fn,
        {
            "get_psychology_report",
            "_get_psychology_report",
            "loop_psych.get_psychology_report",
        },
    ), ast.dump(fn)
    loop_src = LOOP.read_text(encoding="utf-8")
    assert "shaded lines detected" not in loop_src
    assert "shade_magnitude_cents" not in loop_src
    assert "full_market_psychology" not in loop_src


def test_loop_psych_defines_the_three_functions():
    names = _fn_names(PSYCH)
    assert "run_market_psychology" in names
    assert "get_psychology_for_edge" in names
    assert "get_psychology_report" in names
    assert "_loop" not in names
    assert "_cleanup_dedup" not in names
    assert "get_status" not in names
    tree = ast.parse(PSYCH.read_text(encoding="utf-8"))
    by_name = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
    }
    assert [a.arg for a in by_name["run_market_psychology"].args.args] == ["loop"]
    assert [a.arg for a in by_name["get_psychology_for_edge"].args.args] == [
        "loop",
        "sport",
        "game",
        "team",
        "market",
    ]
    assert [a.arg for a in by_name["get_psychology_report"].args.args] == ["loop"]
    src = PSYCH.read_text(encoding="utf-8")
    assert src.count("self = loop") >= 3
    assert "shaded lines detected" in src
    assert "number_shading_detected" in src
    assert "full_market_psychology" in src


def test_leftover_methods_not_moved_to_psych_or_edge():
    psych_src = PSYCH.read_text(encoding="utf-8")
    edge_src = EDGE.read_text(encoding="utf-8")
    for name in ("_loop", "_cleanup_dedup", "get_status"):
        assert f"def {name}" not in psych_src
        assert f"def {name}" not in edge_src


def test_loop_psych_does_not_import_autonomous():
    assert not _imports_autonomous(PSYCH)
    assert not _imports_autonomous(LOOP)


def test_loop_line_count_dropped():
    n = LOOP.read_text(encoding="utf-8").count("\n")
    assert n < 400, n
    d = PSYCH.read_text(encoding="utf-8").count("\n")
    assert d >= 90, d


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


def test_no_live_execute_gate_in_loop_psych():
    src = PSYCH.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
