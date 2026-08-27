"""
ResearchLoop helper slices — extracted from tools/autonomous.py.

This module hosts the large method groups of ResearchLoop as mixins; the
class in tools.autonomous composes them so behaviour and the public import
surface (`from tools.autonomous import ResearchLoop`) are unchanged:

  * MaintenanceMixin      — one-time startup migrations / sweeps
                            (temporal-metadata backfill, edge-threshold
                            migration under CALLISTO_ALLOW_THRESHOLD_MIGRATION,
                            rejection requeues, anti-predictive and
                            low-signal-rate rejections)
  * DeferredQueueMixin    — re-exported from tools.auto.deferred
                            (never-idle drain + GATE POLICY on interpret)
  * CycleLoopMixin        — re-exported from tools.auto.cycle (main
                            research cycle `_loop` + quant scan)
  * CorrelationMixin      — pairwise hypothesis correlation matrix + signal
                            count map for the portfolio/Kelly layer
  * ProgressMixin         — Ralph-loop progress tracking, spinning detection,
                            data-driven spinning diagnosis

GATE POLICY notes preserved verbatim from the original inline code:
threshold migration and historical-evidence rewrites stay behind the
CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 operator opt-in.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from tools.auto.cycle import CycleLoopMixin  # re-exported
from tools.auto.deferred import DeferredQueueMixin  # re-exported
from tools.loop.phases_impl import (  # noqa: F401 — re-exported cadence constants
    BACKTEST_BATCH_SIZE,
    BACKTEST_GAP_DAYS,
    CLAUDE_ESCALATION_COOLDOWN,
    DATA_COLLECTION_INTERVAL,
    DEFAULT_TRAINING_WINDOW_DAYS,
    HYPOTHESIS_GEN_INTERVAL,
    MAX_EDGE_THRESHOLD_CEILING,
    MIN_EDGE_THRESHOLD_FLOOR,
)

logger = logging.getLogger("callisto.auto.research")



class MaintenanceMixin:

    async def _backfill_temporal_metadata(self) -> None:
        """Backfill training_period_end on legacy hypotheses that lack temporal metadata.

        Sets reasonable defaults so the backtest engine can enforce temporal isolation
        on the 231 hypotheses created before the temporal split system existed.
        """
        db = self.hypothesis_manager._db
        if db is None:
            logger.warning("Cannot backfill temporal metadata — hypothesis DB not initialized")
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE model_config NOT LIKE '%training_period_end%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Temporal metadata backfill: no legacy hypotheses need updating")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            config["training_period_end"] = "2026-02-22"
            config["training_period_start"] = "2023-01-01"
            config["temporal_split_gap_days"] = 7

            await db.execute(
                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Temporal metadata backfill complete: updated {count} legacy hypotheses "
            f"(training_period_end=2026-02-22, training_period_start=2023-01-01, gap=7d)"
        )

    async def _migrate_edge_thresholds(self) -> None:
        """Lower edge_thresholds that exceed real market edge range.

        GATE POLICY: this routine writes the OPERATIVE edge_threshold column on
        draft/backtesting hypotheses — a gate change made by a maintenance
        routine. It now requires explicit operator opt-in via
        CALLISTO_ALLOW_THRESHOLD_MIGRATION=1. Without the flag it logs what it
        WOULD have done and changes nothing. The migration was also re-running
        on EVERY loop start (not once); under the flag it remains idempotent,
        but each application is now a conscious operator act, visible in logs.

        Original rationale preserved: real market edges in our data top out at
        ~0.83% with most at 0.3-0.8%. Four passes end at 0.3%.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        if not os.getenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION"):
            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses "
                "WHERE edge_threshold > 0.003 AND status IN ('draft', 'backtesting')"
            )
            row = await cursor.fetchone()
            would = row[0] if row else 0
            if would:
                logger.warning(
                    f"Gate policy: edge-threshold migration SKIPPED (would lower "
                    f"{would} hypotheses' operative gates). Set "
                    f"CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 to authorize."
                )
            return

        # Pass 1: legacy — >= 2.5% to 1.5%
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_high = row[0] if row else 0

        if count_high > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.015 "
                "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 1: lowered {count_high} hypotheses "
                f"from ≥2.5% to 1.5%"
            )

        # Pass 2: lower 1.5-2.5% to 1.0%
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.015 AND edge_threshold < 0.025 "
            "AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_mid = row[0] if row else 0

        if count_mid > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.01 "
                "WHERE edge_threshold >= 0.015 AND edge_threshold < 0.025 "
                "AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 2: lowered {count_mid} hypotheses "
                f"from 1.5-2.5% to 1.0%"
            )

        # Pass 3: lower >= 0.8% to 0.5% — max observed edge is 0.83%,
        # so 1.0% and 1.2% thresholds are unreachable
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.008 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_low = row[0] if row else 0

        if count_low > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.005 "
                "WHERE edge_threshold >= 0.008 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 3: lowered {count_low} hypotheses "
                f"from ≥0.8% to 0.5% (max observed edge is 0.83%)"
            )

        # Pass 4: final sweep — lower any remaining threshold > 0.003 to 0.003
        # The 0.005 threshold from pass 3 still filters out edges in the 0.3-0.5%
        # range which are common and profitable at scale. 0.3% is the minimum
        # detectable edge that is consistently above noise.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold > 0.003 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_final = row[0] if row else 0

        if count_final > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.003 "
                "WHERE edge_threshold > 0.003 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 4: lowered {count_final} hypotheses "
                f"to 0.3% (final sweep — captures 0.3-0.5% edges)"
            )

        total = count_high + count_mid + count_low + count_final
        if total > 0:
            await db.commit()
            logger.info(
                f"Edge threshold migration complete: {total} hypotheses updated "
                f"(pass1={count_high}, pass2={count_mid}, pass3={count_low})"
            )
        else:
            logger.info("Edge threshold migration: no hypotheses need lowering")

    async def _retroactive_signal_update(self) -> None:
        """Retroactively update signal_generated on backtest events after threshold migration.

        GATE POLICY: this REWRITES HISTORICAL EVIDENCE (signal_generated flags
        on already-resolved backtest events) to match a lowered gate — the
        evidence base moves to fit the threshold instead of the threshold being
        tested against the evidence. Requires the same operator opt-in as the
        migration that motivates it: CALLISTO_ALLOW_THRESHOLD_MIGRATION=1.
        Without the flag: no-op.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        if not os.getenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION"):
            return

        # For each backtesting hypothesis, update signal_generated based on
        # current edge_threshold (which may have been lowered by migration)
        cursor = await db.execute(
            "SELECT hypothesis_id, edge_threshold FROM hypotheses "
            "WHERE status = 'backtesting'"
        )
        rows = await cursor.fetchall()

        total_updated = 0
        total_new_signals = 0
        for hypothesis_id, threshold in rows:
            if threshold is None:
                continue
            # Update signal_generated for events where edge >= threshold
            update_cursor = await db.execute(
                "UPDATE backtest_events "
                "SET signal_generated = CASE WHEN edge >= ? THEN 1 ELSE 0 END "
                "WHERE hypothesis_id = ? AND signal_generated = 0 AND edge IS NOT NULL "
                "AND edge >= ?",
                (threshold, hypothesis_id, threshold),
            )
            if update_cursor.rowcount > 0:
                total_updated += update_cursor.rowcount
                total_new_signals += update_cursor.rowcount

        if total_updated > 0:
            await db.commit()
            logger.info(
                f"Retroactive signal update: upgraded {total_updated} backtest events "
                f"to signals across {len(rows)} hypotheses (edge >= lowered threshold)"
            )
        else:
            logger.info("Retroactive signal update: no events needed updating")

    async def _requeue_threshold_rejections(self) -> None:
        """Requeue hypotheses that were rejected due to the high-threshold bug.

        These hypotheses were rejected with 'no_edge_after_backtest' because their
        edge_threshold was ≥3% while real market edges cap at ~2.5%. With thresholds
        now lowered, they deserve a second chance.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # GATE POLICY: un-rejecting reverses a rejection decision (rejected ->
        # backtesting) AND writes a lowered operative gate. Operator opt-in
        # required, same flag as the threshold migration.
        if not os.getenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION"):
            logger.warning(
                "Gate policy: _requeue_threshold_rejections SKIPPED (un-rejects "
                "hypotheses and lowers gates). Set CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 "
                "to authorize."
            )
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE status = 'rejected' "
            "AND promoted_by LIKE '%no_edge_after_backtest%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Threshold rejection requeue: no hypotheses to requeue")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            # Reset eval cycles so they get a fresh evaluation
            config["evaluate_cycles"] = 0
            config["requeued_from_threshold_bug"] = True

            await db.execute(
                "UPDATE hypotheses SET status = 'backtesting', "
                "edge_threshold = 0.015, model_config = ? "
                "WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Threshold rejection requeue: moved {count} hypotheses from rejected → backtesting "
            f"(were victims of edge_threshold ≥ 3% bug, now set to 1.5%)"
        )

    async def _requeue_prop_rejections(self) -> None:
        """Requeue player prop hypotheses rejected before prop backtesting was available.

        These were rejected with 'auto:untestable_no_prop_backtest' because
        historical_odds_cache lacked prop data. Now prop_snapshots is wired
        into BacktestEngine, so they can be properly tested.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # GATE POLICY: un-rejecting reverses a rejection decision and writes
        # edge_threshold = 0.003. Operator opt-in required.
        if not os.getenv("CALLISTO_ALLOW_THRESHOLD_MIGRATION"):
            return

        cursor = await db.execute(
            "SELECT hypothesis_id FROM hypotheses "
            "WHERE status = 'rejected' "
            "AND promoted_by LIKE '%untestable_no_prop_backtest%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            return

        count = 0
        for (hypothesis_id,) in rows:
            await db.execute(
                "UPDATE hypotheses SET status = 'draft', edge_threshold = 0.003 "
                "WHERE hypothesis_id = ?",
                (hypothesis_id,),
            )
            count += 1

        if count > 0:
            await db.commit()
            logger.info(
                f"Prop rejection requeue: moved {count} player prop hypotheses "
                f"from rejected → draft (prop_snapshots backtesting now available)"
            )

    async def _requeue_stale_signal_rejections(self) -> None:
        """Requeue hypotheses rejected with '0 signals' that actually have signals.

        Race condition: retroactive signal update runs after backtest but before
        evaluate. The evaluate phase sees stale signals_generated=0 in backtest_runs
        and rejects, even though backtest_events now has signals. Fix: requeue these
        to backtesting so they get a fresh evaluation with correct stats.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # Find hypotheses rejected for "0 signals" that actually have signals in events.
        # Two-step approach to avoid slow correlated subquery on 3000+ rejected hyps.
        cursor = await db.execute(
            "SELECT hypothesis_id, name, promoted_by FROM hypotheses "
            "WHERE status = 'rejected' AND promoted_by LIKE '%0 signals%'"
        )
        candidates = await cursor.fetchall()
        rows = []
        for hid, name, reason in candidates:
            sig_row = await (await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE hypothesis_id = ? AND signal_generated = 1",
                (hid,),
            )).fetchone()
            actual_signals = sig_row[0] if sig_row else 0
            if actual_signals > 0:
                rows.append((hid, name, actual_signals))

        if not rows:
            logger.info(f"Stale signal requeue: checked {len(candidates)} candidates, none had actual signals")
            return

        count = 0
        for hid, name, actual_signals in rows:
            await self.hypothesis_manager.update_status(
                hid, "backtesting",
                f"auto:requeued_stale_signal_rejection — rejected with '0 signals' "
                f"but backtest_events has {actual_signals} signals. Race condition fix."
            )
            count += 1
            logger.info(
                f"Requeued {hid} ({name}): rejected for '0 signals' but has "
                f"{actual_signals} actual signals in backtest_events"
            )

        if count:
            await db.commit()
            logger.info(f"Stale signal rejection requeue: restored {count} hypotheses")

    async def _reject_anti_predictive(self) -> None:
        """Reject hypotheses with strongly negative IC on sufficient sample size.

        Uses the same thresholds as hypothesis.py auto-rejection:
        - IC < -0.15 with 15+ signals (standard)
        - IC < -0.25 with 10+ signals (strong anti-prediction)
        Previous threshold of IC < -0.10 with NO sample minimum was rejecting
        hypotheses based on noise (e.g. IC=-0.13 on 11 paper trades is meaningless).
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT h.hypothesis_id, h.name, h.status, "
            "hs.information_coefficient, hs.brier_score, hs.signals_n "
            "FROM hypotheses h "
            "JOIN hypothesis_stats hs ON h.hypothesis_id = hs.hypothesis_id "
            "WHERE h.status IN ('backtesting', 'paper_trading') "
            "AND hs.information_coefficient < -0.15"
        )
        rows = await cursor.fetchall()

        count = 0
        for hid, name, status, ic, brier, signals_n in rows:
            signals_n = signals_n or 0
            # Require minimum sample size for IC to be meaningful
            if ic < -0.25 and signals_n >= 10:
                pass  # strong anti-prediction — reject
            elif ic < -0.15 and signals_n >= 15:
                pass  # standard anti-prediction — reject
            else:
                continue  # insufficient evidence
            try:
                brier_str = f"{brier:.3f}" if brier is not None else "N/A"
                ic_str = f"{ic:.3f}" if ic is not None else "N/A"
                await self.hypothesis_manager.update_status(
                    hid, "rejected",
                    f"auto:anti_predictive — IC={ic_str}, brier={brier_str}, n={signals_n}. "
                    f"Strongly anti-predictive, worse than random."
                )
                count += 1
                logger.info(
                    f"Rejected anti-predictive {hid} ({name}): IC={ic_str}, brier={brier_str}, n={signals_n}"
                )
            except Exception as e:
                logger.warning(f"Failed to reject anti-predictive {hid} ({name}): {e}")

        if count:
            logger.info(f"Anti-predictive sweep: rejected {count} hypotheses")

    async def _reject_low_signal_rate(self) -> None:
        """Reject backtesting hypotheses with 100+ events but <2% signal rate.

        These hypotheses target edge conditions that don't exist at detectable
        frequency. All p-value and IC rejection tiers gate on signal count,
        so near-zero-signal hypotheses slip through indefinitely.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT h.hypothesis_id, h.name, hs.total_n, hs.signals_n "
            "FROM hypotheses h "
            "JOIN hypothesis_stats hs ON h.hypothesis_id = hs.hypothesis_id "
            "WHERE h.status = 'backtesting' "
            "AND hs.total_n >= 100 "
            "AND (CAST(hs.signals_n AS REAL) / hs.total_n) < 0.02"
        )
        rows = await cursor.fetchall()

        count = 0
        for hid, name, total_n, signals_n in rows:
            signals_n = signals_n or 0
            rate = signals_n / total_n if total_n > 0 else 0
            try:
                await self.hypothesis_manager.update_status(
                    hid, "rejected",
                    f"auto:low_signal_rate — {signals_n}/{total_n} events = "
                    f"{rate:.1%} signal rate < 2%. Edge condition too rare."
                )
                count += 1
                logger.info(
                    f"Rejected low-signal-rate {hid} ({name}): "
                    f"{signals_n}/{total_n} = {rate:.1%}"
                )
            except Exception as e:
                logger.warning(f"Failed to reject low-signal-rate {hid} ({name}): {e}")

        if count:
            logger.info(f"Low-signal-rate sweep: rejected {count} hypotheses")



class CorrelationMixin:

    async def _build_correlation_matrix(
        self, hypothesis_ids: list[str], lookback_days: int = 30
    ) -> dict[tuple[str, str], float]:
        """Build a pairwise correlation matrix from ``backtest_events`` history.

        For each pair (A, B), compute
            corr(A, B) = |events where A AND B signalled on same event_id| /
                         |events where A OR B signalled|
        over the last ``lookback_days``. This is the Jaccard co-firing rate —
        a conservative proxy for bet correlation when both sit on the same
        event. Perfect co-firing = 1.0, no overlap = 0.0.

        Cached on ``self._corr_matrix_cache`` with TTL
        ``CALLISTO_CORR_TTL_SECONDS`` (default 4h). The cache is keyed by
        the sorted tuple of hypothesis_ids so demotion/promotion invalidates
        it implicitly.
        """
        cache_ttl = int(os.getenv("CALLISTO_CORR_TTL_SECONDS", "14400"))
        cache_key = tuple(sorted(hypothesis_ids))
        cache = getattr(self, "_corr_matrix_cache", {})
        now_ts = time.time()
        if cache_key in cache:
            cached_at, matrix = cache[cache_key]
            if now_ts - cached_at < cache_ttl:
                return matrix

        db = self.data_collector._db if self.data_collector else None
        if not db or not hypothesis_ids:
            return {}

        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

        # Pull (hypothesis_id, event_id) tuples where signal_generated=1 in window.
        try:
            placeholders = ",".join(["?"] * len(hypothesis_ids))
            cursor = await db.execute(
                f"SELECT hypothesis_id, event_id FROM backtest_events "
                f"WHERE signal_generated = 1 AND hypothesis_id IN ({placeholders}) "
                f"AND created_at >= ?",
                (*hypothesis_ids, since),
            )
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"Correlation matrix: query failed: {e}")
            return {}

        # Build per-hyp event_id sets.
        fired: dict[str, set[str]] = {}
        for hid, eid in rows:
            if not eid:
                continue
            fired.setdefault(hid, set()).add(eid)

        matrix: dict[tuple[str, str], float] = {}
        ids = sorted(hypothesis_ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                sa = fired.get(a, set())
                sb = fired.get(b, set())
                union = len(sa | sb)
                if union == 0:
                    corr = 0.0
                else:
                    corr = len(sa & sb) / union
                matrix[(a, b)] = round(corr, 4)

        # Store with timestamp; cap cache growth.
        cache[cache_key] = (now_ts, matrix)
        if len(cache) > 32:
            oldest = min(cache, key=lambda k: cache[k][0])
            cache.pop(oldest, None)
        self._corr_matrix_cache = cache
        return matrix

    async def _hyp_signals_n_map(self, hypothesis_ids: list[str]) -> dict[str, int]:
        """Return {hypothesis_id: most_recent_signals_n} from hypothesis_stats."""
        db = self.data_collector._db if self.data_collector else None
        if not db or not hypothesis_ids:
            return {}
        placeholders = ",".join(["?"] * len(hypothesis_ids))
        try:
            cursor = await db.execute(
                f"SELECT hypothesis_id, signals_n FROM hypothesis_stats "
                f"WHERE hypothesis_id IN ({placeholders}) "
                f"ORDER BY computed_at DESC",
                tuple(hypothesis_ids),
            )
            rows = await cursor.fetchall()
        except Exception:
            return {}
        result: dict[str, int] = {}
        for hid, n in rows:
            if hid not in result:
                result[hid] = int(n or 0)
        return result



class ProgressMixin:

    async def _check_progress(self) -> None:
        """Ralph loop pattern: detect spinning vs making progress.

        Every 10 cycles, snapshot key metrics and compare to previous window.
        If no meaningful progress (0 new signals, 0 promotions), the loop is
        spinning — shift to diagnostic mode.

        Since R2 this delegates the decision to the pure
        ``tools.loop_quality.evaluate_progress_window`` so it is unit-testable;
        two fixes over the inline original:
          * the spinning diagnosis fires ONCE per spin episode (it previously
            re-escalated to Claude on every subsequent stagnant check);
          * a DB failure sentinel (-1) is treated as "unknown", never as
            negative progress.
        Everything else is behaviour-preserving (see characterization tests).
        """
        from tools.loop_quality import evaluate_progress_window

        PROGRESS_CHECK_INTERVAL = 10

        if self._cycles % PROGRESS_CHECK_INTERVAL != 0:
            return

        # Take snapshot of current progress
        snapshot = {
            "cycle": self._cycles,
            "promotions": self._promotions,
            "rejections": self._rejections,
            "backtests": self._backtests_run,
            "hypotheses": self._hypotheses_generated,
            "claude_calls": self._claude_escalations,
        }

        # Also query signal count from DB (-1 sentinel = unknown on failure)
        snapshot["total_signals"] = -1
        snapshot["active_backtesting"] = -1
        try:
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
            )
            row = await cursor.fetchone()
            snapshot["total_signals"] = row[0] if row else 0

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE status = 'backtesting'"
            )
            row = await cursor.fetchone()
            snapshot["active_backtesting"] = row[0] if row else 0
        except Exception:
            pass

        prev = self._progress_window[-1] if self._progress_window else None

        verdict = evaluate_progress_window(
            prev,
            snapshot,
            self._consecutive_no_progress,
            already_diagnosed_this_episode=getattr(
                self, "_diagnosis_fired_this_episode", False),
        )

        self._progress_window.append(snapshot)
        if len(self._progress_window) > 5:
            self._progress_window = self._progress_window[-5:]

        if verdict.progressing:
            self._consecutive_no_progress = 0
            self._spinning_detected = False
            self._diagnosis_fired_this_episode = False
            logger.info(f"Progress check: {verdict.detail} — loop is productive")
            return

        self._consecutive_no_progress = verdict.consecutive_no_progress
        logger.warning(
            f"Progress check: {verdict.detail}. "
            f"No-progress streak: {self._consecutive_no_progress}"
        )

        if verdict.spinning:
            self._spinning_detected = True
            logger.warning(
                f"SPINNING DETECTED: {self._consecutive_no_progress} "
                f"consecutive checks with no new signals or promotions. "
                f"Triggering diagnostic mode."
            )
        if verdict.diagnose:
            self._diagnosis_fired_this_episode = True
            await self._run_spinning_diagnosis()

    async def _run_spinning_diagnosis(self) -> None:
        """When spinning is detected, gather real data instead of re-theorizing.

        Queries the DB for concrete evidence of what's failing, then
        escalates to Claude with actionable diagnostics — not vague prompts.
        """
        from inference import escalate_with_ladder

        diag = {}
        try:
            db = self.hypothesis_manager._db

            # 1. Why are backtests producing 0 signals?
            cursor = await db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals, "
                "AVG(CASE WHEN ev_pct IS NOT NULL THEN ev_pct ELSE 0 END) as avg_ev "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            diag["events"] = {"total": row[0], "signals": row[1], "avg_ev": round(row[2] or 0, 5)}

            # 2. What edge thresholds are hypotheses using?
            cursor = await db.execute(
                "SELECT MIN(edge_threshold), MAX(edge_threshold), AVG(edge_threshold) "
                "FROM hypotheses WHERE status IN ('draft', 'backtesting')"
            )
            row = await cursor.fetchone()
            diag["thresholds"] = {"min": row[0], "max": row[1], "avg": round(row[2] or 0, 4)}

            # 3. What's the max observed edge in events?
            cursor = await db.execute(
                "SELECT MAX(ev_pct), AVG(ev_pct), "
                "COUNT(CASE WHEN ev_pct > 0.01 THEN 1 END), "
                "COUNT(CASE WHEN ev_pct > 0.02 THEN 1 END) "
                "FROM backtest_events WHERE ev_pct IS NOT NULL"
            )
            row = await cursor.fetchone()
            diag["edge_distribution"] = {
                "max_edge": round(row[0] or 0, 5),
                "avg_edge": round(row[1] or 0, 5),
                "above_1pct": row[2],
                "above_2pct": row[3],
            }

            # 4. How many books per event?
            cursor = await db.execute(
                "SELECT AVG(json_extract(model_factors, '$.books_used')) "
                "FROM backtest_events WHERE model_factors IS NOT NULL "
                "LIMIT 100"
            )
            row = await cursor.fetchone()
            diag["avg_books_used"] = round(row[0] or 0, 1)

            # 5. Hypothesis status breakdown
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            diag["hypothesis_status"] = {r[0]: r[1] for r in await cursor.fetchall()}

        except Exception as e:
            logger.warning(f"Spinning diagnosis DB query failed: {e}")
            diag["error"] = str(e)

        logger.info(f"Spinning diagnosis results: {json.dumps(diag, indent=2)}")

        # If thresholds are higher than max observed edge, that's the bottleneck
        max_edge = diag.get("edge_distribution", {}).get("max_edge", 0)
        avg_threshold = diag.get("thresholds", {}).get("avg", 0)
        if avg_threshold > 0 and max_edge > 0 and avg_threshold > max_edge:
            logger.warning(
                f"DIAGNOSIS: avg edge_threshold ({avg_threshold:.3f}) exceeds "
                f"max observed edge ({max_edge:.3f}). No hypothesis can EVER "
                f"generate a signal. Thresholds need to be lowered."
            )

        # Escalate to Claude with hard data, not theory
        if self._claude_ok():
            prompt = (
                f"CALLISTO SPINNING DIAGNOSIS — EMERGENCY\n\n"
                f"The research loop has run {self._consecutive_no_progress * 10}+ cycles "
                f"with ZERO new signals and ZERO promotions. This is not working.\n\n"
                f"HARD DATA (from actual database queries, not estimates):\n"
                f"{json.dumps(diag, indent=2)}\n\n"
                f"CRITICAL QUESTION: Why is the loop producing zero value?\n"
                f"Your answer must be ONE specific, actionable root cause based "
                f"on the data above — not a list of possibilities.\n\n"
                f"RESPOND WITH JSON:\n"
                f'{{"root_cause": "single sentence", '
                f'"evidence": "which numbers above prove it", '
                f'"fix": "exact change needed"}}'
            )
            try:
                result = await escalate_with_ladder(
                    prompt,
                    task_type="deep_work",
                    hermes_caller="deep_work",
                )
                if result.get("content"):
                    logger.warning(f"Spinning diagnosis from Claude: {result['content'][:500]}")
            except Exception as e:
                logger.warning(f"Claude spinning diagnosis failed: {e}")

