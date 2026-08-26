"""
Autonomous reasoning loop — makes Callisto think without being asked.

Two loops run concurrently:
  1. AutonomousLoop — real-time edge detection (existing, unchanged)
  2. ResearchLoop — 24/7 hypothesis machine (NEW)

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
"""

import asyncio
import gc
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools import telegram
from tools.loop.cycle_health import last_cycle_ok, last_cycle_phase_failures
from tools.loop.phase_ledger import PhaseFailureLedger
from tools.loop import phases_impl
from tools.loop.sequencer import PERIODIC_PHASES, PHASES
from tools.backtest import _signal_confidence
from tools.edge_confidence import score_edge
from tools.market_psychology import (
    detect_number_shading,
    detect_trap_line,
    attention_arbitrage,
    predict_closing_line,
    full_market_psychology,
)
from tools.line_analysis import (
    detect_rlm,
    detect_steam,
    estimate_public_side,
    contrarian_value,
    optimal_bet_timing,
)
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)
from tools.injury_model import (
    full_injury_analysis,
    redistribute_usage,
    estimate_market_adjustment,
    player_impact,
)

logger = logging.getLogger("callisto.autonomous")

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
    get_regime_for_team,
)

# Map odds-API sport keys to injury_model sport codes
_SPORT_TO_MODEL = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "baseball_mlb": "MLB",
    "basketball_ncaab": "NBA",  # model tables work for college too
    "americanfootball_ncaaf": "NFL",
    "icehockey_nhl": "NHL",
}

# Only analyze edges above these thresholds — don't waste GPU on noise
# Lowered from 4%/3% — with 3-5 scraped books, legitimate edges start at 2%
MIN_IMPLIED_RANGE = 0.02       # 2% cross-book disagreement minimum
MIN_SOFT_EDGE_VS_SHARP = 0.02  # 2% vs sharp consensus minimum
MIN_CONFIDENCE_TO_ALERT = 0.40 # Alert at moderate confidence

# Max concurrent AGP sessions to avoid GPU overload
MAX_CONCURRENT_SESSIONS = 1

# Cooldown between full analysis cycles (seconds)
ANALYSIS_COOLDOWN = 120  # 2 min between analysis runs

# Don't re-analyze the same edge within this window
EDGE_DEDUP_WINDOW = 1800  # 30 minutes

# GATE POLICY bounds for automated threshold modification (_phase_interpret_backtests).
# An automated actor may raise a hypothesis's edge_threshold (tightening the gate)
# but never lower it; refusals are logged to hypothesis notes for human review.
MIN_EDGE_THRESHOLD_FLOOR = 0.005   # never below the creation default (hypothesis.py:488)
MAX_EDGE_THRESHOLD_CEILING = 0.10  # sanity clamp against LLM garbage (e.g. 25.0)



# ── Extracted slice: AutonomousLoop lives in tools/auto/loop.py ────────────
# The real-time edge-detection loop and its module constants moved to
# tools.auto; keep the module-level names importable here for existing
# callers (api.py etc).
from tools.auto import research as research_mod
from tools.auto.research import (  # noqa: F401 — re-exported mixin names
    CorrelationMixin,
    CycleLoopMixin,
    DeferredQueueMixin,
    MaintenanceMixin,
    ProgressMixin,
)
from tools.auto.loop import (  # noqa: E402,F401
    ANALYSIS_COOLDOWN,
    EDGE_DEDUP_WINDOW,
    MIN_CONFIDENCE_TO_ALERT,
    MIN_IMPLIED_RANGE,
    MIN_SOFT_EDGE_VS_SHARP,
    _SPORT_TO_MODEL,
    AutonomousLoop,
)




class ResearchLoop(
    research_mod.MaintenanceMixin,
    research_mod.DeferredQueueMixin,
    research_mod.CycleLoopMixin,
    research_mod.CorrelationMixin,
    research_mod.ProgressMixin,
):
    """
    24/7 autonomous research engine — Claude Code is the primary reasoning engine.

    Runs independently of AutonomousLoop. While AutonomousLoop handles
    real-time edge detection and alerting, ResearchLoop handles the
    slow, deep work: collecting data, discovering patterns, generating
    and testing hypotheses, interpreting results, and self-improving.

    The large method groups now live in tools/auto/research.py as mixins:

      - MaintenanceMixin: one-time startup migrations/sweeps
        (_backfill_temporal_metadata, _migrate_edge_thresholds behind the
        CALLISTO_ALLOW_THRESHOLD_MIGRATION gate, rejection requeues,
        anti-predictive / low-signal-rate rejections)
      - DeferredQueueMixin: deferred work queue drain + drained-item processing
      - CycleLoopMixin: the main research cycle (`_loop`) and quant scan loop
      - CorrelationMixin: pairwise hypothesis correlation matrix helpers
      - ProgressMixin: Ralph-loop progress tracking, spinning detection,
        data-driven spinning diagnosis

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

    This class keeps __init__/start/stop and lifecycle controls, the reactive
    event handlers, phase-failure recording, the thin _phase_* delegation to
    tools.loop.phases_impl (including the CALLISTO_ALLOW_LIVE_EXECUTE gate),
    regime lookup, calibration seams and status reporting.

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
        self.hypothesis_manager = hypothesis_manager
        self.hypothesis_generator = hypothesis_generator
        self.backtest_engine = backtest_engine
        self.data_collector = data_collector
        self.vector_store = vector_store
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Timestamps for cadence control
        self._last_data_collect = 0.0
        self._last_hypothesis_gen = 0.0
        self._last_claude_call = 0.0

        # Bulk backfill tracking — one-time 30-day seed when data is thin
        self._bulk_backfill_done = False

        # Counters
        self._cycles = 0
        self._data_collections = 0
        self._hypotheses_generated = 0
        self._backtests_run = 0
        self._claude_escalations = 0
        self._promotions = 0
        self._rejections = 0

        # Phase-failure ledger — every _phase_* exception/timeout is recorded
        # here so a "healthy-looking" loop can't silently swallow failures.
        # Capped at 50 entries; oldest dropped when full.
        self._phase_failures_ledger = PhaseFailureLedger()

        # Self-diagnostics — track already-escalated issues to avoid spam
        # Capped at 500 entries; oldest keys evicted when full.
        self._diagnostic_issues: set[str] = set()
        self._DIAGNOSTIC_ISSUES_MAX = 500

        # ── Progress tracking (Ralph loop: detect spinning) ──
        self._progress_window: list[dict] = []  # last N cycle snapshots
        PROGRESS_WINDOW_SIZE = 10  # look at last 10 cycles
        self._spinning_detected = False
        self._last_progress_check = 0
        self._consecutive_no_progress = 0
        # R2: the spinning diagnosis must fire ONCE per spin episode, not on
        # every subsequent stagnant check. Reset when progress resumes.
        self._diagnosis_fired_this_episode = False

        # ── R2 loop-quality state ──
        # Calibration trace: per-iteration confidence/evidence ledger, the
        # record shape R1's retrodiction harness scores against outcomes.
        from tools.loop_quality import LoopCalibrationTrace
        self._calibration_trace = LoopCalibrationTrace(subject="research_loop")
        # Per-phase task-class allocation for the ProviderRouter: framing
        # (first) and adversarial review (last) get capability tiers, the
        # middle grind routes to extraction-class endpoints.
        from tools.loop_quality import LOOP_PHASE_TASK_CLASSES
        self.loop_phase_task_classes = dict(LOOP_PHASE_TASK_CLASSES)

        # Regime analysis — uses module-level _regime_cache (shared with AutonomousLoop)
        # Refreshed every REGIME_ANALYSIS_INTERVAL cycles
        self._last_regime_analysis = 0

        # Dedup reactive game completion handlers — prevents 14×14 ESPN calls
        # when 14 games complete on the same date. Cleared each research cycle.
        self._reactive_collected: set[tuple[str, str]] = set()

        # Deferred work queue + downtime tracker (never-idle loop)
        from tools.work_queue import get_work_queue, get_downtime_tracker
        self._work_queue = get_work_queue()
        self._downtime_tracker = get_downtime_tracker()
        self._was_claude_available = True  # track transitions

        # Mode control
        self._paused = False
        self._local_only = os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes")

    async def start(self) -> None:
        """Start the research loop."""
        if self._running:
            return
        self._running = True
        logger.info(f"Research loop starting — all sports equal: {RESEARCH_SPORTS}")
        # Subscribe to event bus for reactive data collection
        try:
            from tools.event_bus import get_event_bus, EVENT_GAME_COMPLETED, EVENT_GAME_LINEUP_WINDOW
            bus = get_event_bus()
            bus.subscribe(EVENT_GAME_COMPLETED, self._on_game_completed)
            bus.subscribe(EVENT_GAME_LINEUP_WINDOW, self._on_game_lineup_window)
            logger.info("Research loop subscribed to game_completed and lineup_window events")
        except Exception as e:
            logger.debug(f"Event bus subscription failed (non-critical): {e}")
        # One-time backfill of temporal metadata on legacy hypotheses
        await self._backfill_temporal_metadata()
        # One-time: requeue hypotheses falsely rejected by high-threshold bug
        await self._requeue_threshold_rejections()
        # One-time: requeue player prop hypotheses now that prop backtesting is available
        await self._requeue_prop_rejections()
        # Edge thresholds: run AFTER requeues so newly-requeued hypotheses get
        # their thresholds lowered too (previously ran before requeues, missing them)
        await self._migrate_edge_thresholds()
        # Retroactively update signal_generated on existing backtest events
        # to match lowered thresholds — unblocks stalled promotions
        await self._retroactive_signal_update()
        # Requeue hypotheses falsely rejected for '0 signals' due to stale stats
        await self._requeue_stale_signal_rejections()
        # Reject any anti-predictive hypotheses still stuck in active states
        await self._reject_anti_predictive()
        await self._reject_low_signal_rate()
        self._task = asyncio.create_task(self._loop())
        # Quant scanner runs on a separate, faster cadence (~60s). It's the
        # live pricing engine — consumes multi-book odds, emits ranked edges
        # into live_edge_surface. Decoupled from the 5-minute research loop
        # so recommendations refresh at market-appropriate speed.
        self._quant_scan_task = asyncio.create_task(self._quant_scan_loop())
        logger.info("Research loop started — autonomous hypothesis machine online")
        logger.info("Quant scanner started — live edge surface refreshing every 60s")

    async def _on_game_completed(self, event_data: dict) -> None:
        """Reactive handler: immediately collect data when a game completes."""
        sport = event_data.get("sport", "")
        game_date = event_data.get("game_date", "")
        if not sport or not game_date:
            return

        # Dedup: collect_play_by_play fetches ALL games for a date, so
        # calling it once per (sport, date) is sufficient. Without this,
        # 14 completed MLB games fire 14 handlers × 14 ESPN calls each = 196.
        key = (sport, game_date)
        if key in self._reactive_collected:
            return
        self._reactive_collected.add(key)

        try:
            date_str = game_date.replace("-", "")
            await self.data_collector.collect_box_scores(sport, date_str)
            await self.data_collector.collect_play_by_play(sport, date_str)
            logger.info(f"Reactive collection: {sport} game completed on {game_date}")

            # Update learned correlations from this game's data
            try:
                from tools.correlation import get_learned_store
                lcs = get_learned_store()
                if lcs is not None and self.data_collector._db is not None:
                    await lcs.update_from_game_data(
                        self.data_collector._db, sport, game_date,
                    )
            except Exception as e:
                logger.debug(f"Reactive correlation update failed: {e}")

            # Compute per-game KL divergence (information flow measurement)
            try:
                from tools.kl_divergence import compute_game_kl, store_kl_metrics
                event_id = event_data.get("event_id", "")
                if event_id:
                    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
                    kl_result = await compute_game_kl(db_path, event_id, sport)
                    if kl_result:
                        await store_kl_metrics(db_path, [kl_result])
            except Exception as e:
                logger.debug(f"KL divergence computation failed: {e}")
        except Exception as e:
            logger.debug(f"Reactive collection failed for {sport} {game_date}: {e}")

    async def _on_game_lineup_window(self, event_data: dict) -> None:
        """Reactive handler: re-scan edges when lineup cards may be posted (T-180min)."""
        sport = event_data.get("sport", "")
        event_id = event_data.get("event_id", "")
        home_team = event_data.get("home_team", "")
        away_team = event_data.get("away_team", "")
        commence_time = event_data.get("commence_time", "")

        if not sport or not event_id:
            return

        matchup = f"{away_team}@{home_team}" if away_team and home_team else event_id
        logger.info(
            f"Lineup window trigger: {matchup} ({sport}) — "
            f"re-scanning edges for lineup confirmation"
        )

        try:
            query = (
                f"LINEUP_WINDOW_RESCAN for {matchup}: Re-evaluate edges now that "
                f"lineup cards may be posted. Check market hold normalization, spread "
                f"compression, and whether pre-lineup phantom edges have resolved. "
                f"event_id={event_id} sport={sport} commence_time={commence_time}"
            )
            await self._work_queue.enqueue("lineup_rescan", query, priority=1)
            logger.info(f"Lineup rescan task enqueued for {matchup}")
        except Exception as e:
            logger.warning(f"Failed to enqueue lineup rescan for {matchup}: {e}")

    async def stop(self) -> None:
        """Stop the research loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Cancel the quant scanner alongside the main loop.
        qt = getattr(self, "_quant_scan_task", None)
        if qt is not None and not qt.done():
            qt.cancel()
            try:
                await qt
            except (asyncio.CancelledError, Exception):
                pass
        # Unsubscribe from event bus to prevent leaked references on restart
        try:
            from tools.event_bus import get_event_bus, EVENT_GAME_COMPLETED, EVENT_GAME_LINEUP_WINDOW
            bus = get_event_bus()
            bus.unsubscribe(EVENT_GAME_COMPLETED, self._on_game_completed)
            bus.unsubscribe(EVENT_GAME_LINEUP_WINDOW, self._on_game_lineup_window)
        except Exception:
            pass
        # Record final downtime stats
        await self._downtime_tracker.record_to_hermes()
        logger.info(
            f"Research loop stopped — {self._cycles} cycles, "
            f"{self._hypotheses_generated} hypotheses generated, "
            f"{self._backtests_run} backtests run, "
            f"{self._promotions} promoted, {self._rejections} rejected"
        )

    async def pause(self) -> dict:
        """Pause the research loop (keeps running but skips all phases)."""
        self._paused = True
        logger.info("Research loop PAUSED")
        return {"status": "paused", "cycles_completed": self._cycles}

    async def resume(self) -> dict:
        """Resume the research loop."""
        self._paused = False
        logger.info("Research loop RESUMED")
        return {"status": "running", "cycles_completed": self._cycles}

    def set_local_only(self, enabled: bool) -> dict:
        """Toggle local-only mode (no Claude Code calls)."""
        self._local_only = enabled
        mode = "local_only" if enabled else "full"
        logger.info(f"Research loop mode: {mode}")
        return {"mode": mode, "local_only": enabled}

    def _claude_ok(self) -> bool:
        """Check if Claude Code calls are allowed."""
        if self._local_only:
            return False
        from tools.claude_code import is_available as claude_available
        return claude_available()

    def _record_phase_failure(
        self,
        phase: str,
        kind: str,
        exc: BaseException | None = None,
    ) -> None:
        """Record a phase failure (exception or timeout) in the ledger.

        Recording is non-fatal: the loop continues after a phase failure, but
        the failure becomes visible via get_status()["phase_failures"].
        """
        try:
            self._phase_failures_ledger.record(
                cycle=self._cycles, phase=phase, kind=kind, exc=exc
            )
        except Exception:
            logger.debug("Failed to record phase failure", exc_info=True)

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
        if isinstance(model_config, str):
            try:
                model_config = json.loads(model_config)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(model_config, dict):
            return None

        training_end = model_config.get("training_period_end")
        backtest_start = model_config.get("backtest_period_start")

        if not training_end or not backtest_start:
            return None  # Can't check without both dates

        try:
            te = datetime.strptime(str(training_end), "%Y-%m-%d").date()
            bs = datetime.strptime(str(backtest_start), "%Y-%m-%d").date()
            if bs <= te:
                return (
                    f"TEMPORAL OVERLAP: backtest starts {bs} but training ends {te}. "
                    f"Backtest results are contaminated by training data."
                )
        except ValueError:
            pass

        return None

    async def _phase_evaluate(self) -> None:
        return await phases_impl.phase_evaluate(self)

    async def _phase_narrative_edges(self) -> None:
        return await phases_impl.phase_narrative_edges(self)

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

    def get_regime_for_team(self, sport: str, team_name: str) -> Optional[dict]:
        """Look up cached regime analysis for a team.

        Args:
            sport: Sport key (e.g., "basketball_nba")
            team_name: Team name as it appears in box scores

        Returns:
            Full regime analysis dict or None if not cached.
        """
        cache_key = f"{sport}:{team_name}"
        result = _regime_cache.get(cache_key)
        if result:
            return result
        # Try partial match — team names can vary (e.g., "Boston Celtics" vs "Celtics")
        for key, val in _regime_cache.items():
            if key.startswith(sport + ":") and team_name.lower() in key.lower():
                return val
        return None

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

    def record_iteration_outcome(
        self,
        confidence: float,
        evidence_counts: dict[str, int],
        position: Optional[int] = None,
        total: Optional[int] = None,
        notes: str = "",
    ) -> dict:
        """R2 seam: record one iteration's confidence + evidence into the
        calibration trace, and return the task_class the ProviderRouter
        should serve this iteration's phase with.

        ``position``/``total`` map the iteration onto a loop phase
        (framing → grind → adversarial_review); omitting them records under
        the extraction (grind) class. Callers that route model calls through
        ProviderRouter pass the returned task_class to
        ``router.complete(...)``; callers that only log ignore it.
        """
        from tools.loop_quality import task_class_for_iteration

        tc = None
        if position is not None and total is not None:
            tc = task_class_for_iteration(position, total)
        rec = self._calibration_trace.add_iteration(
            confidence, evidence_counts, task_class=tc, notes=notes,
        )
        logger.info(
            "Calibration: iter %d conf=%.3f evidence=%d "
            "(+conf/-dis/neutral=%d/%d/%d) task_class=%s",
            rec.iteration, rec.confidence, rec.evidence_total,
            rec.confirming, rec.disconfirming, rec.neutral, tc or "-",
        )
        return {"record": rec.to_dict(), "task_class": tc}

    def compact_iteration_state(self, items: list[dict], **budgets) -> tuple[list[dict], list[dict]]:
        """R2 seam: explicit iteration-boundary compaction.

        Contradicting items survive verbatim regardless of budget; supporting
        and neutral items are capped best-tier-first. Dropped items carry a
        reason. See tools.loop_quality.compact_state.
        """
        from tools.loop_quality import compact_state
        return compact_state(items, **budgets)

    def _last_cycle_phase_failures(self) -> int:
        """Number of phase failures recorded during the current cycle."""
        return last_cycle_phase_failures(self._cycles, self._phase_failures_ledger)

    def _last_cycle_ok(self) -> bool:
        """True iff no phase failed during the current cycle."""
        return last_cycle_ok(self._cycles, self._phase_failures_ledger)

    def get_status(self) -> dict:
        """Return research loop status."""
        from tools.claude_code import get_usage_stats as claude_stats
        from tools.pipeline_integrity import get_checker

        # Include pipeline integrity info
        integrity_report = get_checker().get_latest_report()

        # Include work queue status (async call — best-effort)
        work_queue_status = {}
        try:
            import asyncio
            work_queue_status = asyncio.get_event_loop().run_until_complete(
                self._work_queue.get_status()
            ) if not asyncio.get_event_loop().is_running() else {}
        except Exception:
            pass

        return {
            "running": self._running,
            "paused": self._paused,
            "local_only": self._local_only,
            "mode": "paused" if self._paused else ("local_only" if self._local_only else "full"),
            "cycles_completed": self._cycles,
            "data_collections": self._data_collections,
            "hypotheses_generated": self._hypotheses_generated,
            "backtests_run": self._backtests_run,
            "claude_escalations": self._claude_escalations,
            "promotions": self._promotions,
            "rejections": self._rejections,
            # Phase-failure ledger: last 10 failures + total count so a
            # "healthy-looking" loop can't hide swallowed phase errors.
            "phase_failures": self._phase_failures_ledger.latest(10),
            "phase_failure_count": self._phase_failures_ledger.count,
            # Per-cycle health: False when any phase failed during the most
            # recent cycle (failures are non-fatal, but the loop is NOT ok).
            "last_cycle_ok": self._last_cycle_ok(),
            "last_cycle_phase_failures": self._last_cycle_phase_failures(),
            # R2: loop-quality telemetry — calibration trace + per-phase
            # task-class map, consumed by R1's retrodiction harness.
            "calibration": self._calibration_trace.summary(),
            "calibration_records": self._calibration_trace.to_records()[-20:],
            "phase_task_classes": dict(self.loop_phase_task_classes),
            "research_sports": RESEARCH_SPORTS,
            "claude_code": claude_stats(),
            "pipeline_integrity": integrity_report,
            "work_queue": work_queue_status,
            "claude_downtime": self._downtime_tracker.get_status(),
            "progress": {
                "spinning_detected": self._spinning_detected,
                "consecutive_no_progress": self._consecutive_no_progress,
                "window": self._progress_window[-3:] if self._progress_window else [],
            },
            "regime_analysis": {
                "teams_cached": len(_regime_cache),
                "teams_with_signals": sum(
                    1 for v in _regime_cache.values()
                    if v.get("has_edge_signal")
                ),
                "last_run": self._last_regime_analysis,
                "interval_cycles": REGIME_ANALYSIS_INTERVAL,
            },
            "intervals": {
                "research_cycle_seconds": RESEARCH_CYCLE_INTERVAL,
                "data_collection_seconds": DATA_COLLECTION_INTERVAL,
                "hypothesis_gen_seconds": HYPOTHESIS_GEN_INTERVAL,
                "claude_cooldown_seconds": CLAUDE_ESCALATION_COOLDOWN,
            },
        }
