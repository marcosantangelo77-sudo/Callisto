"""System-improvement ResearchLoop phase, extracted from post_live.

Callers still import this name from tools.loop.phases.post_live / phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays defined in the phases_impl facade (not relocated).
"""
from __future__ import annotations

import json
import time

from tools.loop import phases_impl as _impl

logger = _impl.logger
SYSTEM_IMPROVEMENT_INTERVAL = _impl.SYSTEM_IMPROVEMENT_INTERVAL
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN


async def phase_system_improvement(loop) -> None:
    self = loop
    """Self-improvement phase — runs every SYSTEM_IMPROVEMENT_INTERVAL cycles.

    Asks Claude to review pipeline metrics and suggest specific code
    improvements. Stores suggestions in a system_improvements table.
    This is how the system learns to improve itself over time.
    """
    if self._cycles % SYSTEM_IMPROVEMENT_INTERVAL != 0:
        return

    from inference import escalate_with_ladder

    now = time.time()
    remaining = CLAUDE_ESCALATION_COOLDOWN - (now - self._last_claude_call)
    if remaining > 0:
        logger.debug(f"System improvement: cooldown active ({remaining:.0f}s left), deferring to next interval")
        return

    db = self.data_collector._db
    if not db:
        return

    # Ensure system_improvements table exists
    try:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_improvements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle INTEGER NOT NULL,
                category TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                implemented_at DATETIME
            )
        """)
        await db.commit()
    except Exception as e:
        logger.warning(f"Failed to create system_improvements table: {e}")
        return

    # Gather comprehensive pipeline metrics
    metrics = {}
    try:
        # Hypothesis pipeline funnel
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
        )
        metrics["hypothesis_funnel"] = {r[0]: r[1] for r in await cursor.fetchall()}

        # Backtest throughput
        cursor = await db.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) signals, "
            "SUM(CASE WHEN signal_generated=1 AND actual_result='won' THEN 1 ELSE 0 END) wins, "
            "SUM(CASE WHEN signal_generated=1 AND actual_result='lost' THEN 1 ELSE 0 END) losses, "
            "SUM(CASE WHEN actual_result IS NULL THEN 1 ELSE 0 END) unresolved "
            "FROM backtest_events"
        )
        row = await cursor.fetchone()
        if row:
            metrics["backtest_totals"] = {
                "events": row[0] or 0, "signals": row[1] or 0,
                "wins": row[2] or 0, "losses": row[3] or 0,
                "unresolved": row[4] or 0,
            }

        # Data coverage
        cursor = await db.execute(
            "SELECT sport, COUNT(*), MIN(snapshot_date), MAX(snapshot_date) "
            "FROM historical_odds_cache GROUP BY sport"
        )
        metrics["data_coverage"] = {
            r[0]: {"records": r[1], "from": r[2], "to": r[3]}
            for r in await cursor.fetchall()
        }

        # Loop performance
        metrics["loop_stats"] = {
            "cycles": self._cycles,
            "data_collections": self._data_collections,
            "hypotheses_generated": self._hypotheses_generated,
            "backtests_run": self._backtests_run,
            "claude_escalations": self._claude_escalations,
            "promotions": self._promotions,
            "rejections": self._rejections,
        }

        # Previous improvements (to avoid repetition)
        cursor = await db.execute(
            "SELECT suggestion FROM system_improvements "
            "ORDER BY created_at DESC LIMIT 20"
        )
        metrics["recent_suggestions"] = [r[0] for r in await cursor.fetchall()]

    except Exception as e:
        logger.warning(f"Failed to gather metrics for system improvement: {e}")

    prompt = (
        f"CALLISTO SYSTEM IMPROVEMENT REVIEW — Cycle #{self._cycles}\n\n"
        f"You are an adversarial auditor of this pipeline. Your job is to find "
        f"the single biggest bottleneck and propose a concrete fix.\n\n"
        f"PIPELINE METRICS:\n{json.dumps(metrics, indent=2)}\n\n"
        f"RECENT SUGGESTIONS (already made, avoid repeating):\n"
        f"{json.dumps(metrics.get('recent_suggestions', []))}\n\n"
        f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
        f'{{"diagnosis": "1-sentence root cause of why 0 hypotheses have been promoted", '
        f'"improvements": [\n'
        f'  {{"category": "data_collection|hypothesis_gen|backtesting|evaluation|infrastructure", '
        f'"suggestion": "Specific actionable improvement", '
        f'"priority": "high|medium|low", '
        f'"rationale": "Why this would help based on the metrics"}}\n'
        f"]}}\n\n"
        f"RULES:\n"
        f"- diagnosis FIRST: why has the promotion rate been 0%? Be brutally specific.\n"
        f"- 2-4 suggestions, ranked by impact on the BOTTLENECK (not generic improvements)\n"
        f"- Each must be specific and implementable (not vague)\n"
        f"- If the bottleneck is data quality (few books, thin markets), say so\n"
        f"- If the bottleneck is evaluation criteria (too strict), say so\n"
        f"- Do NOT suggest generating more hypotheses if the funnel is broken\n"
        f"- Do NOT repeat recent suggestions\n"
    )

    if not self._claude_ok():
        # Defer system improvement to queue for when Claude returns
        await self._work_queue.enqueue("system_improvement", prompt, priority=4)
        self._downtime_tracker.item_queued()
        logger.info("Research: system improvement deferred to work queue (Claude unavailable)")
        return

    try:
        result = await escalate_with_ladder(
            prompt,
            task_type="deep_work",
            hermes_caller="deep_work",
        )
        self._last_claude_call = time.time()
        self._claude_escalations += 1

        if result.get("content") and not result.get("error"):
            content = result["content"]
            try:
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
                stored = 0
                for imp in parsed.get("improvements", []):
                    try:
                        await db.execute(
                            "INSERT INTO system_improvements "
                            "(cycle, category, suggestion, priority) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                self._cycles,
                                imp.get("category", "general"),
                                imp.get("suggestion", ""),
                                imp.get("priority", "medium"),
                            ),
                        )
                        stored += 1
                    except Exception as e:
                        logger.warning(f"Failed to store system improvement suggestion: {e}")
                if stored:
                    await db.commit()
                    logger.info(
                        f"Research: system improvement stored {stored} suggestions "
                        f"at cycle #{self._cycles}"
                    )

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"System improvement response not valid JSON: {e}")

        elif result.get("rate_limited"):
            logger.info("Research: Claude rate-limited during system improvement")
    except Exception as e:
        logger.warning(f"System improvement phase failed: {e}")
