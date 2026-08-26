"""Hypothesis-generation ResearchLoop phases, extracted from phases_impl.

phase_generate_hypotheses and phase_injury_prop_hypotheses. Callers still
import these names from tools.loop.phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays in phases_impl with CALLISTO_ALLOW_LIVE_EXECUTE.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from tools.loop import phases_impl as _impl

logger = _impl.logger

HYPOTHESIS_GEN_INTERVAL = _impl.HYPOTHESIS_GEN_INTERVAL
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS
_regime_cache = _impl._regime_cache
_wiki_in_loop_enabled = _impl._wiki_in_loop_enabled
_fetch_wiki_priors = _impl._fetch_wiki_priors


async def phase_generate_hypotheses(loop) -> None:
    self = loop
    """Generate new hypotheses — Claude Code PRIMARY, templates FALLBACK.

    Claude Code is the primary hypothesis generator. Every cycle where
    Claude is available, we ask it to generate hypotheses based on current
    pipeline state, data stats, and what hasn't been tried. Template
    generation is the fallback when Claude is rate-limited.
    """
    now = time.time()
    if now - self._last_hypothesis_gen < HYPOTHESIS_GEN_INTERVAL:
        return

    # When spinning, generate hypotheses biased toward TESTABLE patterns.
    # Previously this disabled generation entirely, creating a permanent
    # deadlock: all existing drafts exhausted → 0 testable → spinning →
    # generation disabled → no new testable drafts → spinning forever.
    spinning_mode = self._spinning_detected
    if spinning_mode:
        logger.info(
            "Research: generating hypotheses in SPINNING RECOVERY mode — "
            "biasing toward pure line-based patterns (no context factors)"
        )
    else:
        logger.info("Research: generating hypotheses (Claude-primary)")
    self._last_hypothesis_gen = now

    total_created = 0
    used_claude = False

    # ── Temporal isolation: compute training cutoff ──
    # All hypotheses generated this cycle train on data before the cutoff.
    # Backtests will only use data AFTER cutoff + gap.
    today = datetime.now(timezone.utc).date()
    training_cutoff = today - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
    training_period_start = "2023-01-01"  # earliest cached data

    # Try to use temporal_analysis module for walk-forward windows
    try:
        from tools.temporal_analysis import get_training_window
        window = get_training_window()
        if window:
            training_period_start = window.get("start", training_period_start)
            training_cutoff = datetime.strptime(
                window.get("end", str(training_cutoff)), "%Y-%m-%d"
            ).date()
            logger.info(f"Research: using temporal_analysis window {training_period_start} to {training_cutoff}")
    except (ImportError, Exception) as e:
        logger.debug(f"Research: temporal_analysis not available, using default window: {e}")

    training_period_end = str(training_cutoff)
    forward_test_start = str(training_cutoff + timedelta(days=BACKTEST_GAP_DAYS))
    logger.info(
        f"Research: temporal isolation — train [{training_period_start} .. {training_period_end}], "
        f"forward-test from {forward_test_start}"
    )

    # ── PRIMARY: hypothesis generation through the ladder ──
    # The ladder picks the best available model for hypothesis_gen
    # (QWEN36 primary, Claude last-resort per MODEL_LADDER).
    from inference import escalate_with_ladder

    if (now - self._last_claude_call > CLAUDE_ESCALATION_COOLDOWN
            and self._claude_ok()):
        try:
            # Gather context for Claude — use lightweight queries instead of loading all rows
            existing_names = list(await self.hypothesis_manager.get_all_names())
            draft_count = await self.hypothesis_manager.count_by_status("draft")
            active_count = await self.hypothesis_manager.count_by_status("backtesting", "paper_trading", "live")
            rejected_count = await self.hypothesis_manager.count_by_status("rejected")

            data_stats = await self.data_collector.get_collection_stats()

            # Get date ranges per sport from DB
            date_ranges = {}
            db = self.data_collector._db
            if db:
                try:
                    cursor = await db.execute(
                        "SELECT sport, MIN(snapshot_date), MAX(snapshot_date), COUNT(*) "
                        "FROM historical_odds_cache GROUP BY sport"
                    )
                    for row in await cursor.fetchall():
                        date_ranges[row[0]] = {
                            "from": row[1], "to": row[2], "records": row[3]
                        }
                except Exception as e:
                    logger.warning(f"Failed to query historical_odds_cache date ranges: {e}")


            # ── Filter sports by data availability ──
            game_counts_by_sport = {}
            if db:
                try:
                    cursor = await db.execute(
                        "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                    )
                    for row in await cursor.fetchall():
                        game_counts_by_sport[row[0]] = row[1]
                except Exception:
                    pass  # Fall back to unfiltered if query fails

            # Gate on BOTH game count AND odds data availability
            sports_with_odds = {s for s, dr in date_ranges.items() if dr.get("records", 0) > 0}
            eligible_sports = [
                s for s in RESEARCH_SPORTS
                if game_counts_by_sport.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                and s in sports_with_odds
            ]
            ineligible_sports = []
            for s in RESEARCH_SPORTS:
                gc = game_counts_by_sport.get(s, 0)
                if gc < MIN_GAMES_FOR_HYPOTHESIS:
                    ineligible_sports.append(f"{s} ({gc} games)")
                elif s not in sports_with_odds:
                    ineligible_sports.append(f"{s} ({gc} games, NO odds data)")
            if ineligible_sports:
                logger.info(
                    f"Research: sports excluded from hypothesis gen "
                    f"(need >={MIN_GAMES_FOR_HYPOTHESIS} games AND odds data): {ineligible_sports}"
                )

            # Build regime analysis context — highlight teams with actionable signals
            regime_context = ""
            if _regime_cache:
                regime_lines = []
                for cache_key, regime in _regime_cache.items():
                    if regime.get("has_edge_signal"):
                        signals = regime.get("actionable_signals", [])
                        team = regime.get("team", cache_key)
                        pr = regime.get("power_rating", {})
                        regime_label = pr.get("regime", "stable") if isinstance(pr, dict) else "stable"
                        recency = regime.get("recency_bias", {})
                        bias_dir = recency.get("bias_direction", "neutral") if isinstance(recency, dict) else "neutral"
                        bias_mag = recency.get("bias_magnitude", 0) if isinstance(recency, dict) else 0
                        mr = regime.get("mean_reversion", {})
                        mr_expected = mr.get("reversion_expected", False) if isinstance(mr, dict) else False
                        mr_z = mr.get("current_zscore", 0) if isinstance(mr, dict) else 0
                        regime_lines.append(
                            f"  {team}: regime={regime_label}, "
                            f"bias={bias_dir}({bias_mag:.2f}), "
                            f"mean_reversion={'yes' if mr_expected else 'no'}(z={mr_z:.1f}), "
                            f"signals={signals}"
                        )
                if regime_lines:
                    regime_context = (
                        "REGIME ANALYSIS (teams with actionable signals — "
                        "prioritize hypotheses around these):\n"
                        + "\n".join(regime_lines[:20]) + "\n\n"
                    )

            # Build correlation context — strongest market pairs per focus sport
            correlation_context = ""
            try:
                from tools.correlation import list_correlated_markets
                corr_lines = []
                sports_to_check = RESEARCH_SPORTS[:4]
                key_markets = [
                    "team_total", "game_total", "team_spread", "player_points",
                ]
                for fs in sports_to_check:
                    sport_pairs = []
                    for km in key_markets:
                        related = list_correlated_markets(km, fs, min_abs_rho=0.35)
                        for r in related[:3]:
                            pair_str = (
                                f"{km}<->{r['market']}"
                                f"(rho={r['correlation']:.2f})"
                            )
                            if pair_str not in sport_pairs:
                                sport_pairs.append(pair_str)
                    if sport_pairs:
                        corr_lines.append(
                            f"  {fs}: {', '.join(sport_pairs[:6])}"
                        )
                if corr_lines:
                    correlation_context = (
                        "CROSS-MARKET CORRELATIONS (strongest pairs — "
                        "use for SGP/parlay hypotheses):\n"
                        + "\n".join(corr_lines) + "\n\n"
                    )
            except Exception as e:
                logger.debug(f"Correlation context generation failed: {e}")

            spinning_preamble = ""
            if spinning_mode:
                spinning_preamble = (
                    "** SPINNING RECOVERY MODE **\n"
                    "The research loop has been spinning with 0 progress. "
                    "ALL existing 2000+ drafts are untestable — they require context "
                    "factors (weather, pitcher, travel, venue, etc.) that can't be filtered.\n"
                    "You MUST generate hypotheses that are PURELY LINE-BASED — "
                    "using ONLY game_filters and line_filters from the AVAILABLE FILTERS list below.\n"
                    "DO NOT reference weather, pitchers, venue type, travel, altitude, "
                    "bullpen, spring training, roster changes, or any factor not in the filters list.\n"
                    "Focus on: schedule spots (B2B, rest mismatch, road streaks), "
                    "win% ranges, spread ranges, underdog/favorite dynamics, "
                    "and cross-book consensus divergence.\n\n"
                )

            # ── Wiki-in-the-loop: prior-knowledge injection ──
            # Pull relevant articles from the knowledge wiki AND the most
            # recent REJECTED hypotheses in the same cohort. Replaces the
            # old HARDCODED banned list — the banned list below is now
            # generated from live wiki + DB state instead of being frozen
            # in source. (feat/wiki-in-the-loop, 2026-04-22)
            wiki_priors_block = ""
            dynamic_banned_lines: list[str] = []
            if db and _wiki_in_loop_enabled():
                try:
                    # Top-10 wiki articles across the eligible sports, weighted
                    # toward SIGNAL domain (demotion lessons, backtest nulls).
                    priors_query = (
                        f"hypothesis generation priors for sports "
                        f"{eligible_sports} — dead patterns, demotion lessons, "
                        f"null backtests"
                    )
                    prior_articles = await _fetch_wiki_priors(
                        db, priors_query, top_k=10,
                    )
                    wiki_priors_block = _render_wiki_priors_block(
                        prior_articles, max_chars_per=360
                    )
                    # Build banned-list from wiki topics that look like null
                    # results or demotion lessons.
                    for a in prior_articles:
                        topic = a.get("topic", "")
                        if (
                            "null_result" in topic
                            or "live_demotion" in topic
                            or "dead" in topic.lower()
                        ):
                            title = a.get("title", topic)
                            dynamic_banned_lines.append(
                                f"  - {title} (wiki:{topic})"
                            )
                except Exception as e:
                    logger.debug(f"Wiki priors fetch failed (non-fatal): {e}")

                # Last 20 REJECTED hypotheses in similar cohort — negative
                # examples so the generator doesn't re-propose them.
                try:
                    cursor = await db.execute(
                        "SELECT name, thesis, sport, market_type "
                        "FROM hypotheses WHERE status = 'rejected' "
                        "ORDER BY COALESCE(updated_at, created_at) DESC "
                        "LIMIT 20"
                    )
                    rejected_rows = await cursor.fetchall()
                    if rejected_rows:
                        dynamic_banned_lines.append(
                            "  (recently-rejected in same pipeline — "
                            "don't resubmit structurally identical variants)"
                        )
                        for r in rejected_rows[:20]:
                            dynamic_banned_lines.append(
                                f"  - {r[0]} [{r[2]}/{r[3]}]: "
                                f"{(r[1] or '')[:120]}"
                            )
                except Exception as e:
                    logger.debug(f"Rejected-cohort fetch failed: {e}")

            # Fall back to the static banned list ONLY when wiki yields nothing
            # — this preserves behaviour on a fresh install with an empty wiki.
            if dynamic_banned_lines:
                banned_block = (
                    "  BANNED (from LIVE wiki + rejected cohort — "
                    "these patterns are demonstrably dead, do NOT re-propose):\n"
                    + "\n".join(dynamic_banned_lines) + "\n"
                )
            else:
                banned_block = (
                    "  BANNED (already priced, stop generating these):\n"
                    "  - Generic rest/B2B/travel advantages\n"
                    "  - Home underdog ATS\n"
                    "  - Eliminated team fades\n"
                    "  - Basic weather totals\n"
                    "  - Blowout-loss bounce-back (63 variants tested, 0 promoted, 3 anti-predictive at p<0.02 — structurally dead)\n"
                    "  - Any hypothesis that is just 'situational factor X is underpriced'\n"
                    "    without specifying WHY models can't capture it\n"
                )

            prompt = (
                f"CALLISTO HYPOTHESIS GENERATION — Cycle #{self._cycles}\n\n"
                f"{spinning_preamble}"
                f"{wiki_priors_block}"
                f"You are a skeptical quantitative researcher. Your default stance: "
                f"most hypotheses are noise. Your job is to find the rare ones that aren't.\n\n"
                f"BEFORE GENERATING: scrutinize the pipeline state below. If something "
                f"is broken or data quality is insufficient, say so in a 'pipeline_warning' "
                f"field instead of generating garbage hypotheses.\n\n"
                f"PIPELINE STATE:\n"
                f"  Total hypotheses: {draft_count + active_count + rejected_count} "
                f"({draft_count} draft, {active_count} active, {rejected_count} rejected)\n"
                f"  Rejection rate: {rejected_count}/{max(1, rejected_count + active_count)}"
                f" — if this is >90%, challenge whether the pipeline can test ANY hypothesis\n"
                f"  Eligible sports (>={MIN_GAMES_FOR_HYPOTHESIS} games): {', '.join(eligible_sports)}\n"
                f"  Ineligible (insufficient data): {', '.join(ineligible_sports) if ineligible_sports else 'none'}\n"
                f"  Data ranges: {json.dumps(date_ranges)}\n"
                f"  Collection stats: {json.dumps(data_stats)}\n"
                f"  Model: consensus devig (power method) — needs 3+ books to be reliable. "
                f"If most events show books_used=1, the devig is meaningless.\n\n"
                f"EXISTING HYPOTHESIS NAMES (avoid duplicates):\n"
                f"  {json.dumps(existing_names[:50])}\n\n"
                f"ELIGIBLE SPORTS ONLY: {eligible_sports}\n"
                f"DO NOT generate hypotheses for ineligible sports — they will be auto-rejected.\n\n"
                f"{regime_context}"
                f"{correlation_context}"
                f"EDGE PHILOSOPHY — READ THIS CAREFULLY:\n"
                f"Vegas prices rest, travel, B2Bs, weather, and schedule spots CORRECTLY.\n"
                f"Every model already has those columns. Do NOT generate more of these.\n"
                f"We need edges in dimensions that models DON'T HAVE COLUMNS FOR:\n\n"
                f"  UNCONVENTIONAL FACTORS (the kind of thing no model prices):\n"
                f"  - Team identity/cohesion: racial composition, regional identity, religious\n"
                f"    institutional values (e.g. BYU, Notre Dame, Liberty), coaching culture\n"
                f"  - Roster sociology: age variance, draft capital distribution, contract year\n"
                f"    clusters, language barriers, shared alma mater connections\n"
                f"  - Referee/umpire biases: specific officials' tendencies with specific teams,\n"
                f"    foul call patterns by game context, home-whistle strength by ref\n"
                f"  - Psychological momentum: post-trade deadline chemistry disruption, coaching\n"
                f"    hire/fire bounce, rivalry game emotional overperformance, clinch letdown\n"
                f"    dynamics in specific roster age profiles\n"
                f"  - Structural market inefficiencies: SGP correlation mispricing (correlated\n"
                f"    legs priced as independent), alt-line vs main-line gaps, live betting\n"
                f"    overreaction to early scores, cross-book consensus divergence\n"
                f"  - Scheme/matchup geometry: specific offensive system vs specific defensive\n"
                f"    scheme interactions, pace-forcing mismatches, platoon advantages\n"
                f"  - Media/narrative mispricing: nationally televised game line inflation,\n"
                f"    star player absence overreaction, preseason ranking anchor bias\n"
                f"  - Venue-specific micro-factors: altitude, turf vs grass transitions,\n"
                f"    dome-to-outdoor, timezone-specific circadian effects\n"
                f"  - Calendar/scheduling quirks: exam week in college sports, holiday games,\n"
                f"    conference tournament motivation asymmetry\n\n"
                f"{banned_block}\n"
                f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
                f'{{"hypotheses": [\n'
                f'  {{"name": "unique_snake_case_name", '
                f'"thesis": "Clear testable statement", '
                f'"sport": "<sport_key>", '
                f'"market_type": "spreads|totals|h2h|player_props", '
                f'"edge_threshold": 0.015, '
                f'"game_filters": {{"STRUCTURED filters on game context — see AVAILABLE FILTERS below"}}, '
                f'"line_filters": {{"STRUCTURED filters on bet lines — see AVAILABLE FILTERS below"}}'
                f'}}\n'
                f'], "pipeline_warning": "optional — flag if data quality makes testing pointless"}}\n\n'
                f"AVAILABLE GAME FILTERS (applied per-game BEFORE edge calculation):\n"
                f"  These filter which GAMES are tested. Use ONLY keys listed here:\n"
                f"  - require_b2b: true — at least one team on back-to-back\n"
                f"  - min_rest_mismatch: N — abs(home_rest - away_rest) >= N days\n"
                f"  - max_rest_days: N — at least one team with rest <= N days\n"
                f"  - min_games_in_4: N — at least one team played N+ games in 4 days\n"
                f"  - require_road_streak: N — at least one team on N+ consecutive road games\n"
                f"  - require_sandwich: true — at least one team in sandwich game spot\n"
                f"  - require_revenge: true — rematch within 30 days\n"
                f"  - min_win_pct: 0.65 — at least one team above this win%\n"
                f"  - max_win_pct: 0.35 — at least one team below this win%\n"
                f"  - win_pct_range: [0.43, 0.57] — at least one team in this range (bubble)\n"
                f"  - max_prev_margin: -10 — at least one team lost prev game by 10+ pts\n"
                f"  - min_prev_margin: 15 — at least one team won prev game by 15+ pts\n"
                f"  - side: 'home'|'away' — apply conditions to THIS side specifically\n"
                f"  If your hypothesis doesn't need game filtering, use {{}}\n\n"
                f"AVAILABLE LINE FILTERS (applied per-line DURING edge calculation):\n"
                f"  These filter which BET LINES are evaluated within matching games:\n"
                f"  - home_away: 'home'|'away' — only evaluate this team's line\n"
                f"  - dog_fav: 'underdog'|'favorite' — only evaluate this role\n"
                f"  - side: 'Over'|'Under' — for totals, only evaluate this side\n"
                f"  - spread_range: [3, 7] — only test spreads in this point range\n"
                f"  - spread_min: 3 — only test spreads >= this value\n"
                f"  If your hypothesis doesn't need line filtering, use {{}}\n\n"
                f"AVAILABLE CONTEXT DATA per game (what game_contexts actually stores):\n"
                f"  ALL SPORTS: scores (home/away), rest_days, b2b (boolean), records,\n"
                f"    attendance, venue (name, dome, altitude_ft), tz_offset, national_tv,\n"
                f"    officials (refs/umps), broadcasts, spread, total,\n"
                f"    play_by_play (period-level scoring summaries)\n"
                f"  MLB ONLY: park_factor (venue-specific run environment multiplier)\n"
                f"  NOT AVAILABLE (do NOT reference): umpire strike zones, ref crew tendencies,\n"
                f"    goalie workloads, pitch-level data, weather, public betting %,\n"
                f"    player prop lines, lineup data, advanced team stats\n\n"
                f"CRITICAL: Every hypothesis MUST include game_filters and line_filters.\n"
                f"If a hypothesis can't be expressed with these filters, it CANNOT be tested.\n"
                f"Do NOT generate hypotheses requiring data NOT in the available context list above.\n\n"
                f"RULES:\n"
                f"- Generate 3-5 hypotheses per call\n"
                f"- Spread across multiple sports — do NOT cluster on one sport\n"
                f"- Each hypothesis MUST explain WHY this edge would survive (why can't\n"
                f"  Vegas or sharp models capture this factor?)\n"
                f"- Each must be testable with the ACTUAL data we have (check collection stats)\n"
                f"- Names must be unique (not in existing list)\n"
                f"- Thesis must be specific and falsifiable\n"
                f"- If the pipeline state shows systemic issues (high rejection rate, "
                f"thin data, broken resolution), flag them — do NOT just generate more "
                f"hypotheses into a broken funnel\n"
            )

            result = await escalate_with_ladder(
                prompt,
                task_type="hypothesis_gen",
                hermes_caller="hypothesis_gen",
            )
            self._last_claude_call = time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
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
                    for nh in parsed.get("hypotheses", []):
                        try:
                            h_sport = nh.get("sport", "basketball_nba")
                            # Hard gate: reject hypotheses for sports with insufficient data
                            if eligible_sports and h_sport not in eligible_sports:
                                logger.info(
                                    f"Research: rejected '{nh.get('name')}' — "
                                    f"sport '{h_sport}' has insufficient data "
                                    f"({game_counts_by_sport.get(h_sport, 0)} games < {MIN_GAMES_FOR_HYPOTHESIS})"
                                )
                                continue
                            h_config = {
                                "source": "claude_primary_gen",
                                "cycle": self._cycles,
                                "training_period_start": training_period_start,
                                "training_period_end": training_period_end,
                                "forward_test_start": forward_test_start,
                            }
                            # Pass structured filters from Claude to model_config
                            if nh.get("game_filters"):
                                h_config["game_filters"] = nh["game_filters"]
                            if nh.get("line_filters"):
                                h_config["line_filters"] = nh["line_filters"]
                            # Enrich with regime data if available for this sport
                            if _regime_cache:
                                sport_regimes = {
                                    k: v for k, v in _regime_cache.items()
                                    if k.startswith(h_sport + ":")
                                    and v.get("has_edge_signal")
                                }
                                if sport_regimes:
                                    # Attach summary of regime signals for backtester
                                    regime_summary = {}
                                    for rk, rv in list(sport_regimes.items())[:5]:
                                        team = rv.get("team", rk)
                                        rb = rv.get("recency_bias", {})
                                        regime_summary[team] = {
                                            "regime": rv.get("power_rating", {}).get("regime", "stable") if isinstance(rv.get("power_rating"), dict) else "stable",
                                            "recency_bias_score": rb.get("bias_magnitude", 0) if isinstance(rb, dict) else 0,
                                            "signals": rv.get("actionable_signals", []),
                                        }
                                    h_config["regime_signals"] = regime_summary
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", f"claude_gen_{self._cycles}"),
                                thesis=nh.get("thesis", ""),
                                sport=h_sport,
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config=h_config,
                            )
                            total_created += 1
                        except Exception as e:
                            logger.warning(f"Failed to create Claude hypothesis: {e}")

                    used_claude = True
                    logger.info(
                        f"Research: Claude generated {total_created} hypotheses"
                    )
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(
                        f"Claude hypothesis response not valid JSON: {e}"
                    )
            elif result.get("rate_limited"):
                logger.info(
                    "Research: Claude rate-limited during hypothesis gen — "
                    "falling back to templates"
                )
        except Exception as e:
            logger.warning(f"Claude hypothesis generation failed: {e}")

    # ── FALLBACK: Template + local model generation when Claude unavailable ──
    if not used_claude:
        # Defer the Claude prompt so it runs when Claude comes back
        from tools.claude_code import is_available as _ca
        if not _ca():
            try:
                existing_names = await self.hypothesis_manager.get_names()
                deferred_prompt = (
                    f"CALLISTO HYPOTHESIS GENERATION — Deferred from Cycle #{self._cycles}\n\n"
                    f"Generate 3-5 UNCONVENTIONAL sports betting hypotheses across: {RESEARCH_SPORTS}\n\n"
                    f"BANNED: rest/B2B, home underdog ATS, eliminated fades, basic weather. "
                    f"Vegas prices these. Find edges in dimensions models lack columns for: "
                    f"team identity/cohesion, roster sociology, ref biases, scheme geometry, "
                    f"SGP correlation mispricing, media narrative inflation, calendar quirks.\n\n"
                    f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                    f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                    f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                    f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                    f"EXISTING NAMES (avoid duplicates): {json.dumps(existing_names[:30])}\n\n"
                    f"RESPOND WITH JSON: {{\"hypotheses\": [{{\"name\": \"...\", \"thesis\": \"...\", "
                    f"\"sport\": \"...\", \"market_type\": \"...\", \"edge_threshold\": 0.015, "
                    f"\"game_filters\": {{}}, \"line_filters\": {{}}}}]}}"
                )
                await self._work_queue.enqueue("hypothesis_gen", deferred_prompt, priority=2)
                self._downtime_tracker.item_queued()
                logger.info("Research: hypothesis gen deferred to work queue (Claude unavailable)")
            except Exception as e:
                logger.warning(f"Failed to enqueue deferred hypothesis gen: {e}")

            # Try local model via escalation ladder (Apriel > Qwen3 > DeepSeek)
            try:
                from inference import escalate_with_ladder
                existing_l = (await self.hypothesis_manager.get_names())[:30]
                ladder_prompt = (
                    f"Generate 3 testable sports betting hypotheses.\n"
                    f"Sports: {RESEARCH_SPORTS}\n"
                    f"Market types: h2h, spreads, totals, player_points, player_strikeouts\n"
                    f"EXISTING (avoid duplicates): {json.dumps(existing_l)}\n\n"
                    f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                    f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                    f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                    f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                    f"RESPOND WITH JSON ONLY:\n"
                    f'{{"hypotheses": [{{"name": "sport_descriptive_name", '
                    f'"thesis": "Testable claim", "sport": "basketball_nba", '
                    f'"market_type": "spreads", "edge_threshold": 0.003, '
                    f'"game_filters": {{}}, "line_filters": {{}}}}]}}'
                )
                ladder_result = await escalate_with_ladder(
                    ladder_prompt, task_type="hypothesis_gen", timeout=120,
                )
                ladder_content = ladder_result.get("content", "")
                if ladder_content:
                    from inference import _parse_json_response
                    parsed = _parse_json_response(ladder_content)
                    if parsed and isinstance(parsed, dict):
                        for nh in parsed.get("hypotheses", []):
                            try:
                                _ladder_config = {
                                        "source": f"ladder_{ladder_result.get('model_used', 'unknown')}",
                                        "cycle": self._cycles,
                                        "training_period_start": training_period_start,
                                        "training_period_end": training_period_end,
                                        "forward_test_start": forward_test_start,
                                    }
                                if nh.get("game_filters"):
                                    _ladder_config["game_filters"] = nh["game_filters"]
                                if nh.get("line_filters"):
                                    _ladder_config["line_filters"] = nh["line_filters"]
                                await self.hypothesis_manager.create_hypothesis(
                                    name=nh.get("name", f"ladder_gen_{self._cycles}"),
                                    thesis=nh.get("thesis", ""),
                                    sport=nh.get("sport", "basketball_nba"),
                                    market_type=nh.get("market_type", "spreads"),
                                    edge_threshold=nh.get("edge_threshold", 0.003),
                                    model_config=_ladder_config,
                                )
                                total_created += 1
                                self._hypotheses_generated += 1
                            except Exception as e:
                                logger.debug(f"Ladder hypothesis creation failed: {e}")
                        logger.info(
                            f"Research: ladder model ({ladder_result.get('model_used')}) "
                            f"generated {total_created} hypotheses"
                        )
            except Exception as e:
                logger.warning(f"Ladder hypothesis generation failed: {e}")

            # Also try template-based local fallback for quick hypothesis ideas
            try:
                from tools.work_queue import local_fallback_hypothesis_gen
                pipeline_state = (
                    f"Cycles: {self._cycles}, Hypotheses: {self._hypotheses_generated}, "
                    f"Backtests: {self._backtests_run}"
                )
                existing_names = await self.hypothesis_manager.get_names()
                local_hypos = await local_fallback_hypothesis_gen(
                    pipeline_state, existing_names, ""
                )
                for nh in local_hypos:
                    try:
                        _local_config = {
                                "source": "local_fallback_gen",
                                "cycle": self._cycles,
                                "training_period_start": training_period_start,
                                "training_period_end": training_period_end,
                                "forward_test_start": forward_test_start,
                            }
                        if nh.get("game_filters"):
                            _local_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _local_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", f"local_gen_{self._cycles}"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_local_config,
                        )
                        total_created += 1
                    except Exception as e:
                        logger.debug(f"Local fallback hypothesis creation failed: {e}")
                if local_hypos:
                    logger.info(f"Research: local model generated {len(local_hypos)} hypotheses")
            except Exception as e:
                logger.debug(f"Local fallback hypothesis gen failed: {e}")

        # Template fallback always runs when Claude didn't
        # Re-check data availability for template path
        _template_eligible = RESEARCH_SPORTS
        if hasattr(self, 'data_collector') and self.data_collector._db:
            try:
                _gc = {}
                cursor = await self.data_collector._db.execute(
                    "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                )
                for row in await cursor.fetchall():
                    _gc[row[0]] = row[1]
                _template_eligible = [
                    s for s in RESEARCH_SPORTS
                    if _gc.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                ]
                _skipped = [s for s in RESEARCH_SPORTS if s not in _template_eligible]
                if _skipped:
                    logger.info(
                        f"Research: template gen skipping sports with <{MIN_GAMES_FOR_HYPOTHESIS} games: {_skipped}"
                    )
            except Exception:
                pass
        logger.info("Research: using template fallback for hypothesis generation")
        for sport in _template_eligible:
            try:
                quota = 20
                created = await self.hypothesis_generator.generate_from_templates(
                    sport=sport,
                    max_hypotheses=quota,
                    training_cutoff_date=training_period_end,
                )
                total_created += len(created)
            except Exception as e:
                logger.warning(f"Template generation failed for {sport}: {e}")

    # ── DATA-DRIVEN PATTERN DISCOVERY ──
    # Pure computation — no LLM needed. Discovers statistical anomalies
    # from historical data using temporal splits. Runs EVERY cycle
    # regardless of Claude availability because data-driven hypotheses
    # are grounded in actual patterns, not LLM-plausible theses.
    if total_created < 3:  # Always try unless we already have enough
        try:
            from tools.temporal_analysis import generate_hypotheses_from_analysis
            import asyncio

            pattern_hypotheses = await asyncio.get_event_loop().run_in_executor(
                None,
                generate_hypotheses_from_analysis,
                os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"),
                None,  # all sports
                training_period_end,
                20,  # min_sample
                3.0,  # min_edge %
                0.10,  # max p-value
            )
            for h_def in pattern_hypotheses[:5]:  # cap at 5 per cycle
                try:
                    await self.hypothesis_manager.create_hypothesis(
                        name=h_def["name"],
                        thesis=h_def["thesis"],
                        sport=h_def["sport"],
                        market_type=h_def["market_type"],
                        model_config=h_def.get("model_config", {}),
                    )
                    total_created += 1
                except Exception as e:
                    logger.debug(f"Pattern hypothesis creation failed: {e}")
            if pattern_hypotheses:
                logger.info(
                    f"Research: data-driven pattern discovery generated "
                    f"{min(len(pattern_hypotheses), 5)} hypotheses"
                )
        except Exception as e:
            logger.debug(f"Pattern discovery failed (non-fatal): {e}")

    self._hypotheses_generated += total_created
    logger.info(f"Research: generated {total_created} new hypotheses")


async def phase_injury_prop_hypotheses(loop) -> None:
    self = loop
    """Generate prop hypotheses from current injury data.

    When a key player is out, redistribute_usage() predicts which
    teammates absorb the production. This directly feeds into player
    prop edges: if Tatum is out, Jaylen Brown's usage increases and
    his over on points has value.

    Creates draft hypotheses for each high-confidence prop opportunity.
    """
    from tools.contextual_data import get_injuries as _get_injuries
    from tools.injury_model import redistribute_usage as _redistribute

    _sport_map = {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "baseball_mlb": "MLB",
    }

    active_sports = list(self.line_monitor._snapshots.keys()) if self.line_monitor else []
    if not active_sports:
        active_sports = ["basketball_nba"]

    total_created = 0
    for sport_key in active_sports:
        model_sport = _sport_map.get(sport_key)
        if not model_sport:
            continue

        try:
            inj_data = await _get_injuries(sport_key)
        except Exception as e:
            logger.warning(f"Injury fetch failed for {sport_key}: {e}")
            continue

        injuries = inj_data.get("injuries", [])
        # Only process players who are OUT (not questionable)
        out_players = [i for i in injuries if (i.get("status") or "").lower() == "out"]
        if not out_players:
            continue

        for inj in out_players[:10]:  # cap at 10 per sport
            player = inj.get("player", "")
            team = inj.get("team", "")
            position = inj.get("position", "")
            if not player or not team:
                continue

            # Build minimal absent player stats from position heuristics
            absent_stats = {}
            if model_sport == "NBA":
                # Default to a starter-level stat line; real data would be better
                absent_stats = {"ppg": 18.0, "rpg": 5.0, "apg": 4.0, "usage_rate": 25.0}
            elif model_sport == "NFL":
                absent_stats = {"role": position or "WR1"}

            try:
                redist = _redistribute(
                    absent_player=player,
                    team_roster=[],  # empty roster triggers generic redistribution
                    sport=model_sport,
                    absent_player_stats=absent_stats or None,
                )
            except Exception as e:
                logger.debug(f"Redistribution failed for {player}: {e}")
                continue

            if not redist:
                continue

            # Create draft hypotheses for top beneficiaries
            for r in redist[:3]:
                beneficiary = r.player if hasattr(r, "player") else "Unknown"
                usage_inc = r.usage_increase if hasattr(r, "usage_increase") else 0
                stat_chg = r.projected_stat_change if hasattr(r, "projected_stat_change") else {}

                if usage_inc < 2.0:
                    continue  # too small to be actionable

                hypo_name = (
                    f"injury_prop_{team}_{beneficiary}_{player}_out"
                ).replace(" ", "_").lower()

                # Check if hypothesis already exists
                try:
                    existing_names = await self.hypothesis_manager.get_all_names()
                    if hypo_name in existing_names:
                        continue
                except Exception:
                    pass

                ppg_inc = stat_chg.get("ppg_increase", stat_chg.get("projected_ppg_increase", 0))
                description = (
                    f"With {player} OUT for {team}, {beneficiary} absorbs "
                    f"+{usage_inc:.1f}% usage (projected +{ppg_inc:.1f} PPG). "
                    f"Player prop overs for {beneficiary} have value when "
                    f"{player} is confirmed out."
                )

                try:
                    await self.hypothesis_manager.create_hypothesis(
                        name=hypo_name,
                        description=description,
                        sport=sport_key,
                        tags=["injury", "prop", "usage_redistribution", "auto_generated"],
                    )
                    total_created += 1
                    logger.info(
                        f"Injury prop hypothesis created: {beneficiary} "
                        f"benefits from {player} OUT (+{usage_inc:.1f}% usage)"
                    )
                except Exception as e:
                    logger.debug(f"Failed to create injury prop hypothesis: {e}")

    if total_created:
        logger.info(f"Injury prop phase: created {total_created} hypotheses")
