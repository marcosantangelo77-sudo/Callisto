"""Sequencer extraction slice: tools.loop package importability + wiring."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = types.ModuleType("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

from tools.loop.phase_ledger import PhaseFailureLedger
import tools.autonomous as auto


class TestPackageImportable:
    def test_tools_loop_imports(self):
        import tools.loop  # noqa: F401

    def test_phase_ledger_exported_from_module(self):
        from tools.loop import phase_ledger

        assert phase_ledger.PhaseFailureLedger is PhaseFailureLedger


class TestResearchLoopWiring:
    def _init_source(self):
        with open(auto.__file__) as f:
            return f.read()

    def test_init_creates_ledger(self):
        import ast

        tree = ast.parse(self._init_source())
        assigns = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "_phase_failures_ledger", "_phase_failures", "_PHASE_FAILURES_MAX"
            ):
                assigns.add(node.attr)
        assert "_phase_failures_ledger" in assigns
        # Raw list / max constant must be gone from the class.
        assert "_phase_failures" not in assigns
        assert "_PHASE_FAILURES_MAX" not in assigns

    def test_get_status_delegates_to_ledger(self):
        import ast

        tree = ast.parse(self._init_source())
        found = {"latest(10)": False, ".count": False}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_status":
                src = ast.unparse(node)
                found["latest(10)"] = "latest(10)" in src
                found[".count"] = "ledger.count" in src
        assert all(found.values()), f"get_status not wired to ledger: {found}"


class TestLiveExecuteUntouched:
    def test_live_execute_still_present(self):
        assert hasattr(auto.ResearchLoop, "_phase_live_execute") or any(
            name.startswith("_live_execute") or "live_execute" in name
            for name in dir(auto.ResearchLoop)
        )
