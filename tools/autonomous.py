"""
Autonomous reasoning loop — makes Callisto think without being asked.

Two loops run concurrently:
  1. AutonomousLoop — real-time edge detection (existing, unchanged)
  2. ResearchLoop — 24/7 hypothesis machine (NEW)

ResearchLoop cycle:
  - Collect post-game data (ESPN scores, box scores) — FREE
  - Embed game contexts and prop outcomes into vector store
  - Generate hypotheses from templates + embedding clusters
  - Backtest hypotheses against historical data
  - Evaluate significance, auto-promote or auto-reject
  - Escalate to Claude Code for heavy analysis when needed
  - Paper trade promoted hypotheses on live odds

This is the core autonomous research engine that runs without human input.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from tools import telegram
from tools.edge_confidence import score_edge

logger = logging.getLogger("callisto.autonomous")

# Only analyze edges above these thresholds — don't waste GPU on noise
MIN_IMPLIED_RANGE = 0.04       # 4% cross-book disagreement minimum
MIN_SOFT_EDGE_VS_SHARP = 0.03  # 3% vs sharp consensus minimum
MIN_CONFIDENCE_TO_ALERT = 0.45 # Don't alert below SPECULATIVE+

# Max concurrent AGP sessions to avoid GPU overload
MAX_CONCURRENT_SESSIONS = 1

# Cooldown between full analysis cycles (seconds)
ANALYSIS_COOLDOWN = 120  # 2 min between analysis runs

# Don't re-analyze the same edge within this window
EDGE_DEDUP_WINDOW = 1800  # 30 minutes


class AutonomousLoop:
    """Proactive reasoning engine — turns raw edges into analyzed recommendations."""

    def __init__(self, orchestrator, line_monitor):
        """
        Args:
            orchestrator: The Orchestrator instance (has run_session())
            line_monitor: The LineMonitor instance (has edge reports)
        """
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._analyzed_edges: dict[str, float] = {}  # edge_key -> timestamp
        self._session_count = 0
        self._alert_count = 0

    async def start(self) -> None:
        """Start the autonomous reasoning loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Autonomous reasoning loop started")

    async def stop(self) -> None:
        """Stop the loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"Autonomous loop stopped — {self._session_count} sessions, "
            f"{self._alert_count} alerts sent"
        )

    async def _loop(self) -> None:
        """Main loop — find edges, reason about them, alert if worthy."""
        # Wait for first snapshot cycle to populate data
        await asyncio.sleep(30)

        while self._running:
            try:
                candidates = self._find_analysis_candidates()

                if candidates:
                    logger.info(
                        f"Autonomous: {len(candidates)} edge candidates found, "
                        f"analyzing top {min(len(candidates), 3)}"
                    )

                    # Analyze top candidates sequentially (GPU bound)
                    for candidate in candidates[:3]:
                        if not self._running:
                            break
                        await self._analyze_edge(candidate)

                # Clean up old dedup entries
                self._cleanup_dedup()

                await asyncio.sleep(ANALYSIS_COOLDOWN)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Autonomous loop error: {e}")
                await asyncio.sleep(30)

    def _find_analysis_candidates(self) -> list[dict]:
        """
        Scan latest edge reports for candidates worth full AGP analysis.

        Filters:
        - Implied range >= 4% (real disagreement, not noise)
        - Has soft book edges vs sharp consensus >= 3%
        - Not analyzed in the last 30 minutes
        """
        candidates = []
        now = time.time()

        all_reports = self.line_monitor.get_edge_report()
        if not isinstance(all_reports, dict):
            return []

        for sport, report in all_reports.items():
            if not isinstance(report, dict):
                continue

            # Cross-book divergence edges
            for market_key in ["cross_book_spreads", "cross_book_h2h", "cross_book_totals"]:
                for edge in report.get(market_key, []):
                    implied_range = edge.get("implied_range", 0)
                    if implied_range < MIN_IMPLIED_RANGE:
                        continue

                    # Check for soft book vs sharp edges
                    soft_edges = edge.get("soft_book_edges", [])
                    best_soft = max(
                        (se.get("edge_vs_sharp", 0) for se in soft_edges),
                        default=0,
                    )
                    if best_soft < MIN_SOFT_EDGE_VS_SHARP:
                        continue

                    # Dedup check
                    edge_key = f"{sport}:{edge.get('game', '')}:{edge.get('team', '')}:{market_key}"
                    last_analyzed = self._analyzed_edges.get(edge_key, 0)
                    if now - last_analyzed < EDGE_DEDUP_WINDOW:
                        continue

                    # Score confidence
                    conf = score_edge(
                        edge_pct=round(best_soft * 100, 2),
                        books_compared=edge.get("num_bookmakers", edge.get("book_count", 1)),
                        book_names=[edge.get("best_line", {}).get("bookmaker", "")],
                        market=market_key.replace("cross_book_", ""),
                        has_sharp_book=edge.get("sharp_consensus") is not None,
                    )

                    candidates.append({
                        "sport": sport,
                        "edge_key": edge_key,
                        "game": edge.get("game", ""),
                        "game_id": edge.get("game_id", ""),
                        "team": edge.get("team", ""),
                        "market": market_key.replace("cross_book_", ""),
                        "implied_range": implied_range,
                        "best_soft_edge": best_soft,
                        "soft_book_edges": soft_edges,
                        "best_line": edge.get("best_line", {}),
                        "worst_line": edge.get("worst_line", {}),
                        "sharp_consensus": edge.get("sharp_consensus"),
                        "num_bookmakers": edge.get("num_bookmakers", 0),
                        "confidence": conf,
                    })

        # Sort by edge magnitude — biggest edges first
        candidates.sort(key=lambda c: c["best_soft_edge"], reverse=True)
        return candidates

    async def _analyze_edge(self, candidate: dict) -> None:
        """
        Run full AGP session on an edge candidate.

        The Architect gets the edge data as a structured query and can use
        tools (injuries, props, cross-book data) to build a complete picture.
        """
        sport = candidate["sport"]
        game = candidate["game"]
        team = candidate["team"]
        market = candidate["market"]
        edge_pct = round(candidate["best_soft_edge"] * 100, 1)
        conf = candidate["confidence"]

        logger.info(
            f"Autonomous: analyzing {team} {market} in {game} "
            f"(edge={edge_pct}%, confidence={conf.tier})"
        )

        # Mark as analyzed
        self._analyzed_edges[candidate["edge_key"]] = time.time()

        # Build a targeted query for the AGP session
        soft_detail = ""
        for se in candidate.get("soft_book_edges", [])[:3]:
            price = se.get("price", 0)
            price_str = f"+{price}" if price > 0 else str(price)
            soft_detail += (
                f"  - {se.get('bookmaker', '?')}: {price_str} "
                f"(edge {se.get('edge_vs_sharp', 0):.1%}, "
                f"EV ${se.get('ev', {}).get('expected_value', 0):.2f})\n"
            )

        best = candidate.get("best_line", {})
        worst = candidate.get("worst_line", {})
        best_price = best.get("price", 0)
        best_str = f"+{best_price}" if best_price > 0 else str(best_price)

        query = (
            f"AUTONOMOUS EDGE ANALYSIS — {sport}\n"
            f"Game: {game}\n"
            f"Team: {team} | Market: {market}\n"
            f"Cross-book implied range: {candidate['implied_range']:.1%}\n"
            f"Sharp consensus: {candidate.get('sharp_consensus', 'N/A')}\n"
            f"Best line: {best.get('bookmaker', '?')} {best_str}\n"
            f"Books compared: {candidate['num_bookmakers']}\n"
            f"\nSoft book edges vs sharp:\n{soft_detail}\n"
            f"Pre-scored confidence: {conf.tier} ({conf.score:.2f})\n\n"
            f"TASK: Use available tools to verify this edge. Check injuries, "
            f"check if the line has moved, check player props if relevant. "
            f"Determine if this is a real exploitable edge on DraftKings or Fanatics, "
            f"or if it's noise. Give a final recommendation with confidence score."
        )

        try:
            result = await asyncio.wait_for(
                self.orchestrator.run_session(query),
                timeout=180,  # 3 minute max per session
            )
            self._session_count += 1

            # Extract the session result
            summary = result.get("summary", {})
            conclusion = summary.get("conclusion", "No conclusion")
            final_confidence = summary.get("confidence_score", 0)
            tier = summary.get("confidence_tier", "UNVERIFIED")

            logger.info(
                f"Autonomous: {team} {market} → {tier} ({final_confidence:.2f}): "
                f"{conclusion[:100]}"
            )

            # Alert if above threshold
            if final_confidence >= MIN_CONFIDENCE_TO_ALERT:
                # Find best DK/Fanatics line from soft edges
                target_book = "?"
                target_price = 0
                for se in candidate.get("soft_book_edges", []):
                    bm = se.get("bookmaker", "").lower()
                    if "draftkings" in bm or "fanatics" in bm:
                        target_book = se.get("bookmaker", "?")
                        target_price = se.get("price", 0)
                        break

                if not target_price and candidate.get("soft_book_edges"):
                    se = candidate["soft_book_edges"][0]
                    target_book = se.get("bookmaker", "?")
                    target_price = se.get("price", 0)

                await telegram.alert_edge(
                    game=game,
                    team=team,
                    market=market,
                    edge_pct=edge_pct,
                    confidence_tier=tier,
                    confidence_score=final_confidence,
                    best_book=target_book,
                    best_price=target_price,
                    reasoning=conclusion[:200],
                )
                self._alert_count += 1
                logger.info(f"Autonomous: Telegram alert sent for {team} {market}")

        except asyncio.TimeoutError:
            logger.warning(f"Autonomous: session timed out for {team} {market}")
        except Exception as e:
            logger.error(f"Autonomous: session failed for {team} {market}: {e}")

    def _cleanup_dedup(self) -> None:
        """Remove old entries from the dedup cache."""
        now = time.time()
        expired = [
            k for k, t in self._analyzed_edges.items()
            if now - t > EDGE_DEDUP_WINDOW * 2
        ]
        for k in expired:
            del self._analyzed_edges[k]

    def get_status(self) -> dict:
        """Return loop status."""
        return {
            "running": self._running,
            "sessions_run": self._session_count,
            "alerts_sent": self._alert_count,
            "cached_edge_keys": len(self._analyzed_edges),
            "analysis_cooldown_seconds": ANALYSIS_COOLDOWN,
            "min_confidence_to_alert": MIN_CONFIDENCE_TO_ALERT,
        }


# ──────────────────────────────────────────────────────────
# RESEARCH LOOP — Karpathy-style autonomous loop
# ──────────────────────────────────────────────────────────
# Design principles (from Karpathy's autoresearch):
#   1. Loop as tight as possible — rate limit is the only governor
#   2. Every iteration: hypothesize → test → measure → keep/discard
#   3. Never pause for human approval
#   4. Append-only experiment log for all attempts
#   5. Clean state between iterations (prevent error accumulation)
#   6. Maximize token throughput to Claude Code at all times

# Cadence controls — MAXIMUM THROUGHPUT (Karpathy loop: rate limit is the only governor)
RESEARCH_CYCLE_INTERVAL = 60        # 1 min between cycles — tight as possible
DATA_COLLECTION_INTERVAL = 900      # 15 min between data pulls — fresher data
HYPOTHESIS_GEN_INTERVAL = 600       # 10 min between hypothesis generation — stay creative
BACKTEST_BATCH_SIZE = 20            # Hypotheses to backtest per cycle — higher throughput
CLAUDE_ESCALATION_COOLDOWN = 60     # 1 min between Claude Code calls — push until rate limited

# Domains to research (ordered by data availability)
RESEARCH_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "icehockey_nhl",
    "baseball_mlb",
    "golf_pga",
]


class ResearchLoop:
    """
    24/7 autonomous research engine.

    Runs independently of AutonomousLoop. While AutonomousLoop handles
    real-time edge detection and alerting, ResearchLoop handles the
    slow, deep work: collecting data, discovering patterns, generating
    and testing hypotheses, and escalating to Claude Code.

    The local models (Architect/Manager) drive the cycle. Claude Code
    is only called for tasks too heavy for local models (creative
    hypothesis generation, complex statistical analysis).
    """

    def __init__(
        self,
        hypothesis_manager,
        hypothesis_generator,
        backtest_engine,
        data_collector,
        vector_store,
        orchestrator=None,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.hypothesis_generator = hypothesis_generator
        self.backtest_engine = backtest_engine
        self.data_collector = data_collector
        self.vector_store = vector_store
        self.orchestrator = orchestrator

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Timestamps for cadence control
        self._last_data_collect = 0.0
        self._last_hypothesis_gen = 0.0
        self._last_claude_call = 0.0

        # Counters
        self._cycles = 0
        self._data_collections = 0
        self._hypotheses_generated = 0
        self._backtests_run = 0
        self._claude_escalations = 0
        self._promotions = 0
        self._rejections = 0

    async def start(self) -> None:
        """Start the research loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Research loop started — autonomous hypothesis machine online")

    async def stop(self) -> None:
        """Stop the research loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"Research loop stopped — {self._cycles} cycles, "
            f"{self._hypotheses_generated} hypotheses generated, "
            f"{self._backtests_run} backtests run, "
            f"{self._promotions} promoted, {self._rejections} rejected"
        )

    async def _loop(self) -> None:
        """Main research cycle."""
        # Brief delay to let other systems start
        await asyncio.sleep(15)

        while self._running:
            try:
                self._cycles += 1
                logger.info(f"Research cycle #{self._cycles} starting")

                # Phase 1: Collect data (if due)
                await self._phase_collect_data()

                if not self._running:
                    break

                # Phase 2: Embed new data
                await self._phase_embed_data()

                if not self._running:
                    break

                # Phase 3: Generate hypotheses (if due)
                await self._phase_generate_hypotheses()

                if not self._running:
                    break

                # Phase 4: Backtest pending hypotheses
                await self._phase_backtest()

                if not self._running:
                    break

                # Phase 5: Evaluate and promote/reject
                await self._phase_evaluate()

                if not self._running:
                    break

                # Phase 6: Paper trade active hypotheses
                await self._phase_paper_trade()

                if not self._running:
                    break

                # Phase 7: Claude deep analysis — use remaining budget
                await self._phase_claude_deep_work()

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

    async def _phase_collect_data(self) -> None:
        """Collect post-game data from ESPN (free)."""
        now = time.time()
        if now - self._last_data_collect < DATA_COLLECTION_INTERVAL:
            return

        logger.info("Research: collecting post-game data")
        self._last_data_collect = now

        for sport in RESEARCH_SPORTS:
            try:
                # Collect yesterday's and today's games
                from datetime import datetime, timedelta, timezone
                today = datetime.now(timezone.utc)
                yesterday = today - timedelta(days=1)

                for dt in [yesterday, today]:
                    date_str = dt.strftime("%Y%m%d")
                    scores = await self.data_collector.collect_scores(sport, date_str)
                    if scores.get("completed", 0) > 0:
                        await self.data_collector.collect_box_scores(sport, date_str)

                # Resolve any pending paper trades
                for dt in [yesterday, today]:
                    date_fmt = dt.strftime("%Y-%m-%d")
                    await self.data_collector.resolve_prop_outcomes(sport, date_fmt)

                self._data_collections += 1
            except Exception as e:
                logger.warning(f"Data collection failed for {sport}: {e}")

    async def _phase_embed_data(self) -> None:
        """Embed new game contexts into the vector store."""
        from tools.embeddings import embed_game_context

        contexts = await self.data_collector.get_unembedded_contexts(limit=50)
        if not contexts:
            return

        logger.info(f"Research: embedding {len(contexts)} game contexts")

        for ctx in contexts:
            try:
                await embed_game_context(
                    store=self.vector_store,
                    sport=ctx["sport"],
                    game_date=ctx["game_date"],
                    home_team=ctx["home_team"],
                    away_team=ctx["away_team"],
                    context=ctx["context"],
                )
                await self.data_collector.mark_embedded(ctx["id"])
            except Exception as e:
                logger.warning(f"Embedding failed for context {ctx['id']}: {e}")

    async def _phase_generate_hypotheses(self) -> None:
        """Generate new hypotheses from templates and clusters."""
        now = time.time()
        if now - self._last_hypothesis_gen < HYPOTHESIS_GEN_INTERVAL:
            return

        logger.info("Research: generating hypotheses")
        self._last_hypothesis_gen = now

        total_created = 0

        # Template-based generation for each sport
        for sport in RESEARCH_SPORTS:
            try:
                created = await self.hypothesis_generator.generate_from_templates(
                    sport=sport, max_hypotheses=20,
                )
                total_created += len(created)
            except Exception as e:
                logger.warning(f"Template generation failed for {sport}: {e}")

        # Cluster-based generation (needs enough embedded data)
        try:
            stats = await self.vector_store.get_collection_stats("prop_outcomes")
            if isinstance(stats, dict) and stats.get("count", 0) >= 50:
                cluster_created = await self.hypothesis_generator.generate_from_clusters(
                    collection="prop_outcomes",
                    min_cluster_size=10,
                )
                total_created += len(cluster_created)
        except Exception as e:
            logger.warning(f"Cluster generation failed: {e}")

        # Claude Code escalation for creative hypotheses (if available + cooldown elapsed)
        from tools.claude_code import is_available as claude_available
        if (now - self._last_claude_call > CLAUDE_ESCALATION_COOLDOWN
                and claude_available()):
            try:
                # Build data summary for Claude
                all_hypos = await self.hypothesis_manager.list_hypotheses()
                draft_count = sum(1 for h in all_hypos if h["status"] == "draft")
                active_count = sum(
                    1 for h in all_hypos
                    if h["status"] in ("backtesting", "paper_trading", "live")
                )
                rejected_count = sum(1 for h in all_hypos if h["status"] == "rejected")

                data_stats = await self.data_collector.get_collection_stats()

                summary = (
                    f"Callisto autonomous research status:\n"
                    f"- {len(all_hypos)} total hypotheses "
                    f"({draft_count} draft, {active_count} active, "
                    f"{rejected_count} rejected)\n"
                    f"- Data: {json.dumps(data_stats, indent=2)}\n"
                    f"- Sports covered: {', '.join(RESEARCH_SPORTS)}\n"
                    f"- Model: consensus devig (power method) across 3+ books\n"
                    f"\nCallisto is a general-purpose autonomous agent. "
                    f"Current sports focus: pre-game edges, player prop mispricing, "
                    f"situational factors books don't price correctly. "
                    f"Future domains: stocks, crypto, any quantifiable edge.\n"
                    f"\nGenerate novel, testable hypotheses we haven't tried yet. "
                    f"Focus on edges that persist for hours (pre-game props), "
                    f"not speed-dependent arbitrage. Think about: "
                    f"rest days, travel, altitude, referee tendencies, "
                    f"public betting % vs sharp, weather, revenge games, "
                    f"back-to-backs, divisional rivalry patterns, "
                    f"line movement timing, closing line value patterns."
                )

                for sport in RESEARCH_SPORTS[:3]:  # Top 3 sports
                    created = await self.hypothesis_generator.generate_from_claude(
                        sport=sport, data_summary=summary,
                    )
                    total_created += len(created)

                self._last_claude_call = now
                self._claude_escalations += 1
            except Exception as e:
                logger.warning(f"Claude escalation failed: {e}")

        self._hypotheses_generated += total_created
        logger.info(f"Research: generated {total_created} new hypotheses")

    async def _phase_backtest(self) -> None:
        """Backtest draft hypotheses that are ready."""
        # Get draft hypotheses that haven't been backtested
        drafts = await self.hypothesis_manager.list_hypotheses(status="draft")

        if not drafts:
            return

        # Backtest up to BACKTEST_BATCH_SIZE per cycle
        to_test = drafts[:BACKTEST_BATCH_SIZE]
        logger.info(f"Research: backtesting {len(to_test)} hypotheses")

        for h in to_test:
            if not self._running:
                break

            try:
                # Use full available historical data range for backtest
                from datetime import datetime, timedelta, timezone
                end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                start_date = "2023-01-01"  # Full cached range

                result = await self.backtest_engine.run_backtest(
                    hypothesis_id=h["hypothesis_id"],
                    start_date=start_date,
                    end_date=end_date,
                    credit_budget=10,  # Conservative per-hypothesis
                )

                self._backtests_run += 1
                signals = result.get("signals_generated", 0)
                logger.info(
                    f"Research: backtest {h['hypothesis_id']} — "
                    f"{result.get('total_events', 0)} events, {signals} signals"
                )
            except Exception as e:
                logger.warning(
                    f"Backtest failed for {h['hypothesis_id']}: {e}"
                )

    async def _phase_evaluate(self) -> None:
        """Evaluate backtesting hypotheses for promotion or rejection."""
        # First, resolve any unresolved backtest events from game_results
        try:
            resolution = await self.backtest_engine.resolve_from_game_results()
            if resolution.get("resolved", 0) > 0:
                logger.info(
                    f"Research: resolved {resolution['resolved']} backtest events "
                    f"from game_results"
                )
        except Exception as e:
            logger.warning(f"Backtest resolution failed: {e}")

        backtesting = await self.hypothesis_manager.list_hypotheses(status="backtesting")

        for h in backtesting:
            try:
                result = await self.hypothesis_manager.auto_promote(h["hypothesis_id"])
                action = result.get("action", "held")

                if action == "promoted":
                    self._promotions += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} PROMOTED to "
                        f"{result.get('new_status')}"
                    )
                elif action == "rejected":
                    self._rejections += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} REJECTED — "
                        f"data disproves thesis"
                    )
            except Exception as e:
                logger.warning(
                    f"Evaluation failed for {h['hypothesis_id']}: {e}"
                )

        # Also evaluate paper trading hypotheses
        paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")
        for h in paper:
            try:
                result = await self.hypothesis_manager.auto_promote(h["hypothesis_id"])
                action = result.get("action", "held")
                if action == "promoted":
                    self._promotions += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} PROMOTED TO LIVE"
                    )
                    # Alert Marco — this thesis is proven
                    try:
                        await telegram.send_message(
                            f"HYPOTHESIS PROVEN: {h['name']}\n"
                            f"Thesis: {h['thesis'][:200]}\n"
                            f"Status: LIVE — ready for real money"
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Paper trade eval failed for {h['hypothesis_id']}: {e}")

    async def _phase_paper_trade(self) -> None:
        """Generate paper trade signals for promoted hypotheses.

        Uses DK scraper (free) as primary source for the target book's
        current lines, with Odds API as enrichment for cross-book data.
        This saves API credits while keeping paper trades accurate.
        """
        paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")

        if not paper:
            return

        logger.info(f"Research: paper trading {len(paper)} hypotheses")

        # Cache live odds per sport to avoid redundant API calls
        odds_cache: dict[str, dict] = {}

        for h in paper:
            if not self._running:
                break

            try:
                sport = h["sport"]
                if sport not in odds_cache:
                    # Try DK scraper first (free), then Odds API
                    from tools.dk_scraper import scrape_dk_odds
                    live_odds = await scrape_dk_odds(sport)

                    if live_odds.get("error") or not live_odds.get("games"):
                        from tools.odds_api import get_odds
                        live_odds = await get_odds(
                            sport=sport,
                            regions="us",
                            markets="h2h,spreads,totals",
                        )

                    if not live_odds.get("error"):
                        odds_cache[sport] = live_odds

                live_odds = odds_cache.get(sport)
                if not live_odds:
                    continue

                signals = await self.backtest_engine.generate_paper_trade_signal(
                    hypothesis_id=h["hypothesis_id"],
                    live_odds=live_odds,
                )

                if signals:
                    logger.info(
                        f"Research: {len(signals)} paper trade signals for "
                        f"hypothesis {h['hypothesis_id']}"
                    )
            except Exception as e:
                logger.warning(
                    f"Paper trading failed for {h['hypothesis_id']}: {e}"
                )

    async def _phase_claude_deep_work(self) -> None:
        """
        Karpathy-style: maximize Claude Code throughput.

        If Claude is available and we have budget, run deep analysis tasks:
        - Analyze backtest results for patterns
        - Review and refine hypotheses
        - Generate novel research directions
        - Self-improve: analyze what's working and what isn't
        """
        from tools.claude_code import is_available as claude_available, claude_code_query
        import time as _time

        now = _time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            return
        if not claude_available():
            return

        logger.info("Research: Claude deep work phase — maximizing throughput")

        # Task 1: Analyze what's working in our hypotheses
        all_hypos = await self.hypothesis_manager.list_hypotheses()
        backtesting = [h for h in all_hypos if h["status"] == "backtesting"]
        draft = [h for h in all_hypos if h["status"] == "draft"]

        if backtesting or draft:
            hypo_summary = []
            for h in (backtesting + draft)[:10]:
                hypo_summary.append(f"- [{h['status']}] {h['name']}: {h['thesis'][:100]}")

            data_stats = await self.data_collector.get_collection_stats()

            prompt = (
                f"Callisto autonomous research — deep analysis cycle #{self._cycles}\n\n"
                f"Current hypotheses ({len(all_hypos)} total):\n"
                + "\n".join(hypo_summary) + "\n\n"
                f"Data available: {json.dumps(data_stats, indent=2)}\n\n"
                f"Backtests so far: {self._backtests_run} run, "
                f"{self._promotions} promoted, {self._rejections} rejected\n\n"
                f"TASKS (do all):\n"
                f"1. Analyze the hypothesis list — which are most promising? "
                f"Which should be discarded as untestable with our data?\n"
                f"2. Suggest 3 NOVEL hypotheses we haven't tried that are "
                f"testable with NBA/NFL game results + historical odds data.\n"
                f"3. What data sources should we acquire next to unlock "
                f"higher-value hypotheses? (player props, referee data, "
                f"weather, public betting %, injury reports)\n"
                f"4. Identify any improvements to Callisto's research "
                f"methodology itself — how can the loop be more effective?\n\n"
                f"Be specific and actionable. No general advice."
            )

            try:
                result = await claude_code_query(prompt)
                self._last_claude_call = _time.time()
                self._claude_escalations += 1

                if result.get("content") and not result.get("error"):
                    content = result["content"]
                    logger.info(
                        f"Research: Claude deep analysis complete — "
                        f"{len(content)} chars response"
                    )

                    # Parse any suggested hypotheses and create them
                    # Store the analysis for reference
                    try:
                        await self.data_collector._db.execute(
                            "INSERT INTO game_contexts "
                            "(sport, game_date, home_team, away_team, context, embedded) "
                            "VALUES (?, ?, ?, ?, ?, 1)",
                            (
                                "meta_research",
                                _time.strftime("%Y-%m-%d"),
                                "callisto",
                                "self_analysis",
                                json.dumps({
                                    "type": "claude_deep_analysis",
                                    "cycle": self._cycles,
                                    "analysis": content[:5000],
                                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                }),
                            ),
                        )
                        await self.data_collector._db.commit()
                    except Exception as e:
                        logger.warning(f"Failed to store deep work analysis: {e}")

                elif result.get("rate_limited"):
                    logger.info("Research: Claude rate limited during deep work — will retry next cycle")
            except Exception as e:
                logger.warning(f"Claude deep work failed: {e}")

    def get_status(self) -> dict:
        """Return research loop status."""
        from tools.claude_code import get_usage_stats as claude_stats
        return {
            "running": self._running,
            "cycles_completed": self._cycles,
            "data_collections": self._data_collections,
            "hypotheses_generated": self._hypotheses_generated,
            "backtests_run": self._backtests_run,
            "claude_escalations": self._claude_escalations,
            "promotions": self._promotions,
            "rejections": self._rejections,
            "claude_code": claude_stats(),
            "intervals": {
                "research_cycle_seconds": RESEARCH_CYCLE_INTERVAL,
                "data_collection_seconds": DATA_COLLECTION_INTERVAL,
                "hypothesis_gen_seconds": HYPOTHESIS_GEN_INTERVAL,
                "claude_cooldown_seconds": CLAUDE_ESCALATION_COOLDOWN,
            },
        }
