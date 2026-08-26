"""Extraction of AutonomousLoop from tools/autonomous.py into tools/auto/.

Contract:
- The class body lives in tools/auto/loop.py; tools/autonomous.py is a facade
  that re-exports it (and its module constants) for existing callers.
- ResearchLoop and the _phase_* delegation to tools.loop.phases_impl stay in
  tools/autonomous.py (pinned by test_auto_phases_extract.py).
- The live-execute env gate semantics are untouched.
"""

import ast
import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous.
if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = types.ModuleType("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

import tools.autonomous as auto
import tools.auto as auto_pkg
from tools.auto import loop as auto_loop


def _read(path):
    with open(path) as f:
        return f.read()


class TestFacadeReexports:
    def test_class_identity(self):
        assert auto.AutonomousLoop is auto_loop.AutonomousLoop
        assert auto_pkg.AutonomousLoop is auto_loop.AutonomousLoop

    def test_constants_reexported(self):
        for name in (
            "ANALYSIS_COOLDOWN",
            "EDGE_DEDUP_WINDOW",
            "MIN_CONFIDENCE_TO_ALERT",
            "MIN_IMPLIED_RANGE",
            "MIN_SOFT_EDGE_VS_SHARP",
        ):
            assert getattr(auto, name) == getattr(auto_loop, name), name

    def test_sport_map_reexported(self):
        assert auto._SPORT_TO_MODEL is auto_loop._SPORT_TO_MODEL
        assert auto._SPORT_TO_MODEL["basketball_nba"] == "NBA"

    def test_research_loop_untouched(self):
        # ResearchLoop still defined in autonomous.py, not moved.
        src = _read(auto.__file__)
        tree = ast.parse(src)
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        assert "ResearchLoop" in names
        assert "AutonomousLoop" not in names

    def test_facade_imports_from_tools_auto(self):
        src = _read(auto.__file__)
        assert "from tools.auto.loop import" in src
        assert "AutonomousLoop" in src.split("from tools.auto.loop import", 1)[1].split(")", 1)[0]

    def test_phases_impl_not_moved(self):
        from tools.loop import phases_impl

        assert phases_impl.__file__.endswith(os.path.join("tools", "loop", "phases_impl.py"))


class TestSourcePins:
    def _al_tree(self):
        tree = ast.parse(_read(auto_loop.__file__))
        return next(
            n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AutonomousLoop"
        )

    def test_key_methods_present(self):
        al = self._al_tree()
        methods = {
            n.name for n in al.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for m in (
            "__init__",
            "start",
            "stop",
            "_loop",
            "_run_market_psychology",
            "_find_analysis_candidates",
            "_analyze_edge",
            "_compute_line_analysis_signals",
            "_phase_parlay_correlation_scan",
            "_cleanup_dedup",
            "get_status",
            "get_psychology_report",
            "get_parlay_scan_report",
        ):
            assert m in methods, f"AutonomousLoop missing {m}"

    def test_no_telegram_send_message(self):
        src = _read(auto_loop.__file__)
        assert "telegram.send_message" not in src
        assert "telegram.alert_edge" in src

    def test_gate_bounds_did_not_move_into_auto(self):
        # GATE POLICY bounds belong to ResearchLoop's interpret phase; they
        # must remain re-exported via phases_impl in the facade, not defined
        # in tools/auto/.
        src = _read(auto_loop.__file__)
        assert "MIN_EDGE_THRESHOLD_FLOOR" not in src
        assert "MAX_EDGE_THRESHOLD_CEILING" not in src

    def test_live_execute_stays_in_research_loop(self):
        src = _read(auto.__file__)
        assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
        al_src = _read(auto_loop.__file__)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in al_src
        assert "generate_paper_trade_signal" not in al_src

    def test_paper_trade_statuses_not_in_auto(self):
        al_src = _read(auto_loop.__file__)
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in al_src
        assert "'live'" not in al_src and '"live"' not in al_src


class TestBehavioral:
    def _make_loop(self):
        class FakeOrchestrator:
            pass

        class FakeLineMonitor:
            def get_edge_report(self):
                return {}

        return auto.AutonomousLoop(FakeOrchestrator(), FakeLineMonitor())

    def test_initial_state(self):
        loop = self._make_loop()
        assert loop._running is False
        assert loop._task is None
        assert loop._analyzed_edges == {}
        assert loop._session_count == 0
        assert loop._alert_count == 0
        assert loop._loop_cycle == 0

    def test_start_stop(self):
        async def main():
            loop = self._make_loop()
            await loop.start()
            assert loop._running is True
            assert loop._task is not None
            await loop.stop()
            assert loop._running is False
            assert loop._task.done()

        asyncio.run(main())

    def test_cleanup_dedup_prunes_old_entries(self):
        loop = self._make_loop()
        now = time.time()
        loop._analyzed_edges = {
            "fresh": now - 10,
            "old": now - auto_loop.EDGE_DEDUP_WINDOW * 3,
        }
        loop._cleanup_dedup()
        assert "fresh" in loop._analyzed_edges
        assert "old" not in loop._analyzed_edges

    def test_status_shape(self):
        loop = self._make_loop()
        status = loop.get_status()
        assert isinstance(status, dict)

    def test_find_candidates_empty_monitor(self):
        loop = self._make_loop()
        assert loop._find_analysis_candidates() == []

    def test_market_psychology_noop_on_empty_report(self):
        async def main():
            loop = self._make_loop()
            candidates = loop._find_analysis_candidates()
            loop._run_market_psychology()  # empty report -> no-op
            return candidates

        assert asyncio.run(main()) == []
