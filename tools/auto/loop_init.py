"""ResearchLoop.__init__ body extracted from tools/autonomous.py.

The facade keeps ``__init__`` defined (signature + one call) so earlier
slices' ``STAYING_METHODS`` / ``hasattr`` pins still pass. Wiring of the
phase-failure ledger, calibration trace, work queue, and local-only flag
lives here.

Does not import ``tools.autonomous`` (no cycles). Does not arm live
betting. ``CALLISTO_LOCAL_ONLY`` only sets the loop's local-only flag;
it does not enable BetExecutor or widen paper-signal statuses.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

from tools.loop.phase_ledger import PhaseFailureLedger


def init_research_loop(
    loop,
    hypothesis_manager,
    hypothesis_generator,
    backtest_engine,
    data_collector,
    vector_store,
    orchestrator=None,
    line_monitor=None,
) -> None:
    """Populate ResearchLoop instance attributes (historical __init__ body)."""
    loop.hypothesis_manager = hypothesis_manager
    loop.hypothesis_generator = hypothesis_generator
    loop.backtest_engine = backtest_engine
    loop.data_collector = data_collector
    loop.vector_store = vector_store
    loop.orchestrator = orchestrator
    loop.line_monitor = line_monitor

    loop._running = False
    loop._task: Optional[asyncio.Task] = None

    # Timestamps for cadence control
    loop._last_data_collect = 0.0
    loop._last_hypothesis_gen = 0.0
    loop._last_claude_call = 0.0

    # Bulk backfill tracking — one-time 30-day seed when data is thin
    loop._bulk_backfill_done = False

    # Counters
    loop._cycles = 0
    loop._data_collections = 0
    loop._hypotheses_generated = 0
    loop._backtests_run = 0
    loop._claude_escalations = 0
    loop._promotions = 0
    loop._rejections = 0

    # Phase-failure ledger — every _phase_* exception/timeout is recorded
    # here so a "healthy-looking" loop can't silently swallow failures.
    # Capped at 50 entries; oldest dropped when full.
    loop._phase_failures_ledger = PhaseFailureLedger()

    # Self-diagnostics — track already-escalated issues to avoid spam
    # Capped at 500 entries; oldest keys evicted when full.
    loop._diagnostic_issues: set[str] = set()
    loop._DIAGNOSTIC_ISSUES_MAX = 500

    # ── Progress tracking (Ralph loop: detect spinning) ──
    loop._progress_window: list[dict] = []  # last N cycle snapshots
    PROGRESS_WINDOW_SIZE = 10  # look at last 10 cycles
    loop._spinning_detected = False
    loop._last_progress_check = 0
    loop._consecutive_no_progress = 0
    # R2: the spinning diagnosis must fire ONCE per spin episode, not on
    # every subsequent stagnant check. Reset when progress resumes.
    loop._diagnosis_fired_this_episode = False

    # ── R2 loop-quality state ──
    # Calibration trace: per-iteration confidence/evidence ledger, the
    # record shape R1's retrodiction harness scores against outcomes.
    from tools.loop_quality import LoopCalibrationTrace
    loop._calibration_trace = LoopCalibrationTrace(subject="research_loop")
    # Per-phase task-class allocation for the ProviderRouter: framing
    # (first) and adversarial review (last) get capability tiers, the
    # middle grind routes to extraction-class endpoints.
    from tools.loop_quality import LOOP_PHASE_TASK_CLASSES
    loop.loop_phase_task_classes = dict(LOOP_PHASE_TASK_CLASSES)

    # Regime analysis — uses module-level _regime_cache (shared with AutonomousLoop)
    # Refreshed every REGIME_ANALYSIS_INTERVAL cycles
    loop._last_regime_analysis = 0

    # Dedup reactive game completion handlers — prevents 14×14 ESPN calls
    # when 14 games complete on the same date. Cleared each research cycle.
    loop._reactive_collected: set[tuple[str, str]] = set()

    # Deferred work queue + downtime tracker (never-idle loop)
    from tools.work_queue import get_work_queue, get_downtime_tracker
    loop._work_queue = get_work_queue()
    loop._downtime_tracker = get_downtime_tracker()
    loop._was_claude_available = True  # track transitions

    # Mode control
    loop._paused = False
    loop._local_only = os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes")
