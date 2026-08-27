"""Pin: ResearchPipeline._answer_leaf body lives in tools.pipeline.answer_leaf.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. Does NOT point MODEL_LADDER at
ProviderRouter. Facade keeps a thin _answer_leaf wrapper. EstimateCeiling
wiring (belief vs entitlement) stays in the extracted body.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "tools" / "pipeline" / "engine.py"
ANSWER = ROOT / "tools" / "pipeline" / "answer_leaf.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _class_methods(path: Path, class_name: str) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    return {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_func_names(path: Path) -> set[str]:
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


def _first_assign_after_docstring(fn: ast.AsyncFunctionDef):
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


def test_answer_leaf_lives_in_extracted_module():
    names = _top_level_func_names(ANSWER)
    assert "answer_leaf" in names
    facade_top = _top_level_func_names(ENGINE)
    assert "answer_leaf" not in facade_top
    # Sandbox persist helpers stay on the facade module.
    assert "_store_sandbox" in facade_top
    assert "_cleanup_workspace" in facade_top


def test_facade_keeps_thin_answer_leaf_wrapper():
    methods = _class_methods(ENGINE, "ResearchPipeline")
    assert "_answer_leaf" in methods
    wrapper = methods["_answer_leaf"]
    dump = ast.dump(wrapper)
    assert "answer_leaf" in dump
    text = ENGINE.read_text(encoding="utf-8")
    src = "\n".join(text.splitlines()[wrapper.lineno - 1 : wrapper.end_lineno])
    assert "EstimateCeiling" not in src
    assert "classify_null_kind" not in src
    assert "run_python" not in src
    assert src.count("return") == 1


def test_estimate_ceiling_wiring_in_answer_leaf():
    src = ANSWER.read_text(encoding="utf-8")
    assert "from agp.estimate import EstimateCeiling" in src
    assert "out.confidence_estimate" in src
    assert "out.confidence_ceiling" in src
    # Historical rounding: round(min(estimate, ceiling), 2)
    assert "round(" in src
    assert "min(ec.estimate, ec.ceiling)" in src
    # answers_question declared signal (R3) still here.
    assert 'proposal.get("answers_question", True)' in src


def test_self_bound_to_pipeline():
    tree = ast.parse(ANSWER.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "answer_leaf"
    )
    first = _first_assign_after_docstring(fn)
    assert isinstance(first, ast.Assign)
    assert len(first.targets) == 1
    assert isinstance(first.targets[0], ast.Name) and first.targets[0].id == "self"
    assert isinstance(first.value, ast.Name) and first.value.id == "pipeline"


def test_engine_line_count_dropped_again():
    n = ENGINE.read_text(encoding="utf-8").count("\n")
    assert n < 700, n
    ans_n = ANSWER.read_text(encoding="utf-8").count("\n")
    assert ans_n >= 140, ans_n


def test_answer_leaf_does_not_import_autonomous():
    assert not _imports_autonomous(ANSWER)
    assert not _imports_autonomous(ENGINE)


def test_answer_leaf_does_not_name_model_ladder():
    tree = ast.parse(ANSWER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "MODEL_LADDER"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "MODEL_LADDER"


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


def test_pipeline_package_does_not_eager_import_answer_leaf():
    init = (ROOT / "tools" / "pipeline" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree))
    assert "answer_leaf" not in init
