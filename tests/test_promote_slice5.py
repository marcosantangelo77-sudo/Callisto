"""Pin: overlap + report bodies live in tools.hypothesis.promote_report.

HypothesisPromotionMixin keeps thin delegates (hasattr pins).
check_promotion_readiness stays on HypothesisSignificanceMixin only.
auto_promote stays diagnose-only. execute path is not armed.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTE = ROOT / "tools" / "hypothesis" / "promote.py"
REPORT = ROOT / "tools" / "hypothesis" / "promote_report.py"
AUTO = ROOT / "tools" / "hypothesis" / "promote_auto.py"
REVIEW = ROOT / "tools" / "hypothesis" / "promote_review.py"
SIGNIF = ROOT / "tools" / "hypothesis" / "significance.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _class_methods(path: Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(class_name)


def _async_methods(path: Path, class_name: str) -> set[str]:
    cls = _class_methods(path, class_name)
    return {
        n.name
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def _fn_names(path: Path) -> set[str]:
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


def _method(name: str):
    cls = _class_methods(PROMOTE, "HypothesisPromotionMixin")
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def test_mixin_keeps_report_methods():
    asyncs = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    assert "_compute_portfolio_overlap" in asyncs
    assert "get_hypothesis_report" in asyncs
    assert "check_promotion_readiness" not in asyncs
    cls = _class_methods(PROMOTE, "HypothesisPromotionMixin")
    names = {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "get_temporal_metadata" in names


def test_report_methods_are_thin_delegates():
    fn = _method("_compute_portfolio_overlap")
    assert _is_thin_delegate(
        fn,
        {"compute_portfolio_overlap", "_impl", "promote_report.compute_portfolio_overlap"},
    ), ast.dump(fn)
    fn = _method("get_hypothesis_report")
    assert _is_thin_delegate(
        fn,
        {"get_hypothesis_report", "_impl", "promote_report.get_hypothesis_report"},
    ), ast.dump(fn)
    fn = _method("get_temporal_metadata")
    assert _is_thin_delegate(
        fn,
        {"get_temporal_metadata", "_impl", "promote_report.get_temporal_metadata"},
    ), ast.dump(fn)
    src = PROMOTE.read_text(encoding="utf-8")
    assert "PORTFOLIO_OVERLAP_WINDOW_DAYS" not in src.split("class HypothesisPromotionMixin", 1)[1]
    assert "training_period_end" not in src.split("class HypothesisPromotionMixin", 1)[1]
    assert "promotion_readiness" not in src.split("class HypothesisPromotionMixin", 1)[1]


def test_promote_report_defines_bodies():
    names = _fn_names(REPORT)
    assert "compute_portfolio_overlap" in names
    assert "get_hypothesis_report" in names
    assert "get_temporal_metadata" in names
    src = REPORT.read_text(encoding="utf-8")
    assert "PORTFOLIO_OVERLAP_WINDOW_DAYS" in src
    assert "training_period_end" in src
    assert "check_promotion_readiness" in src
    assert "check_promotion_readiness stays on" in src or "HypothesisSignificanceMixin" in src


def test_readiness_not_copied_onto_promotion_mixin():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    signif = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" not in promote
    assert "check_promotion_readiness" in signif
    report_async = {
        n.name
        for n in ast.parse(REPORT.read_text(encoding="utf-8")).body
        if isinstance(n, ast.AsyncFunctionDef)
    }
    assert "check_promotion_readiness" not in report_async


def test_promote_report_does_not_import_autonomous():
    assert not _imports_autonomous(REPORT)
    assert not _imports_autonomous(PROMOTE)
    assert not _imports_autonomous(AUTO)
    assert not _imports_autonomous(REVIEW)


def test_line_counts():
    n = PROMOTE.read_text(encoding="utf-8").count("\n")
    rn = REPORT.read_text(encoding="utf-8").count("\n")
    assert n < 220, n
    assert n >= 150, n
    assert rn >= 120, rn


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


def test_no_live_execute_gate_in_promote_report():
    src = REPORT.read_text(encoding="utf-8")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
