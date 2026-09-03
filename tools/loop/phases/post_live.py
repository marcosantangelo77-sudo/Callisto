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
_wiki_in_loop_enabled = _impl._wiki_in_loop_enabled
_fetch_wiki_priors = _impl._fetch_wiki_priors
_regime_cache = _impl._regime_cache
REGIME_ANALYSIS_INTERVAL = _impl.REGIME_ANALYSIS_INTERVAL
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS
SYSTEM_IMPROVEMENT_INTERVAL = _impl.SYSTEM_IMPROVEMENT_INTERVAL
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS

async def phase_review_live(loop) -> None:
    self = loop
    """Review LIVE hypotheses for underperformance; demote to 'paused'.

    Audit 2026-04-21: Callisto previously had NO demotion path from LIVE.
    Losing hypotheses stayed live until manually retired. This phase runs
    the rolling-window review and calls `review_live_hypotheses()` to
    quantify performance and demote underperformers.

    Cycle-gated: runs once every 4 hours worth of cycles (~every 24 cycles
    at a 10-minute cadence) to avoid thrashing on noisy short windows.
    Can be overridden by setting `_force_live_review` on the instance.
    """
    # Cycle gate: approx every 4 hours. A typical cycle is ~10 minutes, so
    # every 24 cycles ≈ 4 hours. Use modulo so the schedule drifts with
    # actual cycle cadence.
    review_interval = int(os.getenv("CALLISTO_LIVE_REVIEW_EVERY_N_CYCLES", "24"))
    if not getattr(self, "_force_live_review", False):
        if self._cycles == 0 or (self._cycles % max(1, review_interval)) != 0:
            return

    try:
        results = await self.hypothesis_manager.review_live_hypotheses()
    except Exception as e:
        logger.warning(f"_phase_review_live: review failed: {e}")
        return

    if not results:
        return

    demoted = [r for r in results if r.get("demoted")]
    held = [r for r in results if not r.get("demoted")]
    logger.info(
        f"_phase_review_live: reviewed {len(results)} LIVE hypotheses — "
        f"demoted {len(demoted)}, held {len(held)}"
    )

    # ── Wiki consult: surface prior warnings & recovery-cycle precedents ──
    # (feat/wiki-in-the-loop 2026-04-22) — before logging the demotion,
    # query the wiki for matching failure modes so the decision trail
    # includes cites, not just raw stats.
    db = self.data_collector._db if self.data_collector else None
    for r in demoted:
        name = r.get("name") or r["hypothesis_id"]
        wiki_cites = []
        if db and _wiki_in_loop_enabled():
            try:
                prior = await _fetch_wiki_priors(
                    db, f"{name} failure mode underperformance demotion",
                    top_k=3,
                )
                wiki_cites = [
                    f"{a.get('topic')}(sim={a.get('similarity')})"
                    for a in prior if a.get("similarity") is not None
                ]
            except Exception as e:
                logger.debug(f"Wiki demotion prior-fetch failed: {e}")
        r["wiki_cites"] = wiki_cites
        logger.warning(
            f"LIVE DEMOTION: {name} → paused. "
            f"n={r['n_resolved']} hit={r['hit_rate']:.1%} "
            f"roi={r['roi']:.2%} mdd={r['max_drawdown']:.1%} "
            f"clv={r['avg_clv']} reasons={r['reasons']} "
            f"wiki_cites={wiki_cites}"
        )

    # For held hypotheses: consult wiki for historical demote→recover
    # cycles on similar cohorts. Informational only — not a gate.
    if db and _wiki_in_loop_enabled():
        for r in held:
            try:
                name = r.get("name") or r["hypothesis_id"]
                recov = await _fetch_wiki_priors(
                    db, f"{name} demote recover cycle rebound",
                    top_k=2,
                )
                if recov:
                    logger.debug(
                        f"_phase_review_live: held {name} — wiki recovery "
                        f"precedents: {[a.get('topic') for a in recov]}"
                    )
            except Exception:
                pass


async def phase_narrative_edges(loop) -> None:
    self = loop
    """Detect player-level narrative edges that models can't price.

    Runs every cycle to find: usage surges, role changes, milestone
    proximity, revenge games. Logs actionable findings and stores
    them for the deep_work phase to incorporate into analysis.
    """
    try:
        from tools.narrative_edge import full_narrative_scan
    except ImportError as e:
        logger.debug(f"Narrative edge module not available: {e}")
        return

    # Run for active sports with player data
    for sport in ["basketball_nba", "icehockey_nhl", "baseball_mlb"]:
        try:
            results = await full_narrative_scan(sport)

            # Log actionable edges
            actionable = [
                e for e in results.get("usage_surges", [])
                if e.get("actionable")
            ]
            if actionable:
                for edge in actionable[:5]:
                    logger.info(
                        f"NARRATIVE EDGE: {edge['player']} {edge['stat_type']} "
                        f"surge {edge['surge_ratio']:.2f}x "
                        f"(recent {edge['recent_avg']:.1f} vs season {edge['season_avg']:.1f}) "
                        f"| line={edge.get('current_line','?')} gap={edge.get('line_gap','?')} "
                        f"| {edge.get('book','?')} [{sport}]"
                    )

            role_changes = results.get("role_changes", [])
            if role_changes:
                for rc in role_changes[:3]:
                    logger.info(
                        f"ROLE CHANGE: {rc['player']} "
                        f"+{rc['minute_increase']:.0f} min/game "
                        f"({rc['season_avg_minutes']:.0f} -> {rc['recent_avg_minutes']:.0f}) [{sport}]"
                    )

            milestones = results.get("milestones", [])
            if milestones:
                for m in milestones[:3]:
                    logger.info(
                        f"MILESTONE: {m['player']} "
                        f"{m['edge_type']} — {m.get('note','')} [{sport}]"
                    )

            revenge = results.get("revenge_games", [])
            if revenge:
                for r in revenge[:3]:
                    logger.info(
                        f"REVENGE GAME: {r['player']} "
                        f"(now {r['current_team']}, ex-{', '.join(r['former_teams'])}) [{sport}]"
                    )

        except Exception as e:
            logger.debug(f"Narrative scan failed for {sport}: {e}")


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
    self = loop
    """Knowledge wiki compilation — LLM Wiki pattern (Karpathy).

    Reads recent sessions/evidence/learnings and compiles them into
    persistent, cross-referenced wiki articles. Knowledge compounds
    instead of being re-discovered each time.

    Runs every COMPILE_INTERVAL_CYCLES (7) — coprime with lint (11).
    Uses Gemma 4 (local, free) for compilation.
    """
    from tools.knowledge_wiki import get_wiki, COMPILE_INTERVAL_CYCLES

    if self._cycles % COMPILE_INTERVAL_CYCLES != 0:
        return

    db = self.data_collector._db
    if not db:
        return

    try:
        wiki = get_wiki()
        stats = await wiki.compile(db, self._cycles)
        created = stats.get("articles_created", 0)
        updated = stats.get("articles_updated", 0)
        if created or updated:
            logger.info(
                f"Wiki compile: {created} new articles, {updated} updated"
            )
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_compile", True)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Knowledge compile phase failed: {e}")
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_compile", False)
        except Exception:
            pass


async def phase_knowledge_lint(loop) -> None:
    self = loop
    """Knowledge wiki lint — detect contradictions, stale claims, orphans.

    Scans wiki articles for:
      - Contradictions: conflicting claims between articles
      - Stale articles: not updated in >72 hours
      - Orphans: articles with no cross-references

    Runs every LINT_INTERVAL_CYCLES (11) — coprime with compile (7).
    Uses Qwen 3.5 4B (ultra-fast classifier) for contradiction detection.
    """
    from tools.knowledge_wiki import get_wiki, LINT_INTERVAL_CYCLES

    if self._cycles % LINT_INTERVAL_CYCLES != 0:
        return

    db = self.data_collector._db
    if not db:
        return

    try:
        wiki = get_wiki()
        stats = await wiki.lint(db, self._cycles)

        # Alert on high-severity contradictions
        contradictions = stats.get("contradictions_found", 0)
        if contradictions > 0:
            try:
                from tools import telegram
                await telegram.alert_system(
                    f"Wiki lint: {contradictions} contradictions detected. "
                    f"Stale: {stats.get('stale_articles', 0)}, "
                    f"Orphans: {stats.get('orphan_articles', 0)}"
                )
            except Exception:
                pass

        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_lint", True)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Knowledge lint phase failed: {e}")
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_lint", False)
        except Exception:
            pass


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
