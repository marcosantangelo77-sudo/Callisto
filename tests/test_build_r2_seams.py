"""R2 S3 tests — ResearchLoop seam integration.

Verifies the R2 methods on the real ResearchLoop class (duck-typed init,
no DB/network/Claude):
  * record_iteration_outcome → calibration trace + phase-mapped task_class
  * compact_iteration_state  → disconfirming-biased compaction passthrough
  * get_status telemetry keys exist and are serializable
"""

from __future__ import annotations

import json

import pytest

import tools.autonomous as aut
from tools.loop_quality import LOOP_PHASE_TASK_CLASSES


DECLARED_TASK_CLASSES = {
    "hypothesis_generation", "research_synthesis", "screening", "extraction",
    "classification", "backtest_interpretation", "promotion_judgment",
    "adversarial_review",
}


def _make_loop():
    loop = object.__new__(aut.ResearchLoop)
    loop._running = False
    loop._paused = False
    loop._local_only = False
    loop._cycles = 0
    loop._promotions = 0
    loop._rejections = 0
    loop._backtests_run = 0
    loop._hypotheses_generated = 0
    loop._claude_escalations = 0
    from tools.loop_quality import LoopCalibrationTrace, LOOP_PHASE_TASK_CLASSES
    loop._calibration_trace = LoopCalibrationTrace(subject="test")
    loop.loop_phase_task_classes = dict(LOOP_PHASE_TASK_CLASSES)
    return loop


def test_record_iteration_outcome_maps_phases():
    loop = _make_loop()
    r1 = loop.record_iteration_outcome(0.3, {"confirming": 1}, position=0, total=5)
    assert r1["task_class"] == LOOP_PHASE_TASK_CLASSES["framing"]
    rm = loop.record_iteration_outcome(0.4, {"neutral": 2}, position=2, total=5)
    assert rm["task_class"] == LOOP_PHASE_TASK_CLASSES["evidence_grind"]
    rl = loop.record_iteration_outcome(0.8, {"disconfirming": 1}, position=4, total=5)
    assert rl["task_class"] == LOOP_PHASE_TASK_CLASSES["adversarial_review"]
    # Every emitted class is one the ProviderRouter declares.
    for r in (r1, rm, rl):
        assert r["task_class"] in DECLARED_TASK_CLASSES


def test_record_iteration_outcome_without_position_records_grindless():
    loop = _make_loop()
    r = loop.record_iteration_outcome(0.5, {"confirming": 1})
    assert r["task_class"] is None
    assert len(loop._calibration_trace.records) == 1


def test_trace_accumulates_across_iterations_and_exports_json():
    loop = _make_loop()
    for i, (c, ev) in enumerate(
        [(0.3, {"confirming": 2}), (0.55, {"disconfirming": 1}), (0.7, {"neutral": 3})]
    ):
        loop.record_iteration_outcome(c, ev)
    recs = loop._calibration_trace.to_records()
    json.dumps(recs)  # harness-consumable
    s = loop._calibration_trace.summary()
    assert s["iterations"] == 3
    assert s["disconfirming_seen"] == 1
    assert s["overconfidence_suspected"] is False


def test_compact_iteration_state_preserves_dissent():
    loop = _make_loop()
    items = [{"id": f"s{i}", "stance": "supporting", "tier": 1} for i in range(12)]
    items.append({"id": "dissent", "stance": "contradicting", "tier": 3})
    kept, dropped = loop.compact_iteration_state(items, max_supporting=5)
    ids = [k["id"] for k in kept]
    assert "dissent" in ids
    assert sum(1 for k in kept if k["stance"] == "supporting") == 5
    assert all("dropped_reason" in d for d in dropped)


def test_get_status_includes_calibration_telemetry(monkeypatch):
    loop = _make_loop()
    loop._data_collections = 0
    loop.line_monitor = None
    loop.hypothesis_manager = type("HM", (), {})()
    loop.hypothesis_manager._db = None
    loop._progress_window = []
    loop._spinning_detected = False
    loop._consecutive_no_progress = 0
    loop._diagnosis_fired_this_episode = False
    loop._work_queue = None
    loop._downtime_tracker = type("DT", (), {"get_status": lambda self: {}})()
    loop._last_regime_analysis = 0
    loop.vector_store = None
    loop.orchestrator = None

    import tools.claude_code as cc
    monkeypatch.setattr(cc, "get_usage_stats", lambda: {}, raising=False)

    status = loop.get_status()
    assert "calibration" in status
    assert "calibration_records" in status
    assert status["phase_task_classes"].keys() == LOOP_PHASE_TASK_CLASSES.keys()
