"""Pin: check_promotion_readiness body lives in tools.hypothesis.significance_ready.

HypothesisSignificanceMixin keeps a thin ``async def check_promotion_readiness``
delegate (hasattr pin). Live promotion readiness stays on
HypothesisSignificanceMixin only — check_promotion_readiness must not
appear on HypothesisPromotionMixin.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIGNIF = ROOT / "tools" / "hypothesis" / "significance.py"
READY = ROOT / "tools" / "hypothesis" / "significance_ready.py"
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


def _is_thin_delegate_to_check_promotion_readiness(fn: ast.AsyncFunctionDef) -> bool:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        if isinstance(body[0], ast.ImportFrom):
            if body[0].module != "tools.hypothesis.significance_ready":
                return False
            aliases = {a.name for a in body[0].names}
            if "check_promotion_readiness" not in aliases:
                return False
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
        "check_promotion_readiness",
        "_check_promotion_readiness",
        "significance_ready.check_promotion_readiness",
    }


def test_significance_ready_contains_readiness_body():
    names = _top_level_async(READY)
    assert "check_promotion_readiness" in names
    assert "evaluate_significance" not in names
    assert "auto_promote" not in names
    fn = names["check_promotion_readiness"]
    src = READY.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "Šidák" in body or "Sidak" in body or "sidak" in body
    assert "min_paper_trades" in body
    assert "simulate_before_promote" in body
    assert "min_days" in body
    assert "REGIME_DIVERSITY" in src or "regime_diversity" in body or "single_regime_sample" in body
    assert fn.end_lineno - fn.lineno + 1 >= 700
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            assert node.name != "HypothesisSignificanceMixin"
            assert node.name != "HypothesisPromotionMixin"


def test_mixin_keeps_thin_check_promotion_readiness():
    methods = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" in methods
    assert "evaluate_significance" in methods
    ev = methods["check_promotion_readiness"]
    assert _is_thin_delegate_to_check_promotion_readiness(ev), ast.dump(ev)
    src = SIGNIF.read_text(encoding="utf-8")
    wrapper = "\n".join(src.splitlines()[ev.lineno - 1 : ev.end_lineno])
    assert "significance_ready" in wrapper
    assert "INSERT INTO hypothesis_stats" not in wrapper
    assert "simulate_before_promote" not in wrapper
    assert "min_paper_trades" not in wrapper


def test_check_promotion_readiness_not_on_promotion_mixin():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    signif = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" not in promote
    assert "check_promotion_readiness" in signif
    ready_names = _fn_names(READY)
    eval_names = _fn_names(EVAL)
    assert "check_promotion_readiness" in ready_names
    assert "check_promotion_readiness" not in eval_names
    tree = ast.parse(PROMOTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "check_promotion_readiness"


def test_hasattr_pins_check_promotion_readiness():
    from tools.hypothesis.manager import HypothesisManager
    from tools.hypothesis.significance import HypothesisSignificanceMixin
    from tools.hypothesis.promote import HypothesisPromotionMixin

    assert hasattr(HypothesisSignificanceMixin, "check_promotion_readiness")
    assert hasattr(HypothesisManager, "check_promotion_readiness")
    assert HypothesisManager.check_promotion_readiness is (
        HypothesisSignificanceMixin.check_promotion_readiness
    )
    assert not hasattr(HypothesisPromotionMixin, "check_promotion_readiness")


def test_line_counts():
    signif_n = SIGNIF.read_text(encoding="utf-8").count("\n")
    ready_n = READY.read_text(encoding="utf-8").count("\n")
    assert signif_n < 120, signif_n
    assert ready_n >= 700, ready_n


def test_neither_module_imports_autonomous():
    assert not _imports_autonomous(SIGNIF)
    assert not _imports_autonomous(READY)
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
    for path in (SIGNIF, READY):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
