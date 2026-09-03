"""Pin: dead duplicate check_promotion_readiness removed from promote.py.

HypothesisManager uses HypothesisSignificanceMixin.check_promotion_readiness
(MRO). The promote.py copy was unreachable and lacked ``self``.

Does NOT arm live betting. Does NOT add live to paper-signal.
auto_promote stays diagnose-only for edge_threshold / signal_generated.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTE = ROOT / "tools" / "hypothesis" / "promote.py"
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


def _fn(path: Path, class_name: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    return next(
        n for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )


def test_readiness_defined_only_on_significance_mixin():
    promote = _async_methods(PROMOTE, "HypothesisPromotionMixin")
    signif = _async_methods(SIGNIF, "HypothesisSignificanceMixin")
    assert "check_promotion_readiness" not in promote
    assert "check_promotion_readiness" in signif
    assert "auto_promote" in promote
    assert "_diagnose_edge_threshold" in promote
    assert "_compute_portfolio_overlap" in promote


def test_manager_dispatches_to_significance_readiness():
    from tools.hypothesis.manager import HypothesisManager
    from tools.hypothesis.significance import HypothesisSignificanceMixin

    assert HypothesisManager.check_promotion_readiness is (
        HypothesisSignificanceMixin.check_promotion_readiness
    )
    params = list(
        HypothesisManager.check_promotion_readiness.__code__.co_varnames[:2]
    )
    assert params[0] == "self"
    assert params[1] == "hypothesis_id"


def test_promote_py_shrunk():
    n = PROMOTE.read_text(encoding="utf-8").count("\n")
    # Floor dropped after auto_promote extract (slice4 pins n < 320).
    assert n < 320, n
    assert n >= 200, n


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
    for path in (PROMOTE, SIGNIF):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert "autonomous" not in a.name, path
            elif isinstance(node, ast.ImportFrom):
                assert "autonomous" not in (node.module or ""), path
