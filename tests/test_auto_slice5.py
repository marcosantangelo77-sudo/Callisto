"""Slice 5: remaining ResearchLoop facade helpers into tools.auto/.

Contract:
- tools/autonomous.py keeps ResearchLoop, thin ``_phase_*`` wrappers,
  thin ``__init__`` / ``get_status`` / ``_check_temporal_overlap``
  delegates, and the CALLISTO_ALLOW_LIVE_EXECUTE-gated
  ``_phase_live_execute`` (gate is still the first executable statement
  after docstring / ``import os``).
- ``get_status`` body lives in tools.auto.status.build_research_loop_status
- ``__init__`` body lives in tools.auto.loop_init.init_research_loop
- temporal overlap lives in tools.auto.temporal.check_temporal_overlap
- New modules must not import tools.autonomous (no cycles).
- Paper-signal statuses stay paper_trading-only. Live betting is not armed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTO = ROOT / "tools" / "autonomous.py"
STATUS = ROOT / "tools" / "auto" / "status.py"
LOOP_INIT = ROOT / "tools" / "auto" / "loop_init.py"
TEMPORAL = ROOT / "tools" / "auto" / "temporal.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _fn_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _class_methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"{class_name} missing in {path}")


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


def test_extracted_helpers_live_outside_facade():
    auto_fns = _fn_names(AUTO)
    status_fns = _fn_names(STATUS)
    init_fns = _fn_names(LOOP_INIT)
    temporal_fns = _fn_names(TEMPORAL)
    assert "build_research_loop_status" in status_fns
    assert "init_research_loop" in init_fns
    assert "check_temporal_overlap" in temporal_fns
    assert "build_research_loop_status" not in auto_fns
    assert "init_research_loop" not in auto_fns
    assert "check_temporal_overlap" not in auto_fns


def test_facade_keeps_thin_delegates_and_live_execute_gate():
    methods = _class_methods(AUTO, "ResearchLoop")
    for name in (
        "__init__",
        "get_status",
        "_check_temporal_overlap",
        "_phase_live_execute",
        "_phase_paper_trade",
        "_phase_collect_data",
    ):
        assert name in methods, name

    tree = ast.parse(AUTO.read_text(encoding="utf-8"))
    rl = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResearchLoop")

    def _method(name: str):
        return next(
            n
            for n in rl.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        )

    status = _method("get_status")
    status_src = ast.unparse(status)
    assert "build_research_loop_status" in status_src
    assert "latest(10)" not in status_src

    init = _method("__init__")
    init_src = ast.unparse(init)
    assert "init_research_loop" in init_src
    assert "_phase_failures_ledger" not in init_src

    temporal = _method("_check_temporal_overlap")
    assert "check_temporal_overlap" in ast.unparse(temporal)
    assert "TEMPORAL OVERLAP" not in ast.unparse(temporal)


def test_live_execute_gate_still_first_in_facade():
    tree = ast.parse(AUTO.read_text(encoding="utf-8"))
    rl = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ResearchLoop")
    fn = next(
        n
        for n in rl.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_phase_live_execute"
    )
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if body and isinstance(body[0], ast.Import):
        body = body[1:]
    assert body and isinstance(body[0], ast.If)
    dump = ast.dump(body[0].test)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dump
    assert "1" in dump


def test_status_still_wires_ledger_and_cycle_health():
    src = STATUS.read_text(encoding="utf-8")
    assert "latest(10)" in src
    assert '"last_cycle_ok"' in src
    assert '"last_cycle_phase_failures"' in src
    assert '"phase_failures"' in src
    assert '"phase_failure_count"' in src
    assert '"calibration"' in src


def test_init_still_creates_ledger_not_raw_list():
    src = LOOP_INIT.read_text(encoding="utf-8")
    assert "_phase_failures_ledger" in src
    assert "PhaseFailureLedger()" in src
    assert "self._phase_failures =" not in src
    assert "loop._phase_failures =" not in src


def test_new_modules_do_not_import_autonomous():
    for path in (STATUS, LOOP_INIT, TEMPORAL):
        assert not _imports_autonomous(path), path


def test_facade_line_budget_shrank():
    n = AUTO.read_text(encoding="utf-8").count("\n")
    assert n < 380, n
    assert STATUS.read_text(encoding="utf-8").count("\n") > 60
    assert LOOP_INIT.read_text(encoding="utf-8").count("\n") > 60


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


def test_extracted_modules_do_not_arm_live():
    for path in (STATUS, LOOP_INIT, TEMPORAL):
        src = path.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
        assert "generate_paper_trade_signal" not in src
