"""Slice 5: AutonomousLoop._loop body lives in loop_run.

Contract:
- tools/auto/loop.py keeps AsyncFunctionDef ``_loop`` (slice3/slice4
  method-name pins) as a thin delegate to ``run_loop``.
- The nested body is in tools/auto/loop_run.py (``self = loop``).
- loop_run.py must not import tools.autonomous.
- CALLISTO_ALLOW_LIVE_EXECUTE gate stays out of this module.
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "tools" / "auto" / "loop.py"
RUN = ROOT / "tools" / "auto" / "loop_run.py"
STATUS = ROOT / "tools" / "auto" / "loop_status.py"
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


def _is_thin_delegate(fn, expected: set[str]) -> bool:
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
    if isinstance(stmt, ast.Return):
        val = stmt.value
        if isinstance(val, ast.Await):
            val = val.value
        if isinstance(val, ast.Call):
            call = val
    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
    if call is None:
        return False
    return _call_name(call) in expected


def _async_method(name: str) -> ast.AsyncFunctionDef:
    cls = _class_methods(LOOP, "AutonomousLoop")
    for node in cls.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def test_loop_keeps_async_loop_method():
    cls = _class_methods(LOOP, "AutonomousLoop")
    methods = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_loop" in methods


def test_loop_method_is_thin_delegate():
    fn = _async_method("_loop")
    assert _is_thin_delegate(
        fn,
        {"run_loop", "loop_run.run_loop"},
    ), ast.dump(fn)
    loop_src = LOOP.read_text(encoding="utf-8")
    assert "analyzing top" not in loop_src
    assert "Autonomous loop error" not in loop_src
    assert "Wait for first snapshot cycle" not in loop_src


def test_loop_run_defines_run_loop():
    names = _fn_names(RUN)
    assert "run_loop" in names
    tree = ast.parse(RUN.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_loop"
    )
    assert [a.arg for a in fn.args.args] == ["loop"]
    src = RUN.read_text(encoding="utf-8")
    assert "self = loop" in src
    assert "analyzing top" in src
    assert "Wait for first snapshot cycle" in src


def test_run_loop_not_in_other_extracts():
    for path in (STATUS, PSYCH, EDGE):
        src = path.read_text(encoding="utf-8")
        assert "async def run_loop" not in src
        assert "Wait for first snapshot cycle" not in src


def test_loop_run_does_not_import_autonomous():
    assert not _imports_autonomous(RUN)
    assert not _imports_autonomous(LOOP)


def test_loop_line_count_dropped():
    n = LOOP.read_text(encoding="utf-8").count("\n")
    assert n < 280, n
    d = RUN.read_text(encoding="utf-8").count("\n")
    assert d >= 55, d


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


def test_no_live_execute_gate_in_loop_run():
    src = RUN.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
