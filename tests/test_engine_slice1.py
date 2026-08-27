"""Pin: ResearchPipeline._run_inner body lives in tools.pipeline.run_inner.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. Does NOT point MODEL_LADDER at
ProviderRouter. Facade keeps run() (cross-run wrap) and a thin
_run_inner wrapper. _answer_leaf stays on the class.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "tools" / "pipeline" / "engine.py"
INNER = ROOT / "tools" / "pipeline" / "run_inner.py"
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


def test_run_inner_lives_in_extracted_module():
    names = _top_level_func_names(INNER)
    assert "run_inner" in names
    facade_top = _top_level_func_names(ENGINE)
    assert "run_inner" not in facade_top
    # Helpers stay on the facade module.
    assert "verify_artifact_gate" in facade_top
    assert "_fetch_from_payload" in facade_top
    assert "_trace_from_payload" in facade_top
    assert "_leaf_from_payload" in facade_top


def test_facade_keeps_run_and_thin_run_inner_wrapper():
    methods = _class_methods(ENGINE, "ResearchPipeline")
    assert "run" in methods
    assert "_run_inner" in methods
    assert "_answer_leaf" in methods
    wrapper = methods["_run_inner"]
    dump = ast.dump(wrapper)
    assert "run_inner" in dump
    # Thin: no inline decompose/seal work.
    text = ENGINE.read_text(encoding="utf-8")
    src = "\n".join(text.splitlines()[wrapper.lineno - 1 : wrapper.end_lineno])
    assert "seal_hash = session.seal()" not in src
    assert "verify_artifact_gate(self.store" not in src
    assert src.count("return") == 1
    # run() still wraps _run_inner for cross-run memory.
    run_dump = ast.dump(methods["run"])
    assert "_run_inner" in run_dump


def test_gate_before_seal_in_run_inner():
    src = INNER.read_text(encoding="utf-8")
    seal_pos = src.index("seal_hash = session.seal()")
    gate_pos = src.rindex("verify_artifact_gate(self.store")
    assert gate_pos < seal_pos
    assert "verify_artifacts" in src


def test_self_bound_to_pipeline():
    tree = ast.parse(INNER.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_inner"
    )
    # First executable after docstring: self = pipeline
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert body, "run_inner has no body"
    first = body[0]
    assert isinstance(first, ast.Assign)
    assert len(first.targets) == 1
    assert isinstance(first.targets[0], ast.Name) and first.targets[0].id == "self"
    assert isinstance(first.value, ast.Name) and first.value.id == "pipeline"


def test_engine_line_count_dropped():
    n = ENGINE.read_text(encoding="utf-8").count("\n")
    assert n < 900, n
    inner_n = INNER.read_text(encoding="utf-8").count("\n")
    assert inner_n >= 500, inner_n


def test_run_inner_does_not_import_autonomous():
    assert not _imports_autonomous(INNER)
    assert not _imports_autonomous(ENGINE)


def test_run_inner_does_not_name_model_ladder():
    for path in (INNER, ENGINE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
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


def test_pipeline_package_does_not_eager_import_run_inner():
    init = (ROOT / "tools" / "pipeline" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree))
