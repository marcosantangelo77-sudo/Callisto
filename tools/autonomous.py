"""
Autonomous reasoning loop — makes Callisto think without being asked.

Two loops run concurrently:
  1. AutonomousLoop — real-time edge detection (tools/auto/loop.py, NEW)
  2. ResearchLoop — 24/7 hypothesis machine (composed below)

ResearchLoop cycle:
  - Collect post-game data (ESPN scores, box scores) — FREE
  - Embed game contexts and prop outcomes into vector store
  - Generate hypotheses (Claude Code PRIMARY, templates FALLBACK)
  - Backtest hypotheses against historical data
  - Evaluate significance, auto-promote or auto-reject
  - Claude interprets backtest results (signal vs noise, threshold mods)
  - Paper trade promoted hypotheses on live odds
  - Claude deep analysis — actionable hypothesis/rejection work
  - System self-improvement (every 10 cycles) — pipeline optimization

Claude Code is the PRIMARY reasoning engine. Local models stay only
for fast classification (Sentinel) and embeddings.

GATE POLICY — threshold migration opt-in:
CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 is the ONLY arming switch for the
startup maintenance routines that lower operative gates or rewrite
evidence: _migrate_edge_thresholds, _retroactive_signal_update,
_requeue_threshold_rejections and _requeue_prop_rejections
(now in tools/auto/research.py via MaintenanceMixin). Without that
flag each is a logged no-op.

GATE POLICY — live execution opt-in:
_phase_live_execute below stays in this facade with its
CALLISTO_ALLOW_LIVE_EXECUTE=1 env gate as the first executable
statement; that flag is the ONLY arming switch and it is pinned here
by tests/test_live_execute_gate.py.
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("callisto.autonomous")

from tools.loop import phases_impl  # noqa: E402
from tools.loop.phase_ledger import PhaseFailureLedger

# ── Re-exports from tools.loop.phases_impl ──────────────────────────────────
# The cadence constants, sport tables, gate-policy bounds, regime cache and
# wiki-in-the-loop helpers moved to phases_impl with the phase bodies; keep
# the module-level names importable here for existing callers (api.py etc).
from tools.loop.phases_impl import (  # noqa: E402,F401
    BACKTEST_BATCH_SIZE,
    BACKTEST_GAP_DAYS,
    CLAUDE_ESCALATION_COOLDOWN,
    DATA_COLLECTION_INTERVAL,
    DEFAULT_TRAINING_WINDOW_DAYS,
    HYPOTHESIS_GEN_INTERVAL,
    MAX_EDGE_THRESHOLD_CEILING,
    MIN_EDGE_THRESHOLD_FLOOR,
    MIN_GAMES_FOR_HYPOTHESIS,
    REGIME_ANALYSIS_INTERVAL,
    RESEARCH_CYCLE_INTERVAL,
    RESEARCH_SPORTS,
    SPORT_PRIORITY,
    SYSTEM_IMPROVEMENT_INTERVAL,
    _fetch_wiki_priors,
    _regime_cache,
    _render_wiki_priors_block,
    _wiki_in_loop_enabled,
)

# ── Extracted slices: AutonomousLoop + ResearchLoop helpers live in tools.auto ─
from tools.auto.research import (  # noqa: F401 — re-exported mixin names
    CorrelationMixin,
    CycleLoopMixin,
    DeferredQueueMixin,
    MaintenanceMixin,
    ProgressMixin,
)
from tools.auto import research as research_mod  # noqa: F401
from tools.auto.loop import (  # noqa: E402,F401 — re-exported loop constants
    ANALYSIS_COOLDOWN,
    EDGE_DEDUP_WINDOW,
    MIN_CONFIDENCE_TO_ALERT,
    MIN_IMPLIED_RANGE,
    MIN_SOFT_EDGE_VS_SHARP,
    _SPORT_TO_MODEL,
    AutonomousLoop,
)
from tools.auto.facade import (  # noqa: F401 — re-exported mixin names
    CalibrationMixin,
    FailureLedgerMixin,
    LifecycleMixin,
    ReactiveMixin,
    RegimeMixin,
)
from tools.loop.cycle_health import last_cycle_ok, last_cycle_phase_failures
from tools.loop.sequencer import PERIODIC_PHASES, PHASES  # noqa: F401


# GATE POLICY bounds for automated threshold modification (_phase_interpret_backtests).
# An automated actor may raise a hypothesis's edge_threshold (tightening the gate)
# but never lower it; refusals are logged to hypothesis notes for human review.
MIN_EDGE_THRESHOLD_FLOOR = 0.005   # never below the creation default (hypothesis.py:488)
MAX_EDGE_THRESHOLD_CEILING = 0.10  # sanity clamp against LLM garbage (e.g. 25.0)

# Keep the historical module-level name importable from tools.loop.phases_impl.
MIN_EDGE_THRESHOLD_FLOOR_DEFAULT = MIN_EDGE_THRESHOLD_FLOOR  # noqa: E305


class ResearchLoop(
    MaintenanceMixin,
    DeferredQueueMixin,
    CycleLoopMixin,
    CorrelationMixin,
    ProgressMixin,
    LifecycleMixin,
    ReactiveMixin,
    FailureLedgerMixin,
    RegimeMixin,
    CalibrationMixin,
):
    """
    24/7 autonomous research engine — Claude Code is the primary reasoning engine.

    Runs independently of AutonomousLoop. While AutonomousLoop handles
    real-time edge detection and alerting, ResearchLoop handles the
    slow, deep work: collecting data, discovering patterns, generating
    and testing hypotheses, interpreting results, and self-improving.

    The method groups live in tools/auto as mixins:

    From tools/auto/research.py:
      - MaintenanceMixin: one-time startup migrations/sweeps
        (_backfill_temporal_metadata, _migrate_edge_thresholds behind the
        CALLISTO_ALLOW_THRESHOLD_MIGRATION gate, rejection requeues,
        anti-predictive / low-signal-rate rejections)
      - DeferredQueueMixin: deferred work queue drain + drained-item processing
      - CycleLoopMixin: the main research cycle (`_loop`) and quant scan loop
      - CorrelationMixin: pairwise hypothesis correlation matrix helpers
      - ProgressMixin: Ralph-loop progress tracking, spinning detection,
        data-driven spinning diagnosis

    From tools/auto/facade.py (slice 4):
      - LifecycleMixin: start/stop/pause/resume/mode controls
      - ReactiveMixin: event-bus reactive handlers
      - FailureLedgerMixin: phase-failure recording + cycle health checks
      - RegimeMixin: cached regime lookup
      - CalibrationMixin: R2 loop-quality seams

    Still defined in this class body (pinned here by earlier slices' tests):
    the thin ``_phase_*`` delegation wrappers to tools.loop.phases_impl,
    thin ``get_status`` / ``__init__`` / ``_check_temporal_overlap``
    delegates, and the gated _phase_live_execute (its
    CALLISTO_ALLOW_LIVE_EXECUTE env gate must remain the first executable
    statement — see tests/test_live_execute_gate.py).

    GATE POLICY: four MaintenanceMixin startup routines are gated behind the
    operator opt-in flag CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 and are no-ops
    without it:
      * _migrate_edge_thresholds     — lowers operative edge_threshold gates
                                       on draft/backtesting hypotheses;
      * _retroactive_signal_update   — rewrites historical signal_generated
                                       evidence to match a lowered gate;
      * _requeue_threshold_rejections— un-rejects hypotheses AND lowers their
                                       operative gates;
      * _requeue_prop_rejections     — un-rejects prop hypotheses and sets
                                       edge_threshold = 0.003.
    Each routine carries its own CALLISTO_ALLOW_THRESHOLD_MIGRATION check in
    tools/auto/research.py; that opt-in is the ONLY arming switch for
    threshold migration. Without the flag they log what they WOULD have done
    and change nothing.

    This class keeps __init__ and the gated _phase_live_execute (which must
    stay defined HERE so its CALLISTO_ALLOW_LIVE_EXECUTE env gate remains
    the first executable statement, pinned by tests).

    Reminder: every threshold-migration routine re-checks
    CALLISTO_ALLOW_THRESHOLD_MIGRATION itself — the facade never bypasses it.
    """

    def __init__(
        self,
        hypothesis_manager,
        hypothesis_generator,
        backtest_engine,
        data_collector,
        vector_store,
        orchestrator=None,
        line_monitor=None,
    ):
        from tools.auto.loop_init import init_research_loop

        init_research_loop(
            self,
            hypothesis_manager=hypothesis_manager,
            hypothesis_generator=hypothesis_generator,
            backtest_engine=backtest_engine,
            data_collector=data_collector,
            vector_store=vector_store,
            orchestrator=orchestrator,
            line_monitor=line_monitor,
        )

    # ── Thin _phase_* delegation to tools.loop.phases_impl ─────────────────
    # Kept in this class body: earlier extraction slices pinned these wrappers
    # (and the live-execute env gate below) to tools/autonomous.py via AST.
    async def _phase_self_repair(self) -> None:
        return await phases_impl.phase_self_repair(self)

    async def _phase_self_diagnose(self) -> None:
        return await phases_impl.phase_self_diagnose(self)

    async def _phase_refresh_signals(self) -> None:
        return await phases_impl.phase_refresh_signals(self)

    async def _phase_collect_data(self) -> None:
        return await phases_impl.phase_collect_data(self)

    async def _phase_embed_data(self) -> None:
        return await phases_impl.phase_embed_data(self)

    async def _phase_injury_prop_hypotheses(self) -> None:
        return await phases_impl.phase_injury_prop_hypotheses(self)

    async def _phase_generate_hypotheses(self) -> None:
        return await phases_impl.phase_generate_hypotheses(self)

    async def _phase_validate(self) -> None:
        return await phases_impl.phase_validate(self)

    async def _phase_backtest(self) -> None:
        return await phases_impl.phase_backtest(self)

    @staticmethod
    def _check_temporal_overlap(model_config: dict) -> Optional[str]:
        """Check if training and backtest periods overlap. Returns error message or None."""
        from tools.auto.temporal import check_temporal_overlap

        return check_temporal_overlap(model_config)

    async def _phase_evaluate(self) -> None:
        return await phases_impl.phase_evaluate(self)

    async def _phase_narrative_edges(self) -> None:
        return await phases_impl.phase_narrative_edges(self)

    async def _phase_interpret_backtests(self) -> None:
        return await phases_impl.phase_interpret_backtests(self)

    async def _phase_review_live(self) -> None:
        return await phases_impl.phase_review_live(self)

    async def _phase_paper_trade(self) -> None:
        return await phases_impl.phase_paper_trade(self)

    async def _phase_claude_deep_work(self) -> None:
        return await phases_impl.phase_claude_deep_work(self)

    async def _phase_granger_analysis(self) -> None:
        return await phases_impl.phase_granger_analysis(self)

    async def _phase_regime_analysis(self) -> None:
        return await phases_impl.phase_regime_analysis(self)

    async def _phase_knowledge_compile(self) -> None:
        return await phases_impl.phase_knowledge_compile(self)

    async def _phase_knowledge_lint(self) -> None:
        return await phases_impl.phase_knowledge_lint(self)

    async def _phase_system_improvement(self) -> None:
        return await phases_impl.phase_system_improvement(self)

    async def _phase_system_watchdog(self) -> None:
        return await phases_impl.phase_system_watchdog(self)

    async def _phase_integrity_check(self) -> None:
        return await phases_impl.phase_integrity_check(self)

    async def _phase_live_execute(self) -> None:
        """Execute bets on live (proven) hypotheses.

        SAFETY GATE: this phase is OFF by default — it only runs when the
        operator explicitly arms it via ``CALLISTO_ALLOW_LIVE_EXECUTE=1``.
        That env var is the ONLY arming switch, and it is checked here,
        BEFORE any hypothesis listing, in the implementation too.
        """
        import os as _os

        if _os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1":
            logger.info("live_execute skipped (CALLISTO_ALLOW_LIVE_EXECUTE!=1)")
            return
        return await phases_impl.phase_live_execute(self)

    def get_status(self) -> dict:
        """Return research loop status."""
        from tools.auto.status import build_research_loop_status

        return build_research_loop_status(self)
