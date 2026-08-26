"""Slice 3: extraction of ResearchLoop helper groups into tools/auto/research.py.

Contract:
- tools/autonomous.py keeps ResearchLoop (composed from five mixins) plus
  lifecycle, reactive handlers, phase delegation, status reporting.
- tools/auto/research.py hosts MaintenanceMixin, DeferredQueueMixin,
  CycleLoopMixin, CorrelationMixin, ProgressMixin — behaviour-preserving.
- The live-execute env gate and threshold-migration gate policies are intact.
"""

import ast
import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous (mirrors slice2 harness).
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
import tools.auto.research as research_mod


def _read(path):
    with open(path) as f:
        return f.read()


MIXINS = (
    "MaintenanceMixin",
    "DeferredQueueMixin",
    "CycleLoopMixin",
    "CorrelationMixin",
    "ProgressMixin",
)

MOVED_METHODS = {
    "MaintenanceMixin": [
        "_backfill_temporal_metadata",
        "_migrate_edge_thresholds",
        "_retroactive_signal_update",
        "_requeue_threshold_rejections",
        "_requeue_prop_rejections",
        "_requeue_stale_signal_rejections",
        "_reject_anti_predictive",
        "_reject_low_signal_rate",
    ],
    "DeferredQueueMixin": [
        "_drain_deferred_queue",
        "_process_drained_item",
    ],
    "CycleLoopMixin": [
        "_loop",
        "_quant_scan_loop",
    ],
    "CorrelationMixin": [
        "_build_correlation_matrix",
        "_hyp_signals_n_map",
    ],
    "ProgressMixin": [
        "_check_progress",
        "_run_spinning_diagnosis",
    ],
}

STAYING_METHODS = [
    "__init__",
    "start",
    "stop",
    "pause",
    "resume",
    "set_local_only",
    "_claude_ok",
    "_on_game_completed",
    "_on_game_lineup_window",
    "_record_phase_failure",
    "_phase_self_repair",
    "_phase_collect_data",
    "_phase_backtest",
    "_phase_evaluate",
    "_phase_live_execute",
    "_phase_interpret_backtests",
    "_phase_paper_trade",
    "get_regime_for_team",
    "record_iteration_outcome",
    "compact_iteration_state",
    "_last_cycle_ok",
    "_last_cycle_phase_failures",
    "get_status",
]


class TestExtractionContract:
    def test_research_loop_still_in_autonomous(self):
        tree = ast.parse(_read(auto.__file__))
        names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        assert "ResearchLoop" in names
        assert "AutonomousLoop" not in names

    def test_all_mixins_defined_in_research_module(self):
        for mixin in MIXINS:
            assert hasattr(research_mod, mixin), mixin

    def test_composition(self):
        for mixin in MIXINS:
            assert issubclass(
                auto.ResearchLoop, getattr(research_mod, mixin)
            ), mixin

    def test_moved_methods_resolve_to_mixins(self):
        for mixin, methods in MOVED_METHODS.items():
            cls = getattr(research_mod, mixin)
            for m in methods:
                fn = getattr(cls, m, None)
                assert fn is not None, f"{mixin}.{m} missing"
                owner = fn.__qualname__.split(".")[0]
                assert owner == mixin, f"{m} landed in {owner}, expected {mixin}"

    def test_no_duplication_between_facade_and_mixins(self):
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
        # Facade should now be well under half its pre-extraction size (~2000).
        assert len(_read(auto.__file__).splitlines()) < 1000


class TestGatePoliciesPreserved:
    def test_threshold_migration_flag_count(self):
        # One mention per gated routine + docstrings; the flag must stay
        # visible at both layers.
        src = _read(auto.__file__) + _read(research_mod.__file__)
        assert src.count("CALLISTO_ALLOW_THRESHOLD_MIGRATION") >= 8

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

    def test_retroactive_signal_update_skips_without_flag(self, monkeypatch):
        """With a real DB handle but no opt-in flag: no writes happen."""
        import aiosqlite

        async def main():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute(
                    "CREATE TABLE backtest_events "
                    "(hypothesis_id TEXT, signal_generated INT DEFAULT 0, edge REAL)"
                )
                await db.execute(
                    "CREATE TABLE hypotheses (hypothesis_id TEXT, status TEXT, edge_threshold REAL)"
                )
                await db.execute(
                    "INSERT INTO hypotheses VALUES ('h1', 'backtesting', 0.015)"
                )
                await db.execute(
                    "INSERT INTO backtest_events VALUES ('h1', 0, 0.02)"
                )
                await db.commit()
                monkeypatch.delenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION", raising=False)

                class FakeMgr:
                    pass

                loop = object.__new__(auto.ResearchLoop)
                loop.hypothesis_manager = FakeMgr()
                loop.hypothesis_manager._db = db
                await loop._retroactive_signal_update()

                cur = await db.execute("SELECT signal_generated FROM backtest_events")
                row = await cur.fetchone()
                assert row[0] == 0, "evidence rewritten without operator opt-in"

        asyncio.run(main())

    def test_prop_requeue_gated(self, monkeypatch):
        import aiosqlite

        async def main():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute(
                    "CREATE TABLE hypotheses (hypothesis_id TEXT, status TEXT,"
                    " promoted_by TEXT, edge_threshold REAL)"
                )
                await db.execute(
                    "INSERT INTO hypotheses VALUES ('p1', 'rejected',"
                    " 'auto:untestable_no_prop_backtest', 0.02)"
                )
                await db.commit()
                monkeypatch.delenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION", raising=False)

                class FakeMgr:
                    pass

                loop = object.__new__(auto.ResearchLoop)
                loop.hypothesis_manager = FakeMgr()
                loop.hypothesis_manager._db = db
                await loop._requeue_prop_rejections()

                cur = await db.execute("SELECT status FROM hypotheses")
                assert (await cur.fetchone())[0] == "rejected"

        asyncio.run(main())

    def test_prop_requeue_applies_with_flag(self, monkeypatch):
        import aiosqlite

        async def main():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute(
                    "CREATE TABLE hypotheses (hypothesis_id TEXT, status TEXT,"
                    " promoted_by TEXT, edge_threshold REAL)"
                )
                await db.execute(
                    "INSERT INTO hypotheses VALUES ('p1', 'rejected',"
                    " 'auto:untestable_no_prop_backtest', 0.02)"
                )
                await db.commit()
                monkeypatch.setenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION", "1")

                class FakeMgr:
                    pass

                loop = object.__new__(auto.ResearchLoop)
                loop.hypothesis_manager = FakeMgr()
                loop.hypothesis_manager._db = db
                await loop._requeue_prop_rejections()

                cur = await db.execute("SELECT status, edge_threshold FROM hypotheses")
                status, thresh = await cur.fetchone()
                assert status == "draft"
                assert thresh == 0.003

        asyncio.run(main())

    def test_live_execute_gate_unchanged(self):
        src = _read(auto.__file__)
        assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
        # And it's checked BEFORE delegating to phases_impl.
        method = inspect.getsource(auto.ResearchLoop._phase_live_execute)
        assert '!= "1"' in method
        assert "return" in method.split('!= "1"', 1)[1].split("\n\n", 1)[0]

    def test_paper_trade_statuses_untouched(self):
        # tools/signals/paper.py must not have been modified by this slice.
        src = _read(os.path.join(os.path.dirname(auto.__file__), "signals", "paper.py"))
        # The signal-status set stays exactly {"paper_trading"} — never 'live'.
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
        # And no code path in the facade/mixins widens it.
        for path in (auto.__file__, research_mod.__file__):
            s = _read(path)
            assert "_PAPER_TRADE_SIGNAL_STATUSES" not in s, path
            assert "'live'" not in s.replace("liverpool", ""), path


class TestDrainedItemProcessing:
    def _make_loop(self):
        return object.__new__(auto.ResearchLoop)

    def test_hypothesis_gen_creates_from_json(self):
        loop = self._make_loop()
        created = []

        class FakeMgr:
            async def create_hypothesis(self, **kw):
                created.append(kw)

        loop.hypothesis_manager = FakeMgr()
        loop._cycles = 3
        loop._hypotheses_generated = 0
        content = json.dumps({
            "hypotheses": [
                {"name": "n1", "thesis": "t1", "sport": "icehockey_nhl"},
                {"name": "n2", "thesis": "t2"},
            ]
        })
        asyncio.run(loop._process_drained_item({"work_type": "hypothesis_gen"}, content))
        assert len(created) == 2
        assert created[0]["sport"] == "icehockey_nhl"
        assert created[1]["sport"] == "basketball_nba"  # default
        cfg = created[0]["model_config"]
        assert cfg["training_period_start"] == "2023-01-01"
        assert "forward_test_start" in cfg

    def test_fenced_json_is_unwrapped(self):
        loop = self._make_loop()
        created = []

        class FakeMgr:
            async def create_hypothesis(self, **kw):
                created.append(kw)

        loop.hypothesis_manager = FakeMgr()
        loop._cycles = 0
        loop._hypotheses_generated = 0
        content = "```json\n{\"hypotheses\": [{\"name\": \"fenced\"}]}\n```"
        asyncio.run(loop._process_drained_item({"work_type": "hypothesis_gen"}, content))
        assert created[0]["name"] == "fenced"

    def test_deep_work_rejects_and_routes_pipeline_issues(self):
        loop = self._make_loop()
        rejected = []

        class FakeMgr:
            async def update_status(self, hid, status, note):
                rejected.append((hid, status))

        loop.hypothesis_manager = FakeMgr()
        loop._cycles = 1
        loop._rejections = 0
        loop._hypotheses_generated = 0
        content = json.dumps({"reject_ids": ["a", "b"], "pipeline_issues": []})
        asyncio.run(loop._process_drained_item({"work_type": "deep_work"}, content))
        assert rejected == [("a", "rejected"), ("b", "rejected")]

    def test_interpret_modify_never_lowers_gate(self):
        """GATE POLICY: drain path refuses threshold lowering."""
        import aiosqlite

        async def main():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute(
                    "CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY,"
                    " edge_threshold REAL, notes TEXT)"
                )
                await db.execute("INSERT INTO hypotheses VALUES ('keep', 0.010, NULL)")
                await db.commit()

                class FakeDC:
                    _db = db

                class FakeMgr:
                    async def update_status(self, *a):
                        pass

                loop = object.__new__(auto.ResearchLoop)
                loop.data_collector = FakeDC()
                loop.hypothesis_manager = FakeMgr()
                loop._cycles = 7
                content = json.dumps({
                    "modify": [
                        {"id": "keep", "new_threshold": 0.005},   # lowering → refuse
                        {"id": "keep", "new_threshold": 25.0},    # clamp to ceiling
                        {"id": "ghost", "new_threshold": 0.05},   # unknown hyp → skip
                        {"id": "keep", "new_threshold": None},    # malformed → skip
                    ],
                })
                await loop._process_drained_item(
                    {"work_type": "interpret_backtests"}, content
                )
                cur = await db.execute("SELECT edge_threshold, notes FROM hypotheses")
                row = await cur.fetchone()
                # Lowering refused; ceiling-clamped raise applied.
                assert row[0] == 0.10
                assert "REFUSED deferred-drain threshold lowering" in (row[1] or "")

        asyncio.run(main())

    def test_system_improvement_stored(self):
        import aiosqlite

        async def main():
            async with aiosqlite.connect(":memory:") as db:
                await db.execute(
                    "CREATE TABLE system_improvements "
                    "(cycle INT, category TEXT, suggestion TEXT, priority TEXT)"
                )

                class FakeDC:
                    _db = db

                loop = object.__new__(auto.ResearchLoop)
                loop.data_collector = FakeDC()
                loop._cycles = 11
                content = json.dumps({
                    "improvements": [{"category": "cadence", "suggestion": "s1"}]
                })
                await loop._process_drained_item(
                    {"work_type": "system_improvement"}, content
                )
                cur = await db.execute("SELECT cycle, category FROM system_improvements")
                assert (await cur.fetchone()) == (11, "cadence")

        asyncio.run(main())


class TestTemporalOverlapHelper:
    def test_overlap_detected(self):
        msg = auto.ResearchLoop._check_temporal_overlap({
            "training_period_end": "2026-02-22",
            "backtest_period_start": "2026-01-01",
        })
        assert msg is not None and "TEMPORAL OVERLAP" in msg

    def test_clean_split_passes(self):
        assert auto.ResearchLoop._check_temporal_overlap({
            "training_period_end": "2026-02-22",
            "backtest_period_start": "2026-03-01",
        }) is None

    def test_missing_dates_pass(self):
        assert auto.ResearchLoop._check_temporal_overlap({}) is None

    def test_string_config_tolerated(self):
        cfg = json.dumps({
            "training_period_end": "2026-02-22",
            "backtest_period_start": "2026-02-01",
        })
        assert "TEMPORAL OVERLAP" in auto.ResearchLoop._check_temporal_overlap(cfg)


class TestCorrelationMatrix:
    def test_jaccard_matrix(self):
        import aiosqlite

        async def main():
            db = await aiosqlite.connect(":memory:")
            await db.execute(
                "CREATE TABLE backtest_events (hypothesis_id TEXT, event_id TEXT,"
                " signal_generated INT, created_at TEXT)"
            )
            now = "2099-01-01T00:00:00+00:00"
            await db.executemany(
                "INSERT INTO backtest_events VALUES (?,?,?,?)",
                [("a", "e1", 1, now), ("b", "e1", 1, now),
                 ("a", "e2", 1, now)],
            )
            await db.commit()

            class FakeDC:
                _db = db

            loop = object.__new__(auto.ResearchLoop)
            loop.data_collector = FakeDC()
            matrix = await loop._build_correlation_matrix(["a", "b"])
            # fired(a)={e1,e2}, fired(b)={e1}: Jaccard = 1 / |{e1,e2}| = 0.5
            assert matrix[("a", "b")] == 0.5
            await db.close()

        asyncio.run(main())

    def test_empty_ids_returns_empty(self):
        async def main():
            class FakeDC:
                _db = object()

            loop = object.__new__(auto.ResearchLoop)
            loop.data_collector = FakeDC()
            assert await loop._hyp_signals_n_map([]) == {}

        asyncio.run(main())


class TestProgressTracking:
    def _make_loop(self, cycles=20, promotions=1, rejections=2,
                   backtests=3, hyps=4, claude=5):
        loop = object.__new__(auto.ResearchLoop)
        loop._cycles = cycles
        loop._promotions = promotions
        loop._rejections = rejections
        loop._backtests_run = backtests
        loop._hypotheses_generated = hyps
        loop._claude_escalations = claude
        loop._progress_window = []
        loop._consecutive_no_progress = 0
        loop._spinning_detected = False
        loop._diagnosis_fired_this_episode = False
        return loop

    def test_check_skipped_off_interval(self):
        loop = self._make_loop(cycles=7)
        asyncio.run(self._run_check(loop, expect_snapshot=False))

    async def _run_check(self, loop, expect_snapshot=True):
        class FakeDBExec:
            def __init__(self, v):
                self.v = v

            async def fetchone(self):
                return [self.v]

        class FakeCursor:
            async def execute(self, *a, **k):
                return self

            async def fetchone(self):
                return [42]

        class FakeMgr:
            _db = FakeCursor()

        loop.hypothesis_manager = FakeMgr()
        await loop._check_progress()

    def test_snapshot_recorded_on_interval(self):
        loop = self._make_loop(cycles=10)
        asyncio.run(self._run_check(loop))
        assert len(loop._progress_window) == 1
        snap = loop._progress_window[0]
        assert snap["cycle"] == 10
        assert snap["promotions"] == 1
        assert snap["total_signals"] == 42

    def test_progressing_resets_streak(self):
        loop = self._make_loop(cycles=10, promotions=5)
        loop._consecutive_no_progress = 4
        loop._diagnosis_fired_this_episode = True
        asyncio.run(self._run_check(loop))
        assert loop._consecutive_no_progress == 0
        assert loop._diagnosis_fired_this_episode is False

    def test_window_capped_at_five(self):
        loop = self._make_loop()
        loop._cycles = 50
        for i in range(60):
            loop._progress_window.append({"cycle": i})
        asyncio.run(self._run_check(loop))
        assert len(loop._progress_window) <= 5


class TestReactiveHandlers:
    def test_game_completed_dedups_per_sport_date(self):
        calls = []

        class FakeCollector:
            async def collect_box_scores(self, sport, date):
                calls.append(("box", sport, date))

            async def collect_play_by_play(self, sport, date):
                calls.append(("pbp", sport, date))

            _db = None

        loop = object.__new__(auto.ResearchLoop)
        loop.data_collector = FakeCollector()
        loop._reactive_collected = set()
        evt = {"sport": "icehockey_nhl", "game_date": "2026-08-01"}
        asyncio.run(loop._on_game_completed(evt))
        asyncio.run(loop._on_game_completed(evt))  # deduped
        kinds = [c[0] for c in calls]
        assert kinds.count("box") == 1 and kinds.count("pbp") == 1
        assert ("box", "icehockey_nhl", "20260801") in calls

    def test_game_completed_requires_fields(self):
        loop = object.__new__(auto.ResearchLoop)
        loop.data_collector = None
        loop._reactive_collected = set()
        asyncio.run(loop._on_game_completed({"sport": ""}))  # returns early

    def test_lineup_window_enqueues_rescan(self):
        enqueued = []

        class FakeQueue:
            async def enqueue(self, kind, query, priority=0):
                enqueued.append((kind, priority))

        loop = object.__new__(auto.ResearchLoop)
        loop._work_queue = FakeQueue()
        evt = {
            "sport": "basketball_nba", "event_id": "e1",
            "home_team": "Celtics", "away_team": "Lakers",
            "commence_time": "t",
        }
        asyncio.run(loop._on_game_lineup_window(evt))
        assert enqueued == [("lineup_rescan", 1)]

    def test_lineup_window_requires_event_id(self):
        loop = object.__new__(auto.ResearchLoop)
        loop._work_queue = None
        asyncio.run(loop._on_game_lineup_window({"sport": "x"}))


class TestStatusShape:
    def test_get_status_keys(self):
        loop = object.__new__(auto.ResearchLoop)
        loop._running = False
        loop._paused = False
        loop._local_only = False
        loop._cycles = 1
        loop._data_collections = 0
        loop._hypotheses_generated = 0
        loop._backtests_run = 0
        loop._claude_escalations = 0
        loop._promotions = 0
        loop._rejections = 0
        from tools.loop.phase_ledger import PhaseFailureLedger
        ledger = PhaseFailureLedger()
        loop._phase_failures_ledger = ledger
        loop._calibration_trace = type(
            "CT", (), {"summary": lambda s: {}, "to_records": lambda s: []}
        )()
        loop.loop_phase_task_classes = {}
        loop._work_queue = None
        loop._downtime_tracker = type(
            "DT", (), {"get_status": lambda s: {}}
        )()
        loop._spinning_detected = False
        loop._consecutive_no_progress = 0
        loop._progress_window = []
        loop._last_regime_analysis = 0

        status = loop.get_status()
        for key in (
            "running", "paused", "mode", "cycles_completed", "promotions",
            "rejections", "phase_failures", "last_cycle_ok",
            "calibration", "research_sports", "progress", "regime_analysis",
            "intervals",
        ):
            assert key in status, key
        assert status["intervals"]["research_cycle_seconds"] > 0
