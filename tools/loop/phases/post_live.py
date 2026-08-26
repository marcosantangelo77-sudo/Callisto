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
    self = loop
    """
    Karpathy-style: maximize Claude Code throughput.

    Claude is NOT used for generic analysis text. Every call must produce
    ACTIONABLE output: hypotheses to create, hypotheses to reject, or
    specific pipeline fixes. If it can't act, it shouldn't call.

    Cooldown-gated to prevent burst stalls — deep work is valuable but
    firing it 10s after interpret_backtests was causing 5x/day rate limit hits.

    When Claude is unavailable: defers the prompt to work queue AND
    runs local model fallback for basic maintenance (reject zero-signal
    hypotheses, gather pipeline metrics).
    """
    from inference import escalate_with_ladder
    import time as _time

    if not self._claude_ok():
        # Local model deep work via model ladder (Qwen3-14B primary)
        # Much better than rule-based: structured diagnosis, JSON output
        try:
            from inference import escalate_with_ladder
            # Build a condensed pipeline summary for the local model
            db = self.data_collector._db
            bt_total = 0
            bt_signals = 0
            try:
                cursor = await db.execute("SELECT COUNT(*), SUM(CASE WHEN signals_generated > 0 THEN 1 ELSE 0 END) FROM backtest_runs")
                row = await cursor.fetchone()
                bt_total, bt_signals = row[0] or 0, row[1] or 0
            except Exception:
                pass

            local_prompt = (
                f"You are diagnosing a sports betting research pipeline.\n"
                f"Backtest runs: {bt_total}, with signals: {bt_signals}\n"
                f"Signal rate: {bt_signals*100//max(bt_total,1)}%\n"
                f"Respond ONLY with JSON:\n"
                f'{{"pipeline_issues": ["specific problem"], "reject_ids": [], "actions": ["specific fix"]}}'
            )
            result = await escalate_with_ladder(
                local_prompt, task_type="deep_work", timeout=30,
            )
            if result.get("content") and result.get("model_used") != "none":
                logger.info(
                    f"Research: local deep work via {result['model_used']} "
                    f"({len(result['content'])} chars)"
                )
        except Exception as e:
            logger.debug(f"Local model deep work failed: {e}")

        # Also run rule-based fallback for guaranteed maintenance
        try:
            from tools.work_queue import local_fallback_deep_work
            actions = await local_fallback_deep_work(db)
            rejected = 0
            for hid in actions.get("reject_ids", []):
                try:
                    # Fetch current status for CAS — concurrent promoters
                    # can move this row under us.
                    _h = await self.hypothesis_manager.get_hypothesis(hid)
                    _curr = (_h or {}).get("status")
                    if not _curr or _curr in ("rejected", "retired"):
                        continue
                    await self.hypothesis_manager.update_status(
                        hid, "rejected", "local_fallback_deep_work",
                        expected_status=_curr,
                    )
                    rejected += 1
                    self._rejections += 1
                except Exception:
                    pass
            if rejected:
                logger.info(f"Research: local fallback rejected {rejected} hypotheses")
        except Exception as e:
            logger.debug(f"Rule-based fallback failed: {e}")

        _defer_deep_work = True
    else:
        _defer_deep_work = False

    logger.info("Research: Claude deep work phase — actionable output only")

    # Gather pipeline metrics for Claude
    db = self.data_collector._db
    metrics = {}
    try:
        # Backtest signal rate (deduplicated by event_id — each game
        # produces multiple rows across books, COUNT(*) overcounts)
        row = await db.execute_fetchone(
            "SELECT COUNT(DISTINCT event_id) total, "
            "COUNT(DISTINCT CASE WHEN signal_generated=1 THEN event_id END) signals "
            "FROM backtest_events"
        ) if hasattr(db, 'execute_fetchone') else None
        if not row:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT event_id) total, "
                "COUNT(DISTINCT CASE WHEN signal_generated=1 THEN event_id END) signals "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
        metrics["bt_events"] = (row[0] or 0) if row else 0
        metrics["bt_signals"] = (row[1] or 0) if row else 0

        # Avg books in historical odds
        cursor = await db.execute(
            "SELECT sport, COUNT(*) FROM historical_odds_cache GROUP BY sport"
        )
        metrics["odds_cache"] = {r[0]: r[1] for r in await cursor.fetchall()}

        # Hypothesis stats
        cursor = await db.execute(
            "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
        )
        metrics["hypo_status"] = {r[0]: r[1] for r in await cursor.fetchall()}
    except Exception as e:
        logger.warning(f"Failed to gather metrics for deep work: {e}")

    # Get top backtesting hypotheses by UNIQUE signal count
    # (dedup by event_id to match evaluate_significance, which keeps
    # best-edge row per event — raw row counts inflate signal/event totals)
    top_hypos = []
    try:
        cursor = await db.execute("""
            SELECT h.hypothesis_id, h.name, h.thesis, h.sport, h.status,
                   COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as sigs,
                   COUNT(DISTINCT be.event_id) as events,
                   AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge,
                   br.actual_win, br.actual_loss, br.p_value_binomial
            FROM hypotheses h
            LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
            LEFT JOIN backtest_runs br ON br.hypothesis_id = h.hypothesis_id
            WHERE h.status IN ('backtesting', 'paper_trading')
            GROUP BY h.hypothesis_id
            ORDER BY sigs DESC, events DESC
            LIMIT 15
        """)
        for r in await cursor.fetchall():
            w = r[8] or 0
            l = r[9] or 0
            p = r[10] or 1.0
            wr = w / (w + l) * 100 if (w + l) > 0 else 0
            status_tag = f"[{r[4]}]" if r[4] == "paper_trading" else ""
            top_hypos.append(
                f"  {r[1]} [{r[3]}]{status_tag}: {r[5]} signals / {r[6]} events, "
                f"avg_edge={r[7] or 0:.4f}, {w}W-{l}L ({wr:.0f}%), p={p:.4f}"
            )
    except Exception as e:
        logger.warning(f"Failed to query top hypotheses for deep work prompt: {e}")

    # Self-scrutiny: check if hypotheses are testing the same games
    scrutiny_info = ""
    try:
        cursor = await db.execute("""
            SELECT h.hypothesis_id, h.name,
                   COUNT(DISTINCT be.event_id) as unique_events,
                   COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as unique_signals
            FROM hypotheses h
            JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
            WHERE h.status IN ('backtesting', 'paper_trading')
            GROUP BY h.hypothesis_id
            HAVING unique_events > 0
            ORDER BY unique_events DESC
            LIMIT 10
        """)
        game_sets = []
        for r in await cursor.fetchall():
            game_sets.append(f"  {r[1]}: {r[2]} unique events, {r[3]} signals")

        # Check for duplicate game sets (different hypotheses testing identical events)
        cursor2 = await db.execute("""
            SELECT GROUP_CONCAT(DISTINCT h.name) as hypo_names,
                   COUNT(DISTINCT be.event_id) as unique_events,
                   COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as unique_signals
            FROM hypotheses h
            JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
            WHERE h.status IN ('backtesting', 'paper_trading')
            GROUP BY h.hypothesis_id
            HAVING unique_events > 10
        """)
        event_counts = {}
        for r in await cursor2.fetchall():
            key = f"{r[1]}e_{r[2]}s"
            if key not in event_counts:
                event_counts[key] = []
            event_counts[key].append(r[0])
        duplicates = {k: v for k, v in event_counts.items() if len(v) > 1}

        if game_sets or duplicates:
            scrutiny_info = "\nBACKTEST SCRUTINY:\n"
            if game_sets:
                scrutiny_info += "  Event counts per hypothesis:\n" + "\n".join(game_sets) + "\n"
            if duplicates:
                scrutiny_info += (
                    "  WARNING: These hypotheses tested IDENTICAL game sets "
                    "(same unique_games and total_events):\n"
                )
                for k, names in duplicates.items():
                    scrutiny_info += f"    {k}: {', '.join(names)}\n"
                scrutiny_info += (
                    "  This suggests backtests are NOT filtering for hypothesis-specific conditions.\n"
                    "  Different hypotheses should produce DIFFERENT event sets.\n"
                )
    except Exception as e:
        logger.warning(f"Failed to gather scrutiny metrics: {e}")

    prompt = (
        f"CALLISTO AUTONOMOUS SYSTEM — DEEP WORK CYCLE #{self._cycles}\n"
        f"You are a critical analyst auditing an autonomous research pipeline.\n"
        f"Your primary obligation is honesty about what is working and what is broken.\n"
        f"pipeline_issues takes PRIORITY over new_hypotheses — fix the funnel before "
        f"pouring more hypotheses into it.\n\n"
        f"PIPELINE STATE:\n"
        f"  Backtest events: {metrics.get('bt_events') or 0} total, "
        f"{metrics.get('bt_signals') or 0} signals ({((metrics.get('bt_signals') or 0) / max(1,(metrics.get('bt_events') or 1))) * 100:.1f}%)\n"
        f"  Hypothesis status: {json.dumps(metrics.get('hypo_status', {}))}\n"
        f"  Odds cache: {json.dumps(metrics.get('odds_cache', {}))}\n"
        f"  Promotions: {self._promotions} | Rejections: {self._rejections}\n"
        f"  Cycles: {self._cycles} | Data collections: {self._data_collections}\n\n"
        f"CRITICAL QUESTIONS (answer these honestly before generating anything):\n"
        f"  1. What is the promotion rate? If 0%, why — bad hypotheses or broken pipeline?\n"
        f"  2. Signal rate {((metrics.get('bt_signals') or 0) / max(1,(metrics.get('bt_events') or 1))) * 100:.1f}% — "
        f"is this because edges don't exist, or because devig uses too few books?\n"
        f"  3. Are hypotheses getting a fair trial, or dying before results are collected?\n\n"
        f"ALL SPORTS RESEARCHED EQUALLY: {RESEARCH_SPORTS}\n\n"
        f"TOP HYPOTHESES BY SIGNALS:\n"
        + ("\n".join(top_hypos) if top_hypos else "  (none with signals)") + "\n\n"
        f"RESPOND WITH EXACTLY THIS JSON STRUCTURE (no other text):\n"
        f'{{"pipeline_issues": ["MOST IMPORTANT — specific, actionable problems"], '
        f'"reject_ids": ["hypothesis_id1", ...], '
        f'"promising_sports": ["sport1", ...], '
        f'"new_hypotheses": [{{"name": "...", "thesis": "...", "sport": "...", '
        f'"market_type": "...", "edge_threshold": 0.015}}]}}\n\n'
        f"{scrutiny_info}\n"
        f"RULES:\n"
        f"- pipeline_issues FIRST: what is structurally preventing any hypothesis from succeeding?\n"
        f"  - If multiple hypotheses tested the EXACT SAME events, that is a filtering bug\n"
        f"  - If 0 promotions after {self._cycles} cycles, diagnose the bottleneck explicitly\n"
        f"  - If devig uses <3 books on most events, the edge detection is unreliable\n"
        f"- reject_ids: ONLY hypotheses with 50+ events, 0 signals, AND adequate data quality\n"
        f"- new_hypotheses: 3-5 NOVEL, testable — but ONLY if the pipeline can actually test them.\n"
        f"  If the funnel is broken, say so and generate 0.\n"
        f"  BANNED TOPICS: generic rest/B2B, home underdog ATS, eliminated team fades, basic weather.\n"
        f"  Vegas already prices these. Focus on dimensions models DON'T have columns for:\n"
        f"  team identity/cohesion, roster sociology, ref biases, scheme geometry,\n"
        f"  SGP correlation mispricing, media narrative inflation, calendar quirks.\n"
    )

    if _defer_deep_work:
        await self._work_queue.enqueue("deep_work", prompt, priority=3)
        self._downtime_tracker.item_queued()
        logger.info("Research: deep work prompt deferred to work queue (Claude unavailable)")
        return

    remaining = CLAUDE_ESCALATION_COOLDOWN - (_time.time() - self._last_claude_call)
    if remaining > 0:
        logger.debug(f"Deep work: cooldown active ({remaining:.0f}s left), deferring to next cycle")
        return

    try:
        result = await escalate_with_ladder(
            prompt,
            task_type="deep_work",
            hermes_caller="deep_work",
        )
        self._last_claude_call = _time.time()
        self._claude_escalations += 1

        if result.get("content") and not result.get("error"):
            content = result["content"]
            logger.info(f"Research: Claude deep work response — {len(content)} chars")

            # Write learnings back to Hermes from the deep work output
            try:
                from tools.hermes_memory import get_hermes_memory
                hermes = get_hermes_memory()
                # Store a summary learning from this deep work cycle
                await hermes.record_learning(
                    key=f"deep_work_cycle_{self._cycles}",
                    value=content[:500],
                    confidence=0.6,
                    source="deep_work",
                )
            except Exception as e:
                logger.debug(f"Failed to record deep work learning: {e}")

            # Parse and ACT on the structured response
            try:
                # Extract JSON from response (may have markdown fences)
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

                actions = json.loads(json_str)

                # Act 1: Reject hopeless hypotheses
                rejected = 0
                for hid in actions.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "claude_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception as e:
                        logger.warning(f"Failed to reject hypothesis {hid} in deep work: {e}")
                if rejected:
                    logger.info(f"Research: Claude rejected {rejected} hopeless hypotheses")

                # Act 2: Create new hypotheses
                created = 0
                for nh in actions.get("new_hypotheses", []):
                    try:
                        _dw_config = {
                                "source": "claude_deep_work",
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
                            _dw_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _dw_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", "claude_generated"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_dw_config,
                        )
                        created += 1
                    except Exception as e:
                        logger.warning(f"Failed to create hypothesis '{nh.get('name', '?')}' in deep work: {e}")
                if created:
                    self._hypotheses_generated += created
                    logger.info(f"Research: Claude created {created} new hypotheses")

                # Act 3: Convert pipeline issues into structured findings
                # and route them to self-repair for automatic fixes
                pipeline_issues = actions.get("pipeline_issues", [])
                if pipeline_issues:
                    findings = []
                    for issue in pipeline_issues:
                        logger.warning(f"Research: Claude identified issue — {issue}")
                        # Classify severity based on keywords
                        issue_lower = issue.lower() if isinstance(issue, str) else ""
                        if any(kw in issue_lower for kw in ["identical", "same games", "filtering bug", "broken"]):
                            severity = "CRITICAL"
                        elif any(kw in issue_lower for kw in ["prioritize", "threshold", "zero promotion", "low sample"]):
                            severity = "HIGH"
                        else:
                            severity = "LOW"
                        findings.append({"severity": severity, "description": issue})

                    # Route to self-repair engine
                    try:
                        from tools.self_repair import get_repair_engine
                        engine = get_repair_engine()
                        repair_results = await engine.handle_claude_findings(findings)
                        for r in repair_results:
                            if r["fixed"]:
                                logger.info(f"Deep work auto-fix: {r['action']} — {r['detail']}")
                            else:
                                logger.warning(f"Deep work unfixed: {r['action']} — {r['detail']}")
                    except Exception as e:
                        logger.warning(f"Failed to route findings to self-repair: {e}")

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Claude deep work returned non-JSON: {e}")
                # Still store the raw analysis as fallback
                try:
                    await db.execute(
                        "INSERT INTO game_contexts "
                        "(sport, game_date, home_team, away_team, context_json, embedded) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (
                            "meta_research",
                            _time.strftime("%Y-%m-%d"),
                            "callisto", "self_analysis",
                            json.dumps({
                                "type": "claude_deep_analysis",
                                "cycle": self._cycles,
                                "raw": content[:5000],
                            }),
                        ),
                    )
                    await db.commit()
                except Exception as e:
                    logger.warning(f"Failed to store raw deep analysis fallback: {e}")

        elif result.get("rate_limited"):
            logger.info("Research: Claude rate limited — will retry next cycle")
    except Exception as e:
        logger.warning(f"Claude deep work failed: {e}", exc_info=True)


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
