"""Claude deep-work ResearchLoop phase, extracted from post_live.

Callers still import this name from tools.loop.phases.post_live / phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays defined in the phases_impl facade (not relocated).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tools.loop import phases_impl as _impl

logger = _impl.logger
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS


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

