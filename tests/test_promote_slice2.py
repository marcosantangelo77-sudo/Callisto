"""Pin: promotion query helpers extracted to tools.hypothesis.promote_queries.

HypothesisPromotionMixin keeps the original method names as thin delegates
so hasattr pins still pass. Query bodies live in promote_queries.py.

Does NOT arm live betting. Does NOT add live to paper-signal.
Does NOT add check_promotion_readiness to HypothesisPromotionMixin
(live check is HypothesisSignificanceMixin only).
auto_promote stays diagnose-only for edge_threshold / signal_generated.
auto_promote and review_live_hypotheses stay in promote.py.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTE = ROOT / "tools" / "hypothesis" / "promote.py"
QUERIES = ROOT / "tools" / "hypothesis" / "promote_queries.py"
SIGNIF = ROOT / "tools" / "hypothesis" / "significance.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"

HELPERS = (
    "_get_backtest_signals",
    "_get_backtest_resolved",
    "_diagnose_edge_threshold",
    "_get_best_run_stats",
    "_days_of_odds_data",
    "_avg_books_used",
    "_count_unresolved",
    "_get_paper_trades",
    "_get_paper_trades_all",
)


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


def _is_thin_query_delegate(fn: ast.AsyncFunctionDef, helper: str) -> bool:
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 2:
        return False
    if not isinstance(body[0], ast.ImportFrom):
        return False
    if body[0].module != "tools.hypothesis.promote_queries":
        return False
    aliases = {a.name for a in body[0].names}
    if helper not in aliases:
        return False
    stmt = body[1]
    if not (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Await)):
        return False
    return isinstance(stmt.value.value, ast.Call)


def test_promote_queries_contains_helper_names():
    names = _fn_names(QUERIES)
    for name in HELPERS:
        assert name in names, name
    assert "auto_promote" not in names
    assert "review_live_hypotheses" not in names
    assert "check_promotion_readiness" not in names


def test_mixin_still_has_thin_helper_methods():
    methods = _class_async_fns(PROMOTE, "HypothesisPromotionMixin")
    for name in HELPERS:
        assert name in methods, name
        assert _is_thin_query_delegate(methods[name], name), ast.dump(methods[name])
    src = PROMOTE.read_text(encoding="utf-8")
    # Heavy query bodies must not remain inlined on the mixin.
    assert "CALLISTO_SIGNAL_COLLAPSE_MODE" not in src
    assert "ROW_NUMBER() OVER" not in src
    assert "historical_odds_cache" not in src
    qsrc = QUERIES.read_text(encoding="utf-8")
    assert "CALLISTO_SIGNAL_COLLAPSE_MODE" in qsrc
    assert "ROW_NUMBER() OVER" in qsrc
    assert "historical_odds_cache" in qsrc


def test_check_promotion_readiness_not_on_promotion_mixin():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    signif = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" not in promote
    assert "check_promotion_readiness" in signif
    assert "auto_promote" in promote
    assert "review_live_hypotheses" in promote
    q_tree = ast.parse(QUERIES.read_text(encoding="utf-8"))
    for node in q_tree.body:
        if isinstance(node, ast.ClassDef):
            assert node.name != "HypothesisPromotionMixin"


def test_promote_py_shrunk_again():
    n = PROMOTE.read_text(encoding="utf-8").count("\n")
    assert n < 900, n
    qn = QUERIES.read_text(encoding="utf-8").count("\n")
    assert qn >= 250, qn


def test_auto_promote_still_diagnose_only():
    src = PROMOTE.read_text(encoding="utf-8")
    start = src.index("async def auto_promote")
    end = src.index("\n    async def ", start + 10)
    body = src[start:end]
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
    for path in (PROMOTE, QUERIES, SIGNIF):
        assert not _imports_autonomous(path), path
