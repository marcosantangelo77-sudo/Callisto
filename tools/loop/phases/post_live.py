"""Post-live_execute ResearchLoop phases, extracted from phases_impl.

Kept out of phases_impl so the live-execute env gate stays in the facade
module. Callers still import these names from tools.loop.phases_impl.

This module must never import tools.autonomous (circular).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from tools import telegram
from tools.loop import phases_impl as _impl

logger = logging.getLogger("callisto.autonomous")

# Shared cadence / wiki / regime state — defined on phases_impl before this
# module is imported (late import at the bottom of phases_impl).
_regime_cache = _impl._regime_cache
REGIME_ANALYSIS_INTERVAL = _impl.REGIME_ANALYSIS_INTERVAL
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS
SYSTEM_IMPROVEMENT_INTERVAL = _impl.SYSTEM_IMPROVEMENT_INTERVAL
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS

async def phase_review_live(loop) -> None:
    from tools.loop.phases.post_live_review import phase_review_live as _impl_fn
    return await _impl_fn(loop)


async def phase_narrative_edges(loop) -> None:
    from tools.loop.phases.post_live_review import phase_narrative_edges as _impl_fn
    return await _impl_fn(loop)


async def phase_claude_deep_work(loop) -> None:
    from tools.loop.phases.claude_deep import phase_claude_deep_work as _impl_fn
    return await _impl_fn(loop)


async def phase_granger_analysis(loop) -> None:
    from tools.loop.phases.regime_granger import phase_granger_analysis as _impl_fn
    return await _impl_fn(loop)


async def phase_regime_analysis(loop) -> None:
    from tools.loop.phases.regime_granger import phase_regime_analysis as _impl_fn
    return await _impl_fn(loop)


async def phase_knowledge_compile(loop) -> None:
    from tools.loop.phases.post_live_wiki import phase_knowledge_compile as _impl_fn
    return await _impl_fn(loop)


async def phase_knowledge_lint(loop) -> None:
    from tools.loop.phases.post_live_wiki import phase_knowledge_lint as _impl_fn
    return await _impl_fn(loop)


async def phase_system_improvement(loop) -> None:
    from tools.loop.phases.system_improve import phase_system_improvement as _impl_fn
    return await _impl_fn(loop)


async def phase_system_watchdog(loop) -> None:
    self = loop
    """Hermes-powered system watchdog — detects orphaned features and stale pipelines.

    Runs every 10 cycles. Checks that committed features actually produce data,
    hot tables are receiving writes, and enrichment coverage is adequate.
    Records findings to Hermes for cross-session awareness and escalates
    critical issues to Claude deep work.
    """
    from tools.hermes_memory import get_hermes_memory

    db = self.hypothesis_manager._db
    if not db:
        return

    findings = []

    try:
        # 1. Orphaned table detection — tables that SHOULD have data but don't
        orphan_checks = {
            "clv_log": ("bets WHERE result != 'pending'", "Resolved bets exist but CLV not logged"),
            "paper_trades": ("hypotheses WHERE status = 'paper_trading'", "Paper trading hypotheses exist but 0 trades"),
            "market_microstructure": ("odds_snapshots", "Snapshots exist but microstructure never computed"),
            "closing_lines": ("odds_snapshots", "Snapshots exist but closing lines never captured"),
        }
        from tools.db_utils import safe_ident
        # source_query is a literal SQL fragment (e.g., "bets WHERE result != 'pending'");
        # validate only its leading identifier. The remainder is hard-coded above and
        # is NOT user-derived — but reject any source_query whose first token isn't a
        # plain table name to defeat future careless edits.
        for target, (source_query, msg) in orphan_checks.items():
            try:
                safe_ident(target)
                first_tok = source_query.split()[0] if source_query else ""
                safe_ident(first_tok)
                target_cnt = (await (await db.execute(f"SELECT COUNT(*) FROM {target}")).fetchone())[0]
                source_cnt = (await (await db.execute(f"SELECT COUNT(*) FROM {source_query}")).fetchone())[0]
                if target_cnt == 0 and source_cnt > 0:
                    findings.append(f"ORPHAN: {target} empty — {msg}")
            except Exception:
                pass

        # 2. Feature coverage audit — enrichment rate
        try:
            total = (await (await db.execute(
                "SELECT COUNT(*) FROM game_contexts WHERE game_date >= date('now', '-7 days')"
            )).fetchone())[0]
            enriched = (await (await db.execute(
                "SELECT COUNT(*) FROM game_contexts "
                "WHERE game_date >= date('now', '-7 days') "
                "AND context_json LIKE '%rest_days%'"
            )).fetchone())[0]
            if total > 10 and enriched / total < 0.5:
                findings.append(
                    f"COVERAGE: Only {enriched}/{total} ({enriched/total:.0%}) "
                    f"recent games enriched with rest_days"
                )
        except Exception:
            pass

        # 3. Signal quality audit — check for phantom signals
        try:
            phantom = (await (await db.execute(
                "SELECT COUNT(*) FROM signals WHERE edge_pct > 0.20"
            )).fetchone())[0]
            if phantom > 0:
                findings.append(f"PHANTOM: {phantom} signals with >20% edge still in DB")
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"System watchdog error: {e}")

    if findings:
        logger.warning(
            f"System watchdog ({len(findings)} findings):\n"
            + "\n".join(f"  - {f}" for f in findings)
        )
        # Record to Hermes
        try:
            hm = await get_hermes_memory()
            if hm:
                await hm.record_learning(
                    key="system_watchdog_findings",
                    value="; ".join(findings),
                    confidence=0.9,
                    source="system_watchdog",
                )
        except Exception:
            pass
    else:
        logger.info("System watchdog: all checks passed")


async def phase_integrity_check(loop) -> None:
    self = loop
    """
    Pipeline integrity check — detects silent failures that the
    standard health check misses.

    Runs every INTEGRITY_CHECK_INTERVAL_CYCLES cycles. Checks that
    pipelines are not just running but producing valid, changing,
    non-zero output.
    """
    from tools.pipeline_integrity import get_checker, INTEGRITY_CHECK_INTERVAL_CYCLES

    # Always run on cycle 1 (first cycle), then every N cycles
    if self._cycles > 1 and self._cycles % INTEGRITY_CHECK_INTERVAL_CYCLES != 0:
        return

    checker = get_checker()

    try:
        result = await checker.run_all_checks()

        # If critical issues found, alert via Telegram
        critical = result.get("issues", {}).get("critical", 0)
        if critical > 0:
            issue_details = result.get("issue_details", [])
            critical_msgs = [
                i["message"] for i in issue_details
                if i.get("severity") == "CRITICAL"
            ]
            alert_text = (
                f"PIPELINE INTEGRITY ALERT: {critical} critical issues\n\n"
                + "\n\n".join(f"- {m}" for m in critical_msgs[:3])
            )
            try:
                await telegram.alert_system(alert_text)
            except Exception as e:
                logger.warning(f"Failed to send integrity alert via Telegram: {e}", exc_info=True)

        # Also add phase error rate issues
        phase_issues = checker.check_phase_error_rates()
        if phase_issues:
            logger.warning(
                f"PIPELINE INTEGRITY: {len(phase_issues)} phases with high error rates"
            )

    except Exception as e:
        logger.error(f"Pipeline integrity check failed: {e}", exc_info=True)
