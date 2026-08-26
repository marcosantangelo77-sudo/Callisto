"""ResearchLoop status dict — extracted from tools/autonomous.py.

``ResearchLoop.get_status`` stays defined on the facade as a one-line
delegate so earlier slices' ``hasattr`` pins keep passing. The keys
(phase-failure ledger, last-cycle health, calibration telemetry) live
here so the facade can keep shrinking without losing the reporting
contract.

Does not import ``tools.autonomous`` (no cycles). Does not arm live
betting. Does not touch paper-signal statuses.
"""

from __future__ import annotations

import asyncio

from tools.loop.phases_impl import (
    CLAUDE_ESCALATION_COOLDOWN,
    DATA_COLLECTION_INTERVAL,
    HYPOTHESIS_GEN_INTERVAL,
    REGIME_ANALYSIS_INTERVAL,
    RESEARCH_CYCLE_INTERVAL,
    RESEARCH_SPORTS,
    _regime_cache,
)


def build_research_loop_status(loop) -> dict:
    """Return research loop status (same dict ``ResearchLoop.get_status``)."""
    from tools.claude_code import get_usage_stats as claude_stats
    from tools.pipeline_integrity import get_checker

    # Include pipeline integrity info
    integrity_report = get_checker().get_latest_report()

    # Include work queue status (async call — best-effort)
    work_queue_status = {}
    try:
        work_queue_status = asyncio.get_event_loop().run_until_complete(
            loop._work_queue.get_status()
        ) if not asyncio.get_event_loop().is_running() else {}
    except Exception:
        pass

    return {
        "running": loop._running,
        "paused": loop._paused,
        "local_only": loop._local_only,
        "mode": "paused" if loop._paused else ("local_only" if loop._local_only else "full"),
        "cycles_completed": loop._cycles,
        "data_collections": loop._data_collections,
        "hypotheses_generated": loop._hypotheses_generated,
        "backtests_run": loop._backtests_run,
        "claude_escalations": loop._claude_escalations,
        "promotions": loop._promotions,
        "rejections": loop._rejections,
        # Phase-failure ledger: last 10 failures + total count so a
        # "healthy-looking" loop can't hide swallowed phase errors.
        "phase_failures": loop._phase_failures_ledger.latest(10),
        "phase_failure_count": loop._phase_failures_ledger.count,
        # Per-cycle health: False when any phase failed during the most
        # recent cycle (failures are non-fatal, but the loop is NOT ok).
        "last_cycle_ok": loop._last_cycle_ok(),
        "last_cycle_phase_failures": loop._last_cycle_phase_failures(),
        # R2: loop-quality telemetry — calibration trace + per-phase
        # task-class map, consumed by R1's retrodiction harness.
        "calibration": loop._calibration_trace.summary(),
        "calibration_records": loop._calibration_trace.to_records()[-20:],
        "phase_task_classes": dict(loop.loop_phase_task_classes),
        "research_sports": RESEARCH_SPORTS,
        "claude_code": claude_stats(),
        "pipeline_integrity": integrity_report,
        "work_queue": work_queue_status,
        "claude_downtime": loop._downtime_tracker.get_status(),
        "progress": {
            "spinning_detected": loop._spinning_detected,
            "consecutive_no_progress": loop._consecutive_no_progress,
            "window": loop._progress_window[-3:] if loop._progress_window else [],
        },
        "regime_analysis": {
            "teams_cached": len(_regime_cache),
            "teams_with_signals": sum(
                1 for v in _regime_cache.values()
                if v.get("has_edge_signal")
            ),
            "last_run": loop._last_regime_analysis,
            "interval_cycles": REGIME_ANALYSIS_INTERVAL,
        },
        "intervals": {
            "research_cycle_seconds": RESEARCH_CYCLE_INTERVAL,
            "data_collection_seconds": DATA_COLLECTION_INTERVAL,
            "hypothesis_gen_seconds": HYPOTHESIS_GEN_INTERVAL,
            "claude_cooldown_seconds": CLAUDE_ESCALATION_COOLDOWN,
        },
    }
