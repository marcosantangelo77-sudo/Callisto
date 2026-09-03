"""Pin: ProviderRouter.complete lives in tools.infrouter.complete.

Does NOT import tools.autonomous. Does NOT arm live betting.
Does NOT add live to paper-signal. Does NOT point MODEL_LADDER at
ProviderRouter. Facade keeps thin complete()/complete_sync() delegates
(hasattr pins). hermes_complete stays a last-resort fallback inside
complete.py — not the agent runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "inference_router.py"
COMPLETE = ROOT / "tools" / "infrouter" / "complete.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _top_level_func_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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


def test_complete_helper_lives_in_infrouter_complete():
    names = _top_level_func_names(COMPLETE)
    assert "complete" in names
    tree = ast.parse(COMPLETE.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "complete"
    )
    args = [a.arg for a in fn.args.args]
    assert args[0] == "router"
    assert "task_class" in args
    assert "messages" in args
    dump = ast.dump(fn)
    assert "candidates_for" in dump
    assert "hermes_cli" in dump
    assert "hermes_complete" in dump


def test_facade_keeps_thin_complete_delegate():
    methods = _class_methods(FACADE, "ProviderRouter")
    assert "complete" in methods
    assert "complete_sync" in methods
    complete = methods["complete"]
    assert isinstance(complete, ast.AsyncFunctionDef)
    dump = ast.dump(complete)
    assert "_complete" in dump
    assert "candidates_for" in dump
    assert "hermes_cli" in dump
    # Body is a delegate, not the failover loop.
    assert "cost_ledger" not in dump
    assert "_post_with_retry" not in dump
    dump_sync = ast.dump(methods["complete_sync"])
    assert "_complete" in dump_sync or "complete_sync" in dump_sync


def test_complete_module_does_not_import_autonomous_or_facade():
    assert not _imports_autonomous(COMPLETE)
    assert not _imports_autonomous(FACADE)
    tree = ast.parse(COMPLETE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "inference_router"
            assert node.module != "tools.autonomous"
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "inference_router"
                assert "autonomous" not in a.name


def test_facade_line_count_dropped():
    n = FACADE.read_text(encoding="utf-8").count("\n")
    assert n < 360, n
    complete_n = COMPLETE.read_text(encoding="utf-8").count("\n")
    assert complete_n >= 80, complete_n


def test_complete_does_not_name_model_ladder():
    for path in (COMPLETE, FACADE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "MODEL_LADDER", path
            elif isinstance(node, ast.Attribute):
                assert node.attr != "MODEL_LADDER", path
            elif isinstance(node, ast.Assign):
                dump = ast.dump(node)
                assert "MODEL_LADDER" not in dump, path


def test_hermes_complete_stays_fallback_inside_complete():
    """hermes_complete is last-resort hermes_cli, not the primary path."""
    tree = ast.parse(COMPLETE.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "complete"
    )
    dump = ast.dump(fn)
    http_pos = dump.find("_post_with_retry")
    hermes_pos = dump.find("hermes_complete")
    cli_pos = dump.find("hermes_cli")
    assert http_pos != -1
    assert hermes_pos != -1
    assert cli_pos != -1
    # The CLI backend is gated; HTTP retry is the non-CLI path.
    assert "backend" in dump


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


def test_runtime_complete_delegate_same_object():
    from inference_router import ProviderRouter
    from tools.infrouter import complete as complete_mod

    assert hasattr(ProviderRouter, "complete")
    assert hasattr(ProviderRouter, "complete_sync")
    assert callable(complete_mod.complete)
    r = ProviderRouter()
    assert r.complete is not None
    assert complete_mod.complete_sync is not None
