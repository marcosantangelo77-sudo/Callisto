"""Pin: evaluate_significance body lives in tools.hypothesis.significance_eval.

HypothesisSignificanceMixin keeps a thin ``async def evaluate_significance``
delegate (hasattr pin) plus the full ``check_promotion_readiness`` body.
Live promotion readiness stays on HypothesisSignificanceMixin only —
check_promotion_readiness must not appear on HypothesisPromotionMixin.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNIF = ROOT / "tools" / "hypothesis" / "significance.py"
EVAL = ROOT / "tools" / "hypothesis" / "significance_eval.py"
PROMOTE = ROOT / "tools" / "hypothesis" / "promote.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _async_methods(path: Path, class_name: str) -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    return {
        n.name: n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def _top_level_async(path: Path) -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
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


def _is_thin_delegate_to_evaluate_significance(fn: ast.AsyncFunctionDef) -> bool:
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
    if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Await):
        if isinstance(stmt.value.value, ast.Call):
            call = stmt.value.value
    elif isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Call):
        call = stmt.value
    if call is None:
        return False
    name = _call_name(call)
    return name in {
        "evaluate_significance",
        "_evaluate_significance",
        "significance_eval.evaluate_significance",
    }


def test_significance_eval_contains_evaluate_significance():
    names = _top_level_async(EVAL)
    assert "evaluate_significance" in names
    fn = names["evaluate_significance"]
    src = EVAL.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "used_all_events" in body
    assert "INSERT INTO hypothesis_stats" in body
    assert "binomial_pvalue" in body
    assert fn.end_lineno - fn.lineno + 1 >= 200


def test_mixin_keeps_thin_evaluate_significance_and_full_readiness():
    methods = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "evaluate_significance" in methods
    assert "check_promotion_readiness" in methods

    ev = methods["evaluate_significance"]
    assert _is_thin_delegate_to_evaluate_significance(ev), ast.dump(ev)
    src = SIGNIF.read_text(encoding="utf-8")
    wrapper = "\n".join(src.splitlines()[ev.lineno - 1 : ev.end_lineno])
    assert "significance_eval" in wrapper
    assert "INSERT INTO hypothesis_stats" not in wrapper
    assert "used_all_events = False" not in wrapper

    readiness = methods["check_promotion_readiness"]
    span = readiness.end_lineno - readiness.lineno + 1
    assert span >= 700, span
    body = "\n".join(src.splitlines()[readiness.lineno - 1 : readiness.end_lineno])
    assert "Šidák" in body or "Sidak" in body or "sidak" in body
    assert "min_paper_trades" in body
    assert "simulate_before_promote" in body


def test_hasattr_pins_evaluate_significance():
    from tools.hypothesis.manager import HypothesisManager
    from tools.hypothesis.significance import HypothesisSignificanceMixin

    assert hasattr(HypothesisSignificanceMixin, "evaluate_significance")
    assert hasattr(HypothesisSignificanceMixin, "check_promotion_readiness")
    assert hasattr(HypothesisManager, "evaluate_significance")
    assert hasattr(HypothesisManager, "check_promotion_readiness")
    assert HypothesisManager.evaluate_significance is (
        HypothesisSignificanceMixin.evaluate_significance
    )
    assert HypothesisManager.check_promotion_readiness is (
        HypothesisSignificanceMixin.check_promotion_readiness
    )


def test_check_promotion_readiness_not_in_promote():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    assert "check_promotion_readiness" not in promote
    tree = ast.parse(PROMOTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "check_promotion_readiness"


def test_line_counts():
    signif_n = SIGNIF.read_text(encoding="utf-8").count("\n")
    eval_n = EVAL.read_text(encoding="utf-8").count("\n")
    assert signif_n < 900, signif_n
    assert eval_n >= 200, eval_n


def test_neither_module_imports_autonomous():
    assert not _imports_autonomous(SIGNIF)
    assert not _imports_autonomous(EVAL)
    assert not _imports_autonomous(PROMOTE)


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
    for path in (SIGNIF, EVAL):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
