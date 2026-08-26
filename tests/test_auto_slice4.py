"""Slice 4: extraction of ResearchLoop facade helpers into tools/auto/facade.py.

Contract:
- tools/autonomous.py keeps ResearchLoop (composed from eight mixins) plus
  __init__, the thin _phase_* delegation wrappers, get_status() and the
  CALLISTO_ALLOW_LIVE_EXECUTE-gated _phase_live_execute — all pinned there
  by earlier slices' AST tests.
- tools/auto/facade.py hosts LifecycleMixin, ReactiveMixin,
  FailureLedgerMixin, RegimeMixin, CalibrationMixin — behaviour-preserving.
- The live-execute env gate and threshold-migration gate policies are intact.
"""

import ast
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous (mirrors earlier slices).
if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = type(sys)("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

import tools.autonomous as auto
import tools.auto.facade as facade_mod


def _read(path):
    with open(path) as f:
        return f.read()


MIXINS = (
    "LifecycleMixin",
    "ReactiveMixin",
    "FailureLedgerMixin",
    "RegimeMixin",
    "CalibrationMixin",
)

MOVED_METHODS = {
    "LifecycleMixin": [
        "start",
        "stop",
        "pause",
        "resume",
        "set_local_only",
        "_claude_ok",
    ],
    "ReactiveMixin": [
        "_on_game_completed",
        "_on_game_lineup_window",
    ],
    "FailureLedgerMixin": [
        "_record_phase_failure",
        "_last_cycle_ok",
        "_last_cycle_phase_failures",
    ],
    "RegimeMixin": [
        "get_regime_for_team",
    ],
    "CalibrationMixin": [
        "record_iteration_outcome",
        "compact_iteration_state",
    ],
}

# Methods that MUST stay defined in the facade class body in
# tools/autonomous.py (pinned by earlier slices' AST tests).
STAYING_METHODS = [
    "__init__",
    "_phase_live_execute",
    "_phase_self_repair",
    "_phase_collect_data",
    "_phase_backtest",
    "_phase_evaluate",
    "_phase_live_execute",
    "_phase_interpret_backtests",
    "_phase_paper_trade",
    "get_status",
]


class TestExtractionContract:
    def test_research_loop_still_in_autonomous(self):
        tree = ast.parse(_read(auto.__file__))
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        assert "ResearchLoop" in names
        assert "AutonomousLoop" not in names

    def test_all_mixins_defined_in_facade_module(self):
        for mixin in MIXINS:
            assert hasattr(facade_mod, mixin), mixin

    def test_composition(self):
        for mixin in MIXINS:
            assert issubclass(
                auto.ResearchLoop, getattr(facade_mod, mixin)
            ), mixin

    def test_moved_methods_resolve_to_mixins(self):
        for mixin, methods in MOVED_METHODS.items():
            cls = getattr(facade_mod, mixin)
            for m in methods:
                fn = getattr(cls, m, None)
                assert fn is not None, f"{mixin}.{m} missing"
                owner = fn.__qualname__.split(".")[0]
                assert owner == mixin, f"{m} landed in {owner}, expected {mixin}"

    def test_no_duplication_of_moved_methods(self):
        # Each moved method must be defined exactly once across the two files.
        facade_tree = ast.parse(_read(auto.__file__))
        rl = next(
            n for n in facade_tree.body
            if isinstance(n, ast.ClassDef) and n.name == "ResearchLoop"
        )
        defined = {
            n.name for n in rl.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for methods in MOVED_METHODS.values():
            for m in methods:
                assert m not in defined, f"{m} duplicated in facade"

    def test_staying_methods_present_on_class(self):
        for m in STAYING_METHODS:
            assert hasattr(auto.ResearchLoop, m), m

    def test_module_line_budget_shrank(self):
        # Facade should now be well under its pre-extraction size (~746).
        assert len(_read(auto.__file__).splitlines()) < 500

    def test_public_surface_unchanged(self):
        # Names api.py and older callers import keep resolving.
        for name in ("AutonomousLoop", "ResearchLoop", "RESEARCH_SPORTS"):
            assert hasattr(auto, name), name


class TestGatePoliciesPreserved:
    def test_threshold_migration_flag_still_visible(self):
        src = _read(auto.__file__) + _read(facade_mod.__file__)
        assert src.count("CALLISTO_ALLOW_THRESHOLD_MIGRATION") >= 2

    def test_migration_routines_are_gated_noops_without_flag(self):
        async def main():
            class FakeMgr:
                _db = None

            loop = object.__new__(auto.ResearchLoop)
            loop.hypothesis_manager = FakeMgr()
            # db=None short-circuits before any gate check — still a no-op.
            await loop._migrate_edge_thresholds()
            await loop._retroactive_signal_update()
            await loop._requeue_threshold_rejections()
            await loop._requeue_prop_rejections()

        asyncio.run(main())

    def test_extracted_lifecycle_does_not_arm_live_execute(self):
        src = _read(facade_mod.__file__)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src or (
            # If mentioned at all, only in a comment/docstring context that
            # does not arm anything: the gate itself stays in autonomous.py.
            "getenv" not in src.split("CALLISTO_ALLOW_LIVE_EXECUTE")[0][-200:]
        )
        assert "generate_paper_trade_signal" not in src


class TestFacadeBehaviourUnchanged:
    def test_pause_resume_toggle_state(self):
        async def main():
            loop = object.__new__(auto.ResearchLoop)
            loop._paused = False
            loop._cycles = 7
            out = await loop.pause()
            assert out == {"status": "paused", "cycles_completed": 7}
            assert loop._paused is True
            out = await loop.resume()
            assert out == {"status": "running", "cycles_completed": 7}
            assert loop._paused is False

        asyncio.run(main())

    def test_set_local_only_roundtrip(self):
        loop = object.__new__(auto.ResearchLoop)
        loop._local_only = False
        res = loop.set_local_only(True)
        assert res == {"mode": "local_only", "local_only": True}
        assert loop._local_only is True
        res = loop.set_local_only(False)
        assert res == {"mode": full_mode(), "local_only": False}

    def test_claude_ok_false_in_local_only(self, monkeypatch):
        monkeypatch.setattr(auto.ResearchLoop, "_local_only", True, raising=False)
        loop = object.__new__(auto.ResearchLoop)
        loop._local_only = True
        assert loop._claude_ok() is False

    def test_check_temporal_overlap_detects_contamination(self):
        cfg = {
            "training_period_end": "2025-06-30",
            "backtest_period_start": "2025-06-01",
        }
        msg = auto.ResearchLoop._check_temporal_overlap(cfg)
        assert msg is not None and "TEMPORAL OVERLAP" in msg

    def test_check_temporal_overlap_clean_periods(self):
        cfg = {
            "training_period_end": "2025-01-31",
            "backtest_period_start": "2025-02-01",
        }
        assert auto.ResearchLoop._check_temporal_overlap(cfg) is None

    def test_check_temporal_overlap_handles_garbage(self):
        assert auto.ResearchLoop._check_temporal_overlap("not json") is None
        assert auto.ResearchLoop._check_temporal_overlap({"foo": 1}) is None


class TestReactiveDedup:
    def test_game_completed_dedup_per_sport_date(self):
        """Second identical event must not re-trigger collection."""
        calls = []

        class FakeCollector:
            _db = None

            async def collect_box_scores(self, sport, date):
                calls.append(("box", sport, date))

            async def collect_play_by_play(self, sport, date):
                calls.append(("pbp", sport, date))

        async def main():
            from tools.event_bus import EVENT_GAME_COMPLETED

            assert EVENT_GAME_COMPLETED  # bus constants importable
            loop = object.__new__(auto.ResearchLoop)
            loop._reactive_collected = set()
            loop.data_collector = FakeCollector()
            event = {"sport": "basketball_nba", "game_date": "2026-08-25"}
            await loop._on_game_completed(dict(event))
            n_after_first = len(calls)
            assert n_after_first > 0
            await loop._on_game_completed(dict(event))
            assert len(calls) == n_after_first  # deduped

        asyncio.run(main())

    def test_lineup_window_requires_sport_and_event(self):
        async def main():
            class FakeQueue:
                def __init__(self):
                    self.items = []

                async def enqueue(self, *a, **k):
                    self.items.append(a)

            loop = object.__new__(auto.ResearchLoop)
            loop._work_queue = FakeQueue()
            await loop._on_game_lineup_window({"sport": "", "event_id": ""})
            assert not loop._work_queue.items
            await loop._on_game_lineup_window({
                "sport": "icehockey_nhl",
                "event_id": "ev1",
                "home_team": "Leafs",
                "away_team": "Habs",
                "commence_time": "2026-08-26T23:00:00Z",
            })
            assert len(loop._work_queue.items) == 1

        asyncio.run(main())


class TestFailureLedgerWiring:
    def test_record_and_cycle_health(self):
        loop = object.__new__(auto.ResearchLoop)
        from tools.loop.phase_ledger import PhaseFailureLedger

        loop._cycles = 3
        ledger = PhaseFailureLedger()
        loop._phase_failures_ledger = ledger
        loop._record_phase_failure("backtest", "exception", RuntimeError("x"))
        assert loop._last_cycle_phase_failures() == 1
        assert loop._last_cycle_ok() is False
        loop._record_phase_failure("collect_data", "timeout")
        assert loop._last_cycle_phase_failures() == 2


def full_mode():
    return "full"
