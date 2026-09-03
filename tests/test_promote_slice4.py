"""Pin: auto_promote body lives in tools.hypothesis.promote_auto.

HypothesisPromotionMixin keeps a thin ``async def auto_promote`` delegate
(hasattr pin). Diagnose-only: no edge_threshold / signal_generated writes.
Does NOT add check_promotion_readiness to HypothesisPromotionMixin
(live check is HypothesisSignificanceMixin only).
review_live_hypotheses stays a thin delegate to promote_review.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTE = ROOT / "tools" / "hypothesis" / "promote.py"
AUTO = ROOT / "tools" / "hypothesis" / "promote_auto.py"
REVIEW = ROOT / "tools" / "hypothesis" / "promote_review.py"
QUERIES = ROOT / "tools" / "hypothesis" / "promote_queries.py"
SIGNIF = ROOT / "tools" / "hypothesis" / "significance.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _async_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    return {
        n.name
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef)
    }


def _class_async_fns(path: Path, class_name: str) -> dict[str, ast.AsyncFunctionDef]:
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


def _is_thin_auto_promote(fn: ast.AsyncFunctionDef) -> bool:
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
            if body[0].module != "tools.hypothesis.promote_auto":
                return False
            aliases = {a.name for a in body[0].names}
            if "auto_promote" not in aliases:
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
        "auto_promote",
        "_auto_promote",
        "promote_auto.auto_promote",
    }


def test_promote_auto_contains_auto_promote_body():
    names = _top_level_async(AUTO)
    assert "auto_promote" in names
    assert "review_live_hypotheses" not in names
    assert "check_promotion_readiness" not in names
    fn = names["auto_promote"]
    src = AUTO.read_text(encoding="utf-8")
    body = "\n".join(src.splitlines()[fn.lineno - 1 : fn.end_lineno])
    assert "threshold_too_high" in body
    assert "check_promotion_readiness" in body
    assert "paper_trade_sample_insufficient" in body
    assert fn.end_lineno - fn.lineno + 1 >= 300
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            assert node.name != "HypothesisPromotionMixin"


def test_mixin_keeps_thin_auto_promote():
    methods = _class_async_fns(PROMOTE, "HypothesisPromotionMixin")
    assert "auto_promote" in methods
    assert "review_live_hypotheses" in methods
    ev = methods["auto_promote"]
    assert _is_thin_auto_promote(ev), ast.dump(ev)
    src = PROMOTE.read_text(encoding="utf-8")
    wrapper = "\n".join(src.splitlines()[ev.lineno - 1 : ev.end_lineno])
    assert "promote_auto" in wrapper
    assert "threshold_too_high" not in wrapper
    assert "paper_trade_sample_insufficient" not in wrapper


def test_check_promotion_readiness_not_on_promotion_mixin():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    signif = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" not in promote
    assert "check_promotion_readiness" in signif
    assert "check_promotion_readiness" not in _fn_names(AUTO)
    tree = ast.parse(PROMOTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "check_promotion_readiness"


def test_hasattr_pins_auto_promote():
    from tools.hypothesis.manager import HypothesisManager
    from tools.hypothesis.promote import HypothesisPromotionMixin

    assert hasattr(HypothesisPromotionMixin, "auto_promote")
    assert hasattr(HypothesisManager, "auto_promote")
    assert HypothesisManager.auto_promote is (
        HypothesisPromotionMixin.auto_promote
    )
    assert not hasattr(HypothesisPromotionMixin, "check_promotion_readiness")


def test_line_counts():
    n = PROMOTE.read_text(encoding="utf-8").count("\n")
    an = AUTO.read_text(encoding="utf-8").count("\n")
    assert n < 320, n
    assert an >= 300, an


def test_auto_promote_still_diagnose_only():
    src = AUTO.read_text(encoding="utf-8")
    start = src.index("async def auto_promote")
    body = src[start:]
    assert "edge_threshold =" not in body.replace("edge_threshold ==", "")
    assert "SET edge_threshold" not in body
    assert "SET signal_generated" not in body
    assert "UPDATE paper_trades" not in body
    for line in body.splitlines():
        if "UPDATE" in line:
            assert "model_config = ?" in line or "SET status" in line, line


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


def test_neither_module_imports_autonomous():
    for path in (PROMOTE, AUTO, REVIEW, QUERIES, SIGNIF):
        assert not _imports_autonomous(path), path


def test_no_live_betting_flags_added():
    for path in (PROMOTE, AUTO):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
