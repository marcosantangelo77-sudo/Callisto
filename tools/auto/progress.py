"""Ralph-loop progress tracking extracted from tools.auto.research.

``ProgressMixin`` snapshots key metrics every 10 cycles via
``evaluate_progress_window``, detects spinning, and runs a data-driven
spinning diagnosis (DB queries + Claude JSON prompt). Re-exported from
tools.auto.research so ResearchLoop composition and slice3 hasattr pins
stay intact.

Do not import tools.autonomous. Do not arm live betting.
Do not add live to paper-signal.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("callisto.auto.research")


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
