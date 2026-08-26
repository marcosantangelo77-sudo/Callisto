"""
tools.auto.facade — ResearchLoop facade mixins (slice 4).

Extracted from tools/autonomous.py. The facade class ``ResearchLoop``
there composes these mixins so existing callers keep working unchanged:

  - LifecycleMixin:      start/stop/pause/resume/set_local_only/_claude_ok,
                         event-bus subscribe/unsubscribe, startup maintenance
                         invocations (each gated routine re-checks its own
                         CALLISTO_ALLOW_THRESHOLD_MIGRATION flag internally).
  - ReactiveMixin:       event-bus reactive handlers (_on_game_completed,
                         _on_game_lineup_window).
  - FailureLedgerMixin:  phase-failure recording + per-cycle health checks.
  - RegimeMixin:         regime cache lookup.
  - CalibrationMixin:    R2 loop-quality seams (calibration trace recording,
                         iteration-state compaction).

NOTE: the thin ``_phase_*`` delegation wrappers, thin ``get_status`` /
``__init__`` / ``_check_temporal_overlap`` delegates, and the gated
``_phase_live_execute`` stay defined in the facade class body
(tools/autonomous.py) — earlier slices pinned them there by AST
(tests/test_auto_phases_extract.py, tests/test_loop_sequencer_slice.py,
tests/test_live_execute_gate.py). Slice 5 moved the *bodies* of
get_status / __init__ / temporal-overlap into tools.auto.status,
tools.auto.loop_init, and tools.auto.temporal.

SAFETY: nothing in this module arms or widens live betting;
CALLISTO_ALLOW_LIVE_EXECUTE remains the only live-execution arming switch,
checked first in tools/autonomous.py.

Mixins reference attributes provided by the composed ResearchLoop class;
they are declared here only for type checkers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from tools.loop.cycle_health import last_cycle_ok, last_cycle_phase_failures
from tools.loop.phases_impl import (
    RESEARCH_SPORTS,
    _regime_cache,
)

if TYPE_CHECKING:  # pragma: no cover — attribute surface of the composed class
    from tools.loop.phase_ledger import PhaseFailureLedger

logger = logging.getLogger("callisto.auto.facade")


class _ResearchLoopBase:
    """Attribute contract shared by the composed ResearchLoop class.

    Exists purely so static type checkers see the attributes the mixins use.
    Not a behaviour mixin — carries no methods used at runtime.
    """

    if TYPE_CHECKING:  # pragma: no cover
        _running: bool
        _task: Optional[asyncio.Task]
        _quant_scan_task: Optional[asyncio.Task]
        _paused: bool
        _local_only: bool
        _cycles: int
        _data_collections: int
        _hypotheses_generated: int
        _backtests_run: int
        _claude_escalations: int
        _promotions: int
        _rejections: int
        _last_regime_analysis: float
        _spinning_detected: bool
        _consecutive_no_progress: int
        _progress_window: list[dict]
        _reactive_collected: set[tuple[str, str]]
        _phase_failures_ledger: PhaseFailureLedger
        _calibration_trace: Any
        loop_phase_task_classes: dict
        data_collector: Any
        _work_queue: Any
        _downtime_tracker: Any


class LifecycleMixin(_ResearchLoopBase):
    """Start/stop/pause/resume and mode controls for ResearchLoop."""

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


class ReactiveMixin(_ResearchLoopBase):
    """Event-bus reactive handlers for the research loop."""

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


class FailureLedgerMixin(_ResearchLoopBase):
    """Phase-failure ledger recording and per-cycle health checks."""

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

    def _last_cycle_phase_failures(self) -> int:
        """Number of phase failures recorded during the current cycle."""
        return last_cycle_phase_failures(self._cycles, self._phase_failures_ledger)

    def _last_cycle_ok(self) -> bool:
        """True iff no phase failed during the current cycle."""
        return last_cycle_ok(self._cycles, self._phase_failures_ledger)


class RegimeMixin(_ResearchLoopBase):
    """Cached regime-analysis lookup."""

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


class CalibrationMixin(_ResearchLoopBase):
    """R2 loop-quality seams: calibration trace + state compaction."""

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
