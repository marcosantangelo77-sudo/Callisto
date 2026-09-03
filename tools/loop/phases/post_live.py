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
    self = loop
    focus_sports = RESEARCH_SPORTS
    """Granger temporal prediction phase — identify which books lead each sport.

    Runs weekly (every ~100 cycles at 1-min intervals). Checks the most
    recent computed_at timestamp in granger_results and skips if the last
    analysis is less than 7 days old.

    Results feed into edge_scanner's dynamic sharp book classification:
    when a book is identified as the temporal leader for a sport, it is
    added to the sharp set for edge detection in that sport.
    """
    import aiosqlite
    from tools.granger_causality import analyze_book_leadership, store_results

    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

    # Check if we ran recently (within 7 days) — skip if so
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cursor = await db.execute(
                "SELECT MAX(computed_at) FROM granger_results"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                last_computed = datetime.fromisoformat(row[0])
                age_days = (datetime.now(timezone.utc) - last_computed).total_seconds() / 86400
                if age_days < 7:
                    logger.debug(
                        f"Granger analysis: last run {age_days:.1f} days ago, "
                        f"skipping (< 7 days)"
                    )
                    return
    except Exception as e:
        # Table might not exist yet or be empty — proceed with analysis
        logger.debug(f"Granger recency check failed (will run analysis): {e}")

    # Run analysis for all sports
    total_stored = 0
    for sport in RESEARCH_SPORTS:
        if not self._running:
            break
        try:
            results = await analyze_book_leadership(db_path, sport)
            leader = results.get("leader_book")
            score = results.get("leader_score", 0)
            n_pairs = results.get("n_pairs_tested", 0)

            if results.get("warning"):
                logger.info(
                    f"Granger {sport}: {results['warning']}"
                )
                continue

            if leader:
                logger.info(
                    f"Granger {sport}: leader={leader} "
                    f"(score={score:.3f}, pairs={n_pairs}, "
                    f"books={results.get('books_tested', [])})"
                )
            else:
                logger.info(
                    f"Granger {sport}: no clear leader "
                    f"(pairs={n_pairs}, books={results.get('books_tested', [])})"
                )

            stored = await store_results(db_path, results)
            total_stored += stored

            # Update edge_scanner's cache immediately
            if leader:
                from tools.edge_scanner import _granger_sharp_cache
                _granger_sharp_cache[sport] = (leader, time.time())

        except Exception as e:
            logger.warning(f"Granger analysis failed for {sport}: {e}")

    if total_stored:
        logger.info(
            f"Granger phase complete: {total_stored} results stored "
            f"across {len(focus_sports)} sports"
        )

    # Record phase success for pipeline integrity tracking
    try:
        from tools.pipeline_integrity import get_checker
        get_checker().record_phase_result("granger_analysis", True)
    except Exception:
        pass


async def phase_regime_analysis(loop) -> None:
    self = loop
    """Regime analysis phase — detect regime changes, recency bias, mean reversion.

    Runs every REGIME_ANALYSIS_INTERVAL cycles (regime changes are slow —
    no point re-analyzing every minute). Results are cached in
    _regime_cache and fed into:
      1. Edge confidence scoring (via regime_data parameter)
      2. Hypothesis generation (regime context in Claude prompt)
      3. Edge candidate enrichment (regime signals on candidates)

    Uses ESPN box score data already collected by data_collector to build
    per-team performance histories, then runs full_regime_analysis() from
    tools/regime.py.
    """
    if self._cycles % REGIME_ANALYSIS_INTERVAL != 0:
        return

    from tools.regime import full_regime_analysis

    logger.info("Research: running regime analysis phase")

    # Map sport keys to the short sport name used by regime.py
    sport_short_map = {
        "basketball_nba": "nba",
        "americanfootball_nfl": "nfl",
        "icehockey_nhl": "nhl",
        "baseball_mlb": "mlb",
        "basketball_ncaab": "ncaab",
        "basketball_ncaaw": "ncaaw",
        "basketball_wnba": "wnba",
    }

    db = self.data_collector._db
    if db is None:
        logger.warning("Regime analysis: data_collector DB not initialized")
        return

    total_analyzed = 0
    total_signals = 0

    for sport in RESEARCH_SPORTS:
        if not self._running:
            break

        sport_short = sport_short_map.get(sport, "nba")

        try:
            # Query box score data for team performance histories.
            # box_scores table has: sport, game_date, team_name, points,
            # opponent_points, plus advanced stats when available.
            cursor = await db.execute(
                "SELECT team_name, points FROM box_scores "
                "WHERE sport = ? AND points IS NOT NULL "
                "ORDER BY game_date ASC",
                (sport,),
            )
            rows = await cursor.fetchall()

            if not rows:
                logger.debug(f"Regime analysis: no box score data for {sport}")
                continue

            # Group by team
            from collections import defaultdict
            team_histories: dict[str, list[float]] = defaultdict(list)
            for team_name, points in rows:
                if team_name and points is not None:
                    team_histories[team_name].append(float(points))

            if not team_histories:
                continue

            # Compute league average for this sport
            all_points = []
            for pts_list in team_histories.values():
                all_points.extend(pts_list)
            league_avg = sum(all_points) / len(all_points) if all_points else 100.0

            # Run regime analysis for each team with enough data
            for team_name, history in team_histories.items():
                if len(history) < 8:
                    continue  # Need minimum data for meaningful analysis

                try:
                    team_data = {
                        "name": team_name,
                        "performance_history": history,
                        "league_avg": league_avg,
                    }
                    result = full_regime_analysis(team_data, sport=sport_short)

                    # Cache the result keyed by team name
                    cache_key = f"{sport}:{team_name}"
                    _regime_cache[cache_key] = result
                    total_analyzed += 1

                    if result.get("has_edge_signal"):
                        total_signals += 1
                        logger.info(
                            f"Regime signal: {team_name} ({sport_short}) — "
                            f"{result.get('actionable_signals', [])}"
                        )
                except Exception as e:
                    logger.debug(f"Regime analysis failed for {team_name}: {e}")

        except Exception as e:
            logger.warning(f"Regime analysis failed for {sport}: {e}")

    self._last_regime_analysis = time.time()

    if total_analyzed > 0:
        logger.info(
            f"Regime analysis complete: {total_analyzed} teams analyzed, "
            f"{total_signals} with actionable signals, "
            f"cache size: {len(_regime_cache)}"
        )

    # Record phase success for pipeline integrity tracking
    try:
        from tools.pipeline_integrity import get_checker
        get_checker().record_phase_result("regime_analysis", True)
    except Exception:
        pass


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
