"""Self-repair / diagnose / signal-refresh phases, extracted from phases_impl.

Callers still import these names from tools.loop.phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays in phases_impl with CALLISTO_ALLOW_LIVE_EXECUTE.
phase_refresh_signals write path stays gated on CALLISTO_ALLOW_SIGNAL_REFRESH=1.
"""
from __future__ import annotations

import json
import os
import time

from tools.loop import phases_impl as _impl

logger = _impl.logger
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN


async def phase_self_repair(loop) -> None:
    self = loop
    """
    Self-repair phase — detect issues, fix them autonomously, verify,
    and record to Hermes. Runs every 5 cycles to avoid overhead.
    Also runs cache rotation to maintain operational hygiene.
    """
    if self._cycles % 5 != 1:
        return  # Only run every 5 cycles (cycle 1, 6, 11, ...)

    # Cache rotation — rebuild hot cache, archive stale data
    try:
        from tools.cache_manager import rotate_caches
        await rotate_caches()
    except Exception as e:
        logger.debug(f"Cache rotation failed (non-fatal): {e}")

    try:
        from tools.self_repair import get_repair_engine
        engine = get_repair_engine()
        result = await engine.run_repair_cycle()

        if result["fixed"] > 0:
            logger.info(
                f"Self-repair: fixed {result['fixed']}/{result['issues_found']} issues"
            )

        # Record phase success for pipeline integrity tracking
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("self_repair", True)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Self-repair phase failed: {e}", exc_info=True)
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("self_repair", False)
        except Exception:
            pass


async def phase_self_diagnose(loop) -> None:
    self = loop
    """
    Self-diagnostic phase — detects broken pipelines BEFORE wasting cycles.

    Checks data quality, pipeline throughput, and data freshness.
    Escalates critical issues to Claude Code exactly once per issue.
    """
    from datetime import datetime, timedelta, timezone

    db = self.data_collector._db
    if db is None:
        logger.warning("DIAG: data_collector DB not initialized, skipping diagnostics")
        return

    issues: list[dict] = []  # {"key": str, "severity": str, "message": str}

    # ── 1. Data quality: avg books per record per sport ──
    try:
        cursor = await db.execute(
            "SELECT sport, COUNT(*) as cnt "
            "FROM historical_odds_cache GROUP BY sport"
        )
        rows = await cursor.fetchall()
        for row in rows:
            sport, cnt = row[0], row[1]
            # Sample up to 50 records to estimate avg books
            sample_cursor = await db.execute(
                "SELECT response_json FROM historical_odds_cache "
                "WHERE sport = ? ORDER BY RANDOM() LIMIT 50",
                (sport,),
            )
            samples = await sample_cursor.fetchall()
            total_books = 0
            parsed = 0
            scores_only = 0
            usable = 0
            for (rj,) in samples:
                try:
                    data = json.loads(rj) if isinstance(rj, str) else rj
                    # Cached format: {"games": [...], "sport": "...", ...}
                    # Each game has a "bookmakers" list
                    games = []
                    if isinstance(data, dict) and "games" in data:
                        games = data["games"]
                    elif isinstance(data, list):
                        games = data
                    elif isinstance(data, dict) and "bookmakers" in data:
                        games = [data]

                    record_books = 0
                    for game in games:
                        bm_count = len(game.get("bookmakers", []))
                        total_books += bm_count
                        record_books = max(record_books, bm_count)
                        parsed += 1
                    if record_books == 0:
                        scores_only += 1
                    elif record_books >= 2:
                        usable += 1
                except (json.JSONDecodeError, TypeError):
                    continue
            avg_books = total_books / max(parsed, 1)
            if scores_only > 0 and usable == 0:
                issue_key = f"scores_only_{sport}"
                msg = (
                    f"DIAG: {sport} has {cnt} cached records but ALL are "
                    f"scores-only (0 bookmakers) — no odds data for backtesting"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})
            elif avg_books < 2:
                issue_key = f"low_books_{sport}"
                msg = (
                    f"DIAG: {sport} has avg {avg_books:.1f} books/game "
                    f"({cnt} records, {usable}/{len(samples)} usable) — "
                    f"backtests against <2 books are unreliable"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
            else:
                logger.info(
                    f"DIAG: {sport} data quality OK — avg {avg_books:.1f} books/game "
                    f"({cnt} records, {usable}/{len(samples)} usable)"
                )
    except Exception as e:
        logger.warning(f"DIAG: data quality check failed: {e}")

    # ── 1b. Check game_results overlap with odds data ──
    try:
        cursor = await db.execute(
            "SELECT h.sport, COUNT(DISTINCT h.snapshot_date) as odds_dates, "
            "COUNT(DISTINCT g.game_date) as result_dates "
            "FROM historical_odds_cache h "
            "LEFT JOIN game_results g ON h.sport = g.sport "
            "AND h.snapshot_date = g.game_date "
            "GROUP BY h.sport"
        )
        for row in await cursor.fetchall():
            sport, odds_dates, result_dates = row[0], row[1], row[2]
            if odds_dates > 0 and result_dates == 0:
                issue_key = f"no_results_overlap_{sport}"
                msg = (
                    f"DIAG: {sport} has {odds_dates} odds dates but 0 matching "
                    f"game_results dates — backtest resolution will fail"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
    except Exception as e:
        logger.warning(f"DIAG: date overlap check failed: {e}")

    # ── 2. Pipeline throughput ──
    try:
        if self._hypotheses_generated > 0 and self._backtests_run > 0:
            # Check total signals across all backtest events
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT event_id) as total, "
                "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END) as signals "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            if row:
                total_events, total_signals = row[0] or 0, row[1] or 0
                if total_events > 0:
                    signal_rate = total_signals / total_events
                    if signal_rate < 0.01:
                        issue_key = "low_signal_rate"
                        msg = (
                            f"DIAG: signal rate {signal_rate:.2%} "
                            f"({total_signals}/{total_events} events) — "
                            f"<1% signal generation indicates broken hypothesis logic"
                        )
                        logger.warning(msg)
                        issues.append({"key": issue_key, "severity": "WARNING", "message": msg})

        if self._backtests_run >= 100 and self._promotions == 0:
            issue_key = "zero_promotions"
            msg = (
                f"DIAG: 0 promotions after {self._backtests_run} backtests — "
                f"promotion gates may be too strict or data insufficient"
            )
            logger.warning(msg)
            issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
    except Exception as e:
        logger.warning(f"DIAG: throughput check failed: {e}")

    # ── 3. Data freshness ──
    try:
        now = datetime.now(timezone.utc)

        # Latest game_context
        cursor = await db.execute(
            "SELECT MAX(game_date) FROM game_contexts WHERE sport != 'meta_research'"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                latest_ctx = datetime.strptime(str(row[0]), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                age_days = (now - latest_ctx).days
                if age_days > 2:
                    issue_key = "stale_game_contexts"
                    msg = (
                        f"DIAG: latest game_context is {age_days} days old "
                        f"({row[0]}) — data collection may be broken"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
            except ValueError:
                pass
        else:
            issue_key = "no_game_contexts"
            msg = "DIAG: no game_contexts found at all — data collection has never succeeded"
            logger.warning(msg)
            issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})

        # Latest odds_snapshot (from line_monitor's table)
        try:
            cursor = await db.execute(
                "SELECT MAX(timestamp) FROM odds_snapshots"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    latest_snap = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                    if latest_snap.tzinfo is None:
                        latest_snap = latest_snap.replace(tzinfo=timezone.utc)
                    age_hours = (now - latest_snap).total_seconds() / 3600
                    if age_hours > 1:
                        issue_key = "stale_odds_snapshots"
                        msg = (
                            f"DIAG: latest odds_snapshot is {age_hours:.1f}h old — "
                            f"snapshot collection may be failing"
                        )
                        logger.warning(msg)
                        issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            # odds_snapshots table may be in a different DB (line_monitor's)
            logger.info(f"DIAG: odds_snapshots freshness check skipped: {e}")

    except Exception as e:
        logger.warning(f"DIAG: freshness check failed: {e}")

    # ── 4. Escalate critical issues to Claude Code (once per issue) ──
    new_critical = [
        i for i in issues
        if i["severity"] == "CRITICAL" and i["key"] not in self._diagnostic_issues
    ]
    if new_critical:
        from inference import escalate_with_ladder

        now = time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            logger.debug("DIAG: skipping Claude escalation — cooldown active")
        elif self._claude_ok():
            # Load error patterns for institutional memory
            _error_patterns = ""
            try:
                with open("memory/error_patterns.md", "r") as f:
                    _error_patterns = f.read()[:1500]
            except Exception:
                pass

            diag_report = (
                "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                + "\n".join(
                    f"[{i['severity']}] {i['message']}" for i in issues
                )
                + "\n\nPipeline state:\n"
                f"- Cycles: {self._cycles}\n"
                f"- Hypotheses generated: {self._hypotheses_generated}\n"
                f"- Backtests run: {self._backtests_run}\n"
                f"- Promotions: {self._promotions}\n"
                f"- Rejections: {self._rejections}\n\n"
                + (f"KNOWN ERROR PATTERNS (do not repeat):\n{_error_patterns}\n\n" if _error_patterns else "")
                + f"Analyze these diagnostics and suggest specific fixes. "
                f"Focus on: which data is missing, what to collect, "
                f"and whether the pipeline should pause or adjust parameters."
            )
            try:
                result = await escalate_with_ladder(
                    diag_report,
                    task_type="deep_work",
                    hermes_caller="default",
                )
                self._last_claude_call = time.time()
                if result.get("content") and not result.get("error"):
                    logger.info(
                        f"DIAG: Claude analysis received — "
                        f"{len(result['content'])} chars"
                    )
                # Mark all critical issues as escalated regardless of response
                for i in new_critical:
                    self._diagnostic_issues.add(i["key"])
            except Exception as e:
                logger.warning(f"DIAG: Claude escalation failed: {e}")
        else:
            # Defer diagnostic escalation to queue for when Claude returns
            diag_report = (
                "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                + "\n".join(
                    f"[{i['severity']}] {i['message']}" for i in issues
                )
                + "\n\nPipeline state:\n"
                f"- Cycles: {self._cycles}\n"
                f"- Hypotheses generated: {self._hypotheses_generated}\n"
                f"- Backtests run: {self._backtests_run}\n"
                f"- Promotions: {self._promotions}\n"
                f"- Rejections: {self._rejections}\n\n"
                f"Analyze these diagnostics and suggest specific fixes. "
                f"Focus on: which data is missing, what to collect, "
                f"and whether the pipeline should pause or adjust parameters."
            )
            await self._work_queue.enqueue("diagnostic_escalation", diag_report, priority=1)
            self._downtime_tracker.item_queued()
            logger.warning(
                f"DIAG: {len(new_critical)} critical issues deferred to work queue "
                f"(Claude unavailable)"
            )

    # Mark non-critical issues as seen too (no re-escalation)
    for i in issues:
        if i["severity"] != "CRITICAL":
            self._diagnostic_issues.add(i["key"])

    # Evict oldest entries if set exceeds cap (prevents unbounded growth)
    if len(self._diagnostic_issues) > self._DIAGNOSTIC_ISSUES_MAX:
        # Sets are unordered; drop arbitrary entries to get back under limit
        excess = len(self._diagnostic_issues) - self._DIAGNOSTIC_ISSUES_MAX
        for _ in range(excess):
            self._diagnostic_issues.pop()

    if not issues:
        logger.info("DIAG: all pipeline health checks passed")


async def phase_refresh_signals(loop) -> None:
    self = loop
    """Retroactive signal refresh — WRITE PATH GATED, OFF BY DEFAULT.

    This phase used to UPDATE backtest_events.signal_generated = 1 whenever
    edge >= threshold, which let a later threshold drop retroactively
    rewrite history (laundered evidence). By default this phase is now
    DIAGNOSE-ONLY: it counts rows that *would* have been upgraded (SELECT,
    no UPDATE) and returns without writing.

    The write path is operator-explicit only:
        CALLISTO_ALLOW_SIGNAL_REFRESH=1
    enables the original retroactive UPDATE (backtest_events +
    backtest_runs.signals_generated + stats recalc).
    """
    import aiosqlite

    db_path = self.backtest_engine.db_path
    allow_write = os.getenv("CALLISTO_ALLOW_SIGNAL_REFRESH") == "1"
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            # Diagnose-only: count events where edge now exceeds threshold
            # but signal=0. Read-only — no evidence rewriting.
            count_row = await db.execute(
                """SELECT COUNT(*) FROM backtest_events be
                   JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
                   WHERE be.edge >= h.edge_threshold AND be.edge > 0
                   AND be.signal_generated = 0"""
            )
            row = await count_row.fetchone()
            would_upgrade = row[0] if row else 0
            if not allow_write:
                if would_upgrade:
                    logger.info(
                        f"Signal refresh: {would_upgrade} events WOULD be "
                        "upgraded to signal=1 (write gated; set "
                        "CALLISTO_ALLOW_SIGNAL_REFRESH=1 to enable)"
                    )
                return

            # ── Gated write path (operator-explicit) ──
            # Find events where edge now exceeds threshold but signal=0
            updated = await db.execute(
                """UPDATE backtest_events SET signal_generated = 1
                   WHERE id IN (
                       SELECT be.id FROM backtest_events be
                       JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
                       WHERE be.edge >= h.edge_threshold AND be.edge > 0
                       AND be.signal_generated = 0
                   )"""
            )
            if updated.rowcount > 0:
                await db.commit()
                logger.info(
                    f"Signal refresh: upgraded {updated.rowcount} events "
                    f"to signal=1 (threshold lowered after backtest)"
                )
                # Sync backtest_runs.signals_generated from backtest_events
                # so monitoring/display data reflects retroactive updates.
                await db.execute(
                    """UPDATE backtest_runs SET signals_generated = (
                           SELECT COUNT(DISTINCT event_id)
                           FROM backtest_events
                           WHERE backtest_events.run_id = backtest_runs.run_id
                           AND signal_generated = 1
                       )
                       WHERE run_id IN (
                           SELECT DISTINCT run_id FROM backtest_events
                           WHERE signal_generated = 1
                       )"""
                )
                await db.commit()
                # Recalculate full stats for affected runs
                affected_runs = await db.execute(
                    "SELECT DISTINCT run_id FROM backtest_events "
                    "WHERE signal_generated = 1"
                )
                run_ids = [r[0] for r in await affected_runs.fetchall()]
                for rid in run_ids:
                    try:
                        await self.backtest_engine.recalculate_run_stats(rid)
                    except Exception as rc_e:
                        logger.warning(f"Signal refresh: recalculate_run_stats({rid[:8]}) failed: {rc_e}")
    except Exception as e:
        logger.warning(f"Signal refresh failed: {e}")
