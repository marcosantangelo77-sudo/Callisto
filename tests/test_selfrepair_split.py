"""Tests for the tools/selfrepair split (facade + package).

Verifies:
  1. The facade re-exports the entire public surface of the old module.
  2. Facade and package share state (singleton, mutable scraper-disable dict).
  3. Engine behaviour survives the split: repair cycle dispatch, gate refusals,
     finding classification and routing.
  4. Gate policy constants remain intact in the new location.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.self_repair as facade
import tools.selfrepair as pkg
from tools.selfrepair import (
    ALLOW_REQUEUE_ENV,
    GATE_WEAKENING_STRATEGIES,
    GATE_WRITE_PATTERNS,
    SelfRepairEngine,
    get_repair_engine,
)


# ── Facade re-exports ─────────────────────────────────────────────────────────

class TestFacadeSurface:
    def test_engine_class_identity(self):
        assert facade.SelfRepairEngine is pkg.SelfRepairEngine
        assert facade.get_repair_engine is pkg.get_repair_engine

    def test_heartbeat_exported(self):
        assert facade.Heartbeat is pkg.Heartbeat

    def test_gate_policy_constants_match(self):
        assert facade.GATE_WRITE_PATTERNS == GATE_WRITE_PATTERNS
        assert facade.GATE_WEAKENING_STRATEGIES == GATE_WEAKENING_STRATEGIES
        assert facade.ALLOW_REQUEUE_ENV == "CALLISTO_ALLOW_PREMATURE_REQUEUE"

    def test_threshold_constants_match(self):
        assert facade.STALE_ODDS_MINUTES == 30
        assert facade.EMPTY_BACKTEST_LOOKBACK == 10
        assert facade.REJECTION_RATE_THRESHOLD == 0.95
        assert facade.SIGNAL_DROUGHT_EVENTS == 500
        assert facade.DB_BLOAT_ROWS == 100_000
        assert facade.SCRAPER_DISABLE_SECONDS == 3600
        assert facade.HEARTBEAT_INTERVAL == 300
        assert facade.LOOP_STALL_THRESHOLD == 2400

    def test_prune_safe_alias(self):
        assert facade._PRUNE_SAFE is pkg.PRUNE_SAFE
        assert "backtest_events" in facade._PRUNE_SAFE

    def test_scrapers_table(self):
        assert set(facade.SCRAPERS) == {"dk", "fd"}
        assert facade.BETMGM_ALT_SUBDOMAINS == ["co", "pa", "va", "az"]

    def test_disabled_scrapers_is_shared_state(self):
        assert facade._disabled_scrapers is pkg._disabled_scrapers


# ── Singleton ────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_get_repair_engine_returns_same_instance(self):
        e1 = get_repair_engine()
        e2 = get_repair_engine()
        assert e1 is e2
        assert isinstance(e1, SelfRepairEngine)


# ── Engine behaviour after the split ─────────────────────────────────────────

class TestRepairCycleDispatch:
    def test_unknown_issue_type(self):
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair({"type": "no_such_issue"}))
        assert r["fixed"] is False
        assert r["action"] == "no_strategy"

    def test_high_rejection_refused_at_dispatch(self):
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair({"type": "high_rejection"}))
        assert r["action"] == "gate_change_refused"
        assert r["fixed"] is False

    def test_signal_drought_refused_at_dispatch(self):
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair({"type": "signal_drought"}))
        assert r["action"] == "gate_change_refused"

    def test_run_repair_cycle_counts(self):
        eng = SelfRepairEngine()

        async def fake_detect():
            return [{"type": "high_rejection"}, {"type": "nope"}]

        eng._detect_issues = fake_detect
        out = asyncio.run(eng.run_repair_cycle())
        assert out["issues_found"] == 2
        assert out["fixed"] == 0
        assert len(out["results"]) == 2
        status = eng.get_status()
        assert status["cycles"] == 1

    def test_status_reports_disabled_scrapers(self):
        import time
        eng = SelfRepairEngine()
        pkg._disabled_scrapers.clear()
        pkg._disabled_scrapers["dk"] = time.monotonic() + 100
        try:
            s = eng.get_status()
            assert "dk" in s["disabled_scrapers"]
        finally:
            pkg._disabled_scrapers.clear()


class TestPrematureRequeueGating:
    def _issue(self):
        return {"type": "premature_rejection",
                "candidates": [{"id": f"h{i}", "name": "n", "sport": "basketball_nba"}
                               for i in range(5)]}

    def test_refused_without_opt_in(self, monkeypatch):
        monkeypatch.delenv(ALLOW_REQUEUE_ENV, raising=False)
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair(self._issue()))
        assert r["action"] == "gate_change_refused"

    def test_refused_with_empty_env(self, monkeypatch):
        monkeypatch.setenv(ALLOW_REQUEUE_ENV, "")
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair(self._issue()))
        assert r["action"] == "gate_change_refused"


class TestFindingHandlers:
    def test_classify_finding_patterns(self):
        cases = [
            ("Hypotheses saw identical event sets", "duplicate_events"),
            ("side_filter not applied to totals", "side_filter_broken"),
            ("prioritize NBA over MLB", "prioritize_sports"),
            ("low sample size in backtests", "low_sample_size"),
            ("zero promotions this week", "promotion_thresholds_strict"),
            ("edge ceiling too high", "edge_ceiling"),
            ("resolution has date mismatch vs game_results", "resolution_broken"),
            ("something entirely novel", "unknown"),
        ]
        for desc, expected in cases:
            assert SelfRepairEngine.classify_finding(desc) == expected, desc

    def test_legacy_static_alias(self):
        assert facade.SelfRepairEngine._FINDING_PATTERNS == pkg.FindingsMixin.FINDING_PATTERNS
        assert facade.SelfRepairEngine._classify_finding("edge ceiling") == "edge_ceiling"

    def test_gate_weakening_finding_never_executes_handler(self):
        called = []

        async def boom(finding):
            called.append(finding)
            raise AssertionError("gate-lowering handler invoked")

        eng = SelfRepairEngine()
        eng._fix_finding_promotion_thresholds = boom
        results = asyncio.run(eng.handle_claude_findings(
            [{"severity": "HIGH", "description": "Zero promotions after 50 cycles"}]))
        assert not called
        assert results[0]["action"] == "gate_change_refused"
        assert results[0]["fixed"] is False

    def test_edge_ceiling_finding_never_executes_handler(self):
        called = []

        async def boom(finding):
            called.append(finding)

        eng = SelfRepairEngine()
        eng._fix_finding_edge_ceiling = boom
        results = asyncio.run(eng.handle_claude_findings(
            [{"severity": "CRITICAL", "description": "Edge ceiling blocks all signals"}]))
        assert not called
        assert results[0]["action"] == "gate_change_refused"

    def test_handler_error_is_caught_per_finding(self):
        eng = SelfRepairEngine()

        async def boom(finding):
            raise RuntimeError("kaboom")

        eng._fix_finding_duplicate_events = boom
        results = asyncio.run(eng.handle_claude_findings(
            [{"severity": "LOW", "description": "duplicate event sets detected"}]))
        assert results[0]["fixed"] is False
        assert results[0]["action"] == "handler_error"
        assert "kaboom" in results[0]["detail"]

    def test_explicit_refusers_still_refuse(self):
        eng = SelfRepairEngine()
        r1 = asyncio.run(eng._fix_thresholds({"type": "high_rejection"}))
        r2 = asyncio.run(eng._fix_finding_promotion_thresholds({}))
        r3 = asyncio.run(eng._fix_finding_edge_ceiling({}))
        for r in (r1, r2, r3):
            assert r["fixed"] is False
            assert r["action"] == "gate_change_refused"


class TestMixinComposition:
    def test_engine_composes_all_mixins(self):
        from tools.selfrepair.detectors import DetectorsMixin
        from tools.selfrepair.findings import FindingsMixin
        from tools.selfrepair.fixes import FixesMixin

        assert issubclass(SelfRepairEngine, DetectorsMixin)
        assert issubclass(SelfRepairEngine, FixesMixin)
        assert issubclass(SelfRepairEngine, FindingsMixin)

    def test_detectors_registered(self):
        eng = SelfRepairEngine()
        for name in ("_det_scrapers", "_det_stale_odds", "_det_empty_bt", "_det_claude",
                     "_det_rejection", "_det_drought", "_det_premature_rejection",
                     "_det_resolution_broken", "_det_bloat"):
            assert callable(getattr(eng, name)), name

    def test_fixers_registered(self):
        eng = SelfRepairEngine()
        for name in ("_fix_scraper", "_fix_stale_odds", "_fix_empty_bt", "_fix_claude",
                     "_fix_premature_rejection", "_fix_resolution_broken", "_fix_bloat"):
            assert asyncio.iscoroutinefunction(getattr(eng, name)), name


class TestGatePolicyIntact:
    """The split must not have weakened the gate policy."""

    def test_policy_declares_the_patterns(self):
        joined = "\n".join(GATE_WRITE_PATTERNS)
        assert "SET edge_threshold" in joined
        assert "minimum_events_for_promotion" in joined

    def test_weakening_strategies_cover_both(self):
        assert {"promotion_thresholds_strict", "edge_ceiling"} <= set(GATE_WEAKENING_STRATEGIES)

    def test_no_operative_gate_write_in_package(self):
        import pathlib
        root = pathlib.Path(pkg.__file__).parent
        forbidden = ("SET edge_threshold", "_threshold_lowered_by",
                     "_promotion_threshold_lowered_by", "_edge_ceiling_lowered_by")
        hits = []
        for path in sorted(root.glob("*.py")):
            if path.name == "gate_policy.py":
                continue  # policy declaration itself
            text = path.read_text()
            for line in text.splitlines():
                s = line.strip()
                if any(f in s for f in forbidden) and ("cfg[" in s or "execute(" in s):
                    hits.append(f"{path.name}: {s}")
        assert not hits, hits

    def test_no_detector_to_threshold_mapping(self):
        import ast
        import inspect
        import tools.selfrepair.engine as engine_mod
        tree = ast.parse(inspect.getsource(engine_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and k.value in ("high_rejection", "signal_drought"):
                        raise AssertionError(
                            "detector→threshold-lowering mapping reintroduced at "
                            f"line {node.lineno}")
