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
  * DeferredQueueMixin    — never-idle deferred work queue: draining queued
                            items when Claude returns, processing drained
                            results per work_type
  * CycleLoopMixin        — the main research cycle (`_loop`), quant scan
  * CorrelationMixin      — pairwise hypothesis correlation matrix + signal
                            count map for the portfolio/Kelly layer
  * ProgressMixin         — Ralph-loop progress tracking, spinning detection,
                            data-driven spinning diagnosis

GATE POLICY notes preserved verbatim from the original inline code:
threshold migration and historical-evidence rewrites stay behind the
CALLISTO_ALLOW_THRESHOLD_MIGRATION=1 operator opt-in.
"""

import asyncio
import gc
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from tools.loop.sequencer import PERIODIC_PHASES, PHASES
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



class DeferredQueueMixin:

    async def _drain_deferred_queue(self) -> None:
        """If Claude is available and we have queued work, drain it first.

        This is the critical path: when Claude comes back online after a
        rate-limit window, all deferred hypothesis generation, interpretation,
        and deep work gets executed immediately before the normal cycle.
        """
        from tools.claude_code import is_available as claude_available
        from inference import escalate_with_ladder

        claude_up = claude_available() and not self._local_only

        # Track Claude availability transitions
        if claude_up and not self._was_claude_available:
            self._downtime_tracker.mark_available()
        elif not claude_up and self._was_claude_available:
            self._downtime_tracker.mark_unavailable()
        self._was_claude_available = claude_up

        if not claude_up:
            return

        pending = await self._work_queue.size()
        if pending == 0:
            return

        logger.info(f"Claude available -- draining {pending} deferred items")
        drained = await self._work_queue.drain(max_items=5)

        for item in drained:
            if not self._running:
                break
            try:
                # Route through the ladder; work_type maps onto the
                # ladder task_type bucket. Unknown work_types fall back
                # to 'reasoning', which is the default bucket.
                _task_type = item["work_type"] if item["work_type"] in (
                    "hypothesis_gen", "deep_work", "reasoning"
                ) else "reasoning"
                result = await escalate_with_ladder(
                    item["prompt"],
                    task_type=_task_type,
                    hermes_caller=item["work_type"],
                )
                self._last_claude_call = time.time()
                self._claude_escalations += 1

                if result.get("content") and not result.get("error"):
                    # Process based on work type
                    await self._process_drained_item(item, result["content"])
                    await self._work_queue.mark_done(item["id"], result["content"][:500])
                    logger.info(
                        f"Drained item {item['id']} ({item['work_type']}): success"
                    )
                elif result.get("rate_limited"):
                    # Claude went away again -- put item back
                    await self._work_queue.mark_failed(item["id"], "rate_limited_during_drain")
                    logger.info("Claude rate-limited during drain -- stopping drain")
                    break
                else:
                    await self._work_queue.mark_done(
                        item["id"], f"error: {result.get('error', 'unknown')}"
                    )
            except Exception as e:
                await self._work_queue.mark_failed(item["id"], str(e))
                logger.warning(f"Drain item {item['id']} failed: {e}")

        # Record downtime pattern every 10 cycles
        if self._cycles % 10 == 0:
            await self._downtime_tracker.record_to_hermes()

    async def _process_drained_item(self, item: dict, content: str) -> None:
        """Process the result of a drained deferred work item."""
        work_type = item["work_type"]
        try:
            # Extract JSON from response
            json_str = content
            if "```" in json_str:
                parts = json_str.split("```")
                for part in parts:
                    stripped = part.strip()
                    if stripped.startswith("json"):
                        stripped = stripped[4:].strip()
                    if stripped.startswith("{"):
                        json_str = stripped
                        break
            elif "{" in json_str:
                start = json_str.index("{")
                end = json_str.rindex("}") + 1
                json_str = json_str[start:end]

            parsed = json.loads(json_str)

            if work_type == "hypothesis_gen":
                created = 0
                for nh in parsed.get("hypotheses", []):
                    try:
                        _dq_config = {
                                "source": "deferred_queue_claude",
                                "cycle": self._cycles,
                                "training_period_start": "2023-01-01",
                                "training_period_end": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                ),
                                "forward_test_start": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                    + timedelta(days=BACKTEST_GAP_DAYS)
                                ),
                        }
                        if nh.get("game_filters"):
                            _dq_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _dq_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", f"deferred_gen_{self._cycles}"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_dq_config,
                        )
                        created += 1
                    except Exception as e:
                        logger.warning(f"Failed to create deferred hypothesis: {e}")
                if created:
                    self._hypotheses_generated += created
                    logger.info(f"Deferred drain: created {created} hypotheses")

            elif work_type == "deep_work":
                # Same processing as _phase_claude_deep_work
                rejected = 0
                for hid in parsed.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_claude_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                created = 0
                for nh in parsed.get("new_hypotheses", []):
                    try:
                        _ddw_config = {
                                "source": "deferred_deep_work",
                                "cycle": self._cycles,
                                "training_period_start": "2023-01-01",
                                "training_period_end": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                ),
                                "forward_test_start": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                    + timedelta(days=BACKTEST_GAP_DAYS)
                                ),
                        }
                        if nh.get("game_filters"):
                            _ddw_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _ddw_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", "deferred_deep"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_ddw_config,
                        )
                        created += 1
                    except Exception:
                        pass
                if rejected or created:
                    self._hypotheses_generated += created
                    logger.info(
                        f"Deferred drain deep_work: rejected {rejected}, created {created}"
                    )

                # Route pipeline_issues to self-repair (same as _phase_claude_deep_work)
                pipeline_issues = parsed.get("pipeline_issues", [])
                if pipeline_issues:
                    findings = []
                    for issue in pipeline_issues:
                        issue_lower = issue.lower() if isinstance(issue, str) else ""
                        if any(kw in issue_lower for kw in ["identical", "same games", "filtering bug", "broken"]):
                            severity = "CRITICAL"
                        elif any(kw in issue_lower for kw in ["prioritize", "threshold", "zero promotion", "low sample"]):
                            severity = "HIGH"
                        else:
                            severity = "LOW"
                        findings.append({"severity": severity, "description": issue})
                    try:
                        from tools.self_repair import get_repair_engine
                        engine = get_repair_engine()
                        repair_results = await engine.handle_claude_findings(findings)
                        for r in repair_results:
                            if r["fixed"]:
                                logger.info(f"Deferred deep work auto-fix: {r['action']} — {r['detail']}")
                            else:
                                logger.warning(f"Deferred deep work unfixed: {r['action']} — {r['detail']}")
                    except Exception as e:
                        logger.warning(f"Deferred drain: failed to route findings to self-repair: {e}")

            elif work_type == "interpret_backtests":
                # Same processing as _phase_interpret_backtests
                db = self.data_collector._db
                rejected = 0
                for hid in parsed.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_interpret"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                modified = 0
                refused = 0
                for mod in parsed.get("modify", []):
                    try:
                        hid = mod.get("id")
                        new_thresh = mod.get("new_threshold")
                        if hid and new_thresh is not None and db:
                            # GATE POLICY: same direction guard as
                            # _phase_interpret_backtests — automated actors may
                            # raise a gate but never lower it. This drain path
                            # previously bypassed that guard entirely.
                            new_thresh = max(MIN_EDGE_THRESHOLD_FLOOR,
                                             min(MAX_EDGE_THRESHOLD_CEILING,
                                                 float(new_thresh)))
                            cur = await db.execute(
                                "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?",
                                (hid,),
                            )
                            row = await cur.fetchone()
                            current = float(row[0]) if row and row[0] is not None else None
                            if current is None:
                                continue
                            if new_thresh < current:
                                refused += 1
                                logger.warning(
                                    "GATE POLICY REFUSED (deferred drain) threshold "
                                    "LOWERING hyp=%s %s -> %s — recorded for human review",
                                    hid, current, new_thresh,
                                )
                                await db.execute(
                                    "UPDATE hypotheses SET notes = COALESCE(notes, '') || ? "
                                    "WHERE hypothesis_id = ?",
                                    (f"\n[cycle {self._cycles}] REFUSED deferred-drain "
                                     f"threshold lowering {current} -> {new_thresh} "
                                     f"(gate policy; human decision required)", hid),
                                )
                                await db.commit()
                                continue
                            await db.execute(
                                "UPDATE hypotheses SET edge_threshold = ? WHERE hypothesis_id = ?",
                                (new_thresh, hid),
                            )
                            await db.commit()
                            modified += 1
                    except Exception:
                        pass
                if rejected or modified:
                    logger.info(
                        f"Deferred drain interpret: rejected {rejected}, "
                        f"raised {modified}, refused {refused}"
                    )

            elif work_type == "system_improvement":
                db = self.data_collector._db
                stored = 0
                for imp in parsed.get("improvements", []):
                    try:
                        if db:
                            await db.execute(
                                "INSERT INTO system_improvements "
                                "(cycle, category, suggestion, priority) VALUES (?, ?, ?, ?)",
                                (self._cycles, imp.get("category", "general"),
                                 imp.get("suggestion", ""), imp.get("priority", "medium")),
                            )
                            stored += 1
                    except Exception:
                        pass
                if stored and db:
                    await db.commit()
                    logger.info(f"Deferred drain: stored {stored} system improvements")

            elif work_type == "diagnostic_escalation":
                logger.info(f"Deferred diagnostic processed: {content[:200]}")

        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Deferred item {work_type} response not valid JSON: {e}")



class CycleLoopMixin:

    async def _quant_scan_loop(self) -> None:
        """Continuously refresh the live edge surface.

        Every ``QUANT_SCAN_INTERVAL_S`` seconds, pull current odds for
        every research sport, build per-market snapshots across all
        available books, run the ranker, and persist the output. The
        resulting table (``live_edge_surface``) is what the /edges/live
        API endpoint reads, what the Telegram alerting can consume, and
        what the bet_executor will read once it's enabled.

        Runs independently of the main research cycle so the two
        cadences don't fight each other. Research cycle is human-scale
        (5 min, statistical work). Quant scan is market-scale (60s,
        line movement and soft-book divergence).
        """
        import os as _os
        interval = float(_os.getenv("CALLISTO_QUANT_SCAN_INTERVAL_S", "60"))
        # Brief startup delay so the main loop wins initial DB contention
        # and telemetry collectors have a chance to populate.
        await asyncio.sleep(30)

        from tools.quant import scan_all_sports
        while self._running:
            if self._paused:
                await asyncio.sleep(min(interval, 15))
                continue
            try:
                db = self.data_collector._db if self.data_collector else None
                if db is None:
                    await asyncio.sleep(interval)
                    continue
                result = await scan_all_sports(
                    list(RESEARCH_SPORTS),
                    db,
                    placement_books={"draftkings", "fanatics"},
                    min_recommend_edge=0.02,
                    top_n_per_sport=25,
                )
                total = result.get("total_recommended", 0)
                if total:
                    logger.info(
                        f"Quant scan: {total} recommended edges across "
                        f"{sum(1 for r in result['per_sport'].values() if r.get('recommended'))} "
                        f"sports"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Quant scan loop iteration failed: {e}")
            await asyncio.sleep(interval)

    async def _loop(self) -> None:
        """Main research cycle."""
        # Brief delay to let other systems start
        await asyncio.sleep(15)

        while self._running:
            try:
                self._cycles += 1
                self._reactive_collected.clear()
                _cycle_start = time.monotonic()
                logger.info(f"Research cycle #{self._cycles} starting")

                # Pause check — sleep and skip cycle
                if self._paused:
                    logger.info(f"Research cycle #{self._cycles} skipped (PAUSED)")
                    await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)
                    continue

                # ── Pause line_monitor for ENTIRE cycle to prevent SQLite lock cascade.
                # All phases do DB writes; concurrent line_monitor snapshots cause
                # deadlocks even with 120s busy_timeout. Snapshots catch up between cycles.
                # wait_for_drain() sets _paused, waits for loop ack AND in-flight DB
                # ops to complete — no more fire-and-forget WAL contention.
                if self.line_monitor:
                    drained = await self.line_monitor.wait_for_drain(timeout=30)
                    if drained:
                        logger.debug("line_monitor paused and drained for research cycle")
                    else:
                        logger.warning("line_monitor drain incomplete — proceeding (may contend on WAL)")

                # ── Sequential phases — order lives in tools.loop.sequencer ──
                # Each phase runs under its own wait_for timeout; failures are
                # recorded non-fatally via the phase-failure ledger.
                for spec in PHASES:
                    if spec.every_n and self._cycles % spec.every_n != 0:
                        continue
                    try:
                        coro = getattr(self, spec.method)()
                        if spec.timeout is None:
                            await coro
                        else:
                            await asyncio.wait_for(coro, timeout=spec.timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Phase {spec.name} timed out after {spec.timeout}s — skipping"
                        )
                        self._record_phase_failure(spec.name, "timeout")
                    except Exception as e:
                        logger.warning(f"Phase {spec.name} failed (non-fatal): {e}")
                        self._record_phase_failure(spec.name, "exception", e)

                    if not self._running:
                        break

                if not self._running:
                    break

                # ── Periodic phases: defer if core phases already consumed >5 min ──
                # This prevents phase collision from stacking 10+ min cycles
                # (was causing stalls at cycles 6, 10, 15, 16, 20).
                _cycle_elapsed = time.monotonic() - _cycle_start
                _CYCLE_TIME_BUDGET = 300  # 5 min — if core phases took this long, skip periodic
                if _cycle_elapsed > _CYCLE_TIME_BUDGET:
                    logger.info(
                        f"Cycle #{self._cycles} core phases took {_cycle_elapsed:.0f}s "
                        f"(>{_CYCLE_TIME_BUDGET}s) — deferring periodic phases"
                    )
                else:
                    for spec in PERIODIC_PHASES:
                        if spec.every_n and self._cycles % spec.every_n != 0:
                            continue
                        try:
                            coro = getattr(self, spec.method)()
                            await asyncio.wait_for(coro, timeout=spec.timeout)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"Phase {spec.name} timed out after {spec.timeout}s — skipping"
                            )
                            self._record_phase_failure(spec.name, "timeout")
                        except Exception as e:
                            logger.warning(f"Phase {spec.name} failed (non-fatal): {e}")
                            self._record_phase_failure(spec.name, "exception", e)

                        if not self._running:
                            break

                    if not self._running:
                        break

                # ── Progress tracking: detect spinning ──
                await self._check_progress()

                _cycle_total = time.monotonic() - _cycle_start

                # Force garbage collection after each cycle — large numpy arrays
                # and JSON dicts from backtest processing don't always get freed promptly.
                # Also clear linecache (tracemalloc causes it to grow ~1.5 MB/session).
                gc.collect()
                gc.collect()  # Second pass catches reference cycles
                import linecache
                linecache.clearcache()

                # ── Memory telemetry: track RSS per cycle to detect leaks ──
                try:
                    import psutil
                    _rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                    logger.info(
                        f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s | "
                        f"RSS={_rss_mb:.0f}MB | KL_cache={len(self.line_monitor._kl_cache) if self.line_monitor else '?'}"
                    )
                except Exception:
                    logger.info(f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s")

                # Proactive DB prune — prop_snapshots grows 15K rows/hr,
                # backtest_events from rejected hypotheses bloat DB indefinitely
                try:
                    import aiosqlite
                    _prune_db = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
                    _prune_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                    async with aiosqlite.connect(_prune_db) as _pdb:
                        await _pdb.execute("PRAGMA busy_timeout = 60000")
                        await _pdb.execute(
                            "DELETE FROM prop_snapshots WHERE snapshot_time < ?",
                            (_prune_cutoff,)
                        )
                        await _pdb.execute(
                            "DELETE FROM deferred_work_queue WHERE status = 'done' AND created_at < ?",
                            (_prune_cutoff,)
                        )
                        # Prune backtest_events for rejected hypotheses (>2 days old)
                        # With 3192 rejected hyps, this recovers massive DB space
                        _pruned = await _pdb.execute(
                            "DELETE FROM backtest_events WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        _pruned_count = _pruned.rowcount
                        # Also prune backtest_runs for rejected hypotheses
                        await _pdb.execute(
                            "DELETE FROM backtest_runs WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        await _pdb.commit()
                        if _pruned_count > 0:
                            logger.info(
                                f"DB prune: deleted {_pruned_count} backtest_events "
                                f"from rejected hypotheses"
                            )
                        # WAL checkpoint — prevents unbounded WAL growth (was 1.4GB).
                        # Persistent connections block wal_autocheckpoint; this fresh
                        # connection after commit can checkpoint freed pages.
                        try:
                            wal_result = await (await _pdb.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            )).fetchone()
                            if wal_result:
                                busy, log, ckpt = wal_result
                                if log > 0:
                                    logger.info(
                                        f"WAL checkpoint: {ckpt}/{log} pages "
                                        f"(busy={busy})"
                                    )
                        except Exception as wal_e:
                            logger.debug(f"WAL checkpoint: {wal_e}")
                except Exception:
                    pass  # Non-critical — self_repair will catch it

                # Force GC to reclaim large transient allocations from backtest/resolve
                # phases. CPython's pymalloc holds freed blocks; gc.collect() nudges
                # the allocator to release pages back to the OS.
                gc.collect()

                # ── Unpause line_monitor BEFORE sleeping so it can take snapshots
                # during the inter-cycle window. Previously this was in the finally
                # block which ran after the sleep, giving the monitor ~0ms to run.
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock atomically
                    self.line_monitor._pause_ack.clear()
                    logger.info("line_monitor unpaused for inter-cycle snapshot window")

                logger.info(
                    f"Research cycle #{self._cycles} complete — "
                    f"sleeping {RESEARCH_CYCLE_INTERVAL}s"
                )
                await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Research loop error: {e}", exc_info=True)
                await asyncio.sleep(120)
            finally:
                # ── Safety net: always unpause on exception/cancel too ──
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock if held
                    self.line_monitor._pause_ack.clear()



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

