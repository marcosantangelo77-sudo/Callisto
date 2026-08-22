"""Tier 1 audit — gate-policy enforcement tests for tools/self_repair.py.

Governing principle under enforcement: **a maintenance routine must never be
able to weaken a gate.**

These tests are both behavioural (the refusers refuse) and structural (a static
scan of self_repair.py so no future edit can reintroduce an operative gate
write without a loud diff to GATE_WRITE_PATTERNS / GATE_WEAKENING_STRATEGIES).
"""

import asyncio
import inspect
import os
import sys
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.self_repair as sr
from tools.self_repair import (
    ALLOW_REQUEUE_ENV,
    GATE_WEAKENING_STRATEGIES,
    GATE_WRITE_PATTERNS,
    SelfRepairEngine,
)


# ── Behavioural: the three former lowering paths now refuse ──────────────────

class TestLoweringPathsRefuse:
    """The three ROADMAP §3.1 lowering paths must refuse, never write."""

    def test_fix_thresholds_refuses(self):
        eng = SelfRepairEngine()
        r = asyncio.run(eng._fix_thresholds({"type": "high_rejection"}))
        assert r["fixed"] is False
        assert r["action"] == "gate_change_refused"

    def test_fix_finding_promotion_thresholds_refuses(self):
        eng = SelfRepairEngine()
        r = asyncio.run(
            eng._fix_finding_promotion_thresholds({"severity": "HIGH", "description": "zero promotions"}))
        assert r["fixed"] is False
        assert r["action"] == "gate_change_refused"

    def test_fix_finding_edge_ceiling_refuses(self):
        eng = SelfRepairEngine()
        r = asyncio.run(
            eng._fix_finding_edge_ceiling({"severity": "CRITICAL", "description": "edge ceiling too high"}))
        assert r["fixed"] is False
        assert r["action"] == "gate_change_refused"


class TestFindingRouting:
    """Claude findings classified as gate-weakening are refused before any handler runs."""

    def test_gate_weakening_strategies_cover_both(self):
        assert {"promotion_thresholds_strict", "edge_ceiling"} <= set(GATE_WEAKENING_STRATEGIES)

    def test_zero_promotion_finding_never_lowers(self):
        called = []

        async def boom(finding):  # if this runs, the guard failed
            called.append(finding)
            raise AssertionError("gate-lowering handler was invoked")

        eng = SelfRepairEngine()
        eng._fix_finding_promotion_thresholds = boom
        results = asyncio.run(eng.handle_claude_findings(
            [{"severity": "HIGH", "description": "Zero promotions after 50 cycles"}]))
        assert not called
        assert results[0]["action"] == "gate_change_refused"
        assert results[0]["fixed"] is False

    def test_edge_ceiling_finding_never_lowers(self):
        called = []

        async def boom(finding):
            called.append(finding)
            raise AssertionError("edge-ceiling handler was invoked")

        eng = SelfRepairEngine()
        eng._fix_finding_edge_ceiling = boom
        results = asyncio.run(eng.handle_claude_findings(
            [{"severity": "LOW", "description": "Max edge threshold too high for this market"}]))
        assert not called
        assert results[0]["action"] == "gate_change_refused"


class TestDetectorRouting:
    """high_rejection / signal_drought detector issues must not reach threshold lowering."""

    def test_high_rejection_refused_at_dispatch(self):
        called = []

        async def boom(issue):
            called.append(issue)
            raise AssertionError("_fix_thresholds invoked from _repair dispatch")

        eng = SelfRepairEngine()
        issue = {"type": "high_rejection", "rejected": 3000, "promoted": 0}
        # Simulate the old mapping by pointing the name back in — dispatch must
        # refuse BEFORE consulting any handler table.
        with unittest.mock.patch.object(sr.SelfRepairEngine, "_fix_thresholds", boom):
            r = asyncio.run(eng._repair(issue))
        assert not called
        assert r["action"] == "gate_change_refused"

    def test_signal_drought_refused_at_dispatch(self):
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair({"type": "signal_drought", "total_events": 5000, "signals": 0}))
        assert r["action"] == "gate_change_refused"


class TestPrematureRequeueGated:
    """rejected->draft requeue requires explicit operator opt-in."""

    def _candidates_issue(self):
        return {"type": "premature_rejection",
                "candidates": [{"id": f"h{i}", "name": "n", "sport": "basketball_nba"}
                               for i in range(5)]}

    def test_requeue_refused_without_opt_in(self, monkeypatch):
        monkeypatch.delenv(ALLOW_REQUEUE_ENV, raising=False)
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair(self._candidates_issue()))
        assert r["action"] == "gate_change_refused"

    def test_requeue_still_gated_when_env_set_to_empty(self, monkeypatch):
        monkeypatch.setenv(ALLOW_REQUEUE_ENV, "")
        eng = SelfRepairEngine()
        r = asyncio.run(eng._repair(self._candidates_issue()))
        assert r["action"] == "gate_change_refused"


# ── Structural: static scan so operative gate writes cannot silently return ──

def _handler_sources():
    src = inspect.getsource(sr)
    return src


class TestStaticGatePolicy:
    """Any reintroduction of these patterns must fail this test loudly."""

    def test_no_operative_edge_threshold_column_write(self):
        src = _handler_sources()
        for line in src.splitlines():
            s = line.strip()
            if "SET edge_threshold" in s and ("execute" in s or 'f"' in s or "f'" in s):
                raise AssertionError(
                    f"self_repair writes the operative edge_threshold column: {s}")

    def test_no_lowered_by_markers_written(self):
        src = _handler_sources()
        for marker in ("_threshold_lowered_by", "_promotion_threshold_lowered_by",
                       "_edge_ceiling_lowered_by"):
            # Allowed ONLY inside GATE_WRITE_PATTERNS declaration (policy) or
            # comments/docstrings; forbidden as an executed cfg assignment.
            for line in src.splitlines():
                s = line.strip()
                if marker in s and ('cfg[' in s or 'cfg.update' in s):
                    raise AssertionError(f"gate marker written to config: {line.strip()}")

    def test_minimum_events_for_promotion_only_in_policy_or_comments(self):
        src = _handler_sources()
        for line in src.splitlines():
            s = line.strip()
            if "minimum_events_for_promotion" in s and 'cfg[' in s:
                raise AssertionError(f"promotion knob written to config: {line.strip()}")

    def test_policy_declares_the_patterns(self):
        """If someone removes a pattern from GATE_WRITE_PATTERNS to slip a write
        past the scan above, this fails — the policy itself is load-bearing."""
        joined = "\n".join(GATE_WRITE_PATTERNS)
        assert "SET edge_threshold" in joined
        assert "minimum_events_for_promotion" in joined

    def test_dispatch_table_has_no_threshold_handler(self):
        import ast
        tree = ast.parse(inspect.getsource(sr))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and k.value in ("high_rejection", "signal_drought"):
                        raise AssertionError(
                            "detector→threshold-lowering mapping reintroduced at "
                            f"line {node.lineno}")
