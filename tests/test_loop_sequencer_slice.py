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
    def _source_trees(self):
        import ast
        from pathlib import Path

        facade = Path(auto.__file__)
        paths = [
            facade,
            facade.parent / "auto" / "loop_init.py",
            facade.parent / "auto" / "status.py",
        ]
        return [ast.parse(p.read_text(encoding="utf-8")) for p in paths]

    def test_init_creates_ledger(self):
        import ast

        assigns = set()
        for tree in self._source_trees():
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

        found = {"latest(10)": False, ".count": False}
        for tree in self._source_trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in (
                    "get_status",
                    "build_research_loop_status",
                ):
                    src = ast.unparse(node)
                    found["latest(10)"] = found["latest(10)"] or "latest(10)" in src
                    found[".count"] = found[".count"] or "ledger.count" in src
        assert all(found.values()), f"get_status not wired to ledger: {found}"


class TestLiveExecuteUntouched:
    def test_live_execute_still_present(self):
        assert hasattr(auto.ResearchLoop, "_phase_live_execute") or any(
            name.startswith("_live_execute") or "live_execute" in name
            for name in dir(auto.ResearchLoop)
        )


class TestSequencerExtraction:
    def test_phases_importable(self):
        from tools.loop.sequencer import PHASES

        names = tuple(s.name for s in PHASES)
        assert "live_execute" in names
        assert "paper_trade" in names

    def test_live_execute_present_and_not_widened(self):
        from tools.loop import sequencer

        core = sequencer.phase_names(sequencer.PHASES)
        # live_execute stays a core phase, immediately after paper_trade.
        i = core.index("live_execute")
        assert core[i - 1] == "paper_trade"

    def test_order_stable(self):
        from tools.loop import sequencer

        assert sequencer.phase_names() == (
            "queue_drain",
            "self_repair",
            "self_diagnose",
            "refresh_signals",
            "backtest",
            "validate",
            "generate_hypotheses",
            "injury_prop_hypotheses",
            "collect_data",
            "embed_data",
            "evaluate",
            "interpret_backtests",
            "paper_trade",
            "live_execute",
            "review_live",
            "narrative_edges",
            "claude_deep_work",
        )

    def test_methods_exist_on_research_loop(self):
        from tools.loop import sequencer

        for spec in (*sequencer.PHASES, *sequencer.PERIODIC_PHASES):
            assert hasattr(auto.ResearchLoop, spec.method), f"missing {spec.method}"

    def test_loop_iterates_sequencer_tables(self):
        import ast

        from tools.loop import sequencer

        src = open(auto.__file__).read()
        tree = ast.parse(src)
        rl = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == "ResearchLoop"
        )
        loop_fn = next(
            n for n in rl.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_loop"
        )
        text = ast.unparse(loop_fn)
        assert "for spec in PHASES" in text
        assert "for spec in PERIODIC_PHASES" in text
        # No hardcoded per-phase call remains in _loop.
        for name in sequencer.phase_names():
            assert f"_phase_{name}" not in text.replace(
                "_record_phase_failure", ""
            ), f"_loop still hardcodes _phase_{name}"

    def test_loop_phase_methods_untouched_elsewhere(self):
        # The phase method bodies still live in autonomous.py.
        src = open(auto.__file__).read()
        assert "async def _phase_live_execute" in src
        assert "async def _phase_paper_trade" in src
