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
# Lowered from 4%/3% — with 3-5 scraped books, legitimate edges start at 2%
MIN_IMPLIED_RANGE = 0.02       # 2% cross-book disagreement minimum
MIN_SOFT_EDGE_VS_SHARP = 0.02  # 2% vs sharp consensus minimum
MIN_CONFIDENCE_TO_ALERT = 0.40 # Alert at moderate confidence

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
DATA_COLLECTION_INTERVAL = 300      # 5 min between data pulls — fresher data for live edges
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

        # Bulk backfill tracking — one-time 30-day seed when data is thin
        self._bulk_backfill_done = False

        # Counters
        self._cycles = 0
        self._data_collections = 0
        self._hypotheses_generated = 0
        self._backtests_run = 0
        self._claude_escalations = 0
        self._promotions = 0
        self._rejections = 0

        # Self-diagnostics — track already-escalated issues to avoid spam
        self._diagnostic_issues: set[str] = set()

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

                # Phase 0: Self-diagnose pipeline health
                await self._phase_self_diagnose()

                if not self._running:
                    break

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

    async def _phase_self_diagnose(self) -> None:
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
                "SELECT sport, COUNT(*) as cnt, response_json "
                "FROM historical_odds_cache GROUP BY sport"
            )
            rows = await cursor.fetchall()
            for row in rows:
                sport, cnt, sample_json = row[0], row[1], row[2]
                # Sample up to 50 records to estimate avg books
                sample_cursor = await db.execute(
                    "SELECT response_json FROM historical_odds_cache "
                    "WHERE sport = ? ORDER BY RANDOM() LIMIT 50",
                    (sport,),
                )
                samples = await sample_cursor.fetchall()
                total_books = 0
                parsed = 0
                for (rj,) in samples:
                    try:
                        data = json.loads(rj) if isinstance(rj, str) else rj
                        # Odds API format: list of games, each with bookmakers
                        if isinstance(data, list):
                            for game in data:
                                total_books += len(game.get("bookmakers", []))
                                parsed += 1
                        elif isinstance(data, dict):
                            total_books += len(data.get("bookmakers", []))
                            parsed += 1
                    except (json.JSONDecodeError, TypeError):
                        continue
                avg_books = total_books / max(parsed, 1)
                if avg_books < 2:
                    issue_key = f"low_books_{sport}"
                    msg = (
                        f"DIAG: {sport} has avg {avg_books:.1f} books/record "
                        f"({cnt} cached records) — backtests against <2 books are useless"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})
                else:
                    logger.info(
                        f"DIAG: {sport} data quality OK — avg {avg_books:.1f} books/record "
                        f"({cnt} records)"
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
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals "
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
            except Exception:
                # odds_snapshots table may be in a different DB (line_monitor's)
                pass

        except Exception as e:
            logger.warning(f"DIAG: freshness check failed: {e}")

        # ── 4. Escalate critical issues to Claude Code (once per issue) ──
        new_critical = [
            i for i in issues
            if i["severity"] == "CRITICAL" and i["key"] not in self._diagnostic_issues
        ]
        if new_critical:
            from tools.claude_code import is_available as claude_available, claude_code_query

            if claude_available():
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
                try:
                    result = await claude_code_query(diag_report)
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
                # Can't escalate now — don't mark as escalated, try next cycle
                logger.warning(
                    f"DIAG: {len(new_critical)} critical issues but Claude unavailable "
                    f"— will retry next cycle"
                )

        # Mark non-critical issues as seen too (no re-escalation)
        for i in issues:
            if i["severity"] != "CRITICAL":
                self._diagnostic_issues.add(i["key"])

        if not issues:
            logger.info("DIAG: all pipeline health checks passed")

    async def _phase_collect_data(self) -> None:
        """Collect post-game data from ESPN (free).

        Normal cadence: last 7 days every DATA_COLLECTION_INTERVAL.
        Bulk backfill: if game_contexts < 100, one-time 30-day pull to seed the system.
        """
        from datetime import datetime, timedelta, timezone

        now = time.time()
        if now - self._last_data_collect < DATA_COLLECTION_INTERVAL:
            return

        self._last_data_collect = now

        # Determine how far back to collect
        lookback_days = 7  # default: rolling 7-day window

        # One-time bulk backfill when data is thin
        if not self._bulk_backfill_done:
            try:
                stats = await self.data_collector.get_collection_stats()
                total_contexts = sum(
                    row.get("count", 0)
                    for row in stats.get("game_contexts", [])
                )
                if total_contexts < 100:
                    lookback_days = 30
                    logger.info(
                        f"Research: bulk backfill triggered — only {total_contexts} "
                        f"game contexts, collecting last 30 days"
                    )
                else:
                    logger.info(
                        f"Research: {total_contexts} game contexts already present, "
                        f"skipping bulk backfill"
                    )
            except Exception as e:
                logger.warning(f"Could not check collection stats for backfill: {e}")
            self._bulk_backfill_done = True

        logger.info(f"Research: collecting post-game data (last {lookback_days} days)")

        today = datetime.now(timezone.utc)
        dates = [today - timedelta(days=d) for d in range(lookback_days)]

        for sport in RESEARCH_SPORTS:
            try:
                for dt in dates:
                    date_str = dt.strftime("%Y%m%d")
                    scores = await self.data_collector.collect_scores(sport, date_str)
                    if scores.get("completed", 0) > 0:
                        await self.data_collector.collect_box_scores(sport, date_str)

                # Resolve pending paper trades for the same window
                for dt in dates:
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

        Claude is NOT used for generic analysis text. Every call must produce
        ACTIONABLE output: hypotheses to create, hypotheses to reject, or
        specific pipeline fixes. If it can't act, it shouldn't call.
        """
        from tools.claude_code import is_available as claude_available, claude_code_query
        import time as _time

        now = _time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            return
        if not claude_available():
            return

        logger.info("Research: Claude deep work phase — actionable output only")

        # Gather pipeline metrics for Claude
        db = self.data_collector._db
        metrics = {}
        try:
            # Backtest signal rate
            row = await db.execute_fetchone(
                "SELECT COUNT(*) total, SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) signals "
                "FROM backtest_events"
            ) if hasattr(db, 'execute_fetchone') else None
            if not row:
                cursor = await db.execute(
                    "SELECT COUNT(*) total, SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) signals "
                    "FROM backtest_events"
                )
                row = await cursor.fetchone()
            metrics["bt_events"] = row[0] if row else 0
            metrics["bt_signals"] = row[1] if row else 0

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

        # Get top backtesting hypotheses by signal count
        top_hypos = []
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name, h.thesis, h.sport,
                       COUNT(CASE WHEN be.signal_generated=1 THEN 1 END) as sigs,
                       COUNT(*) as events,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge
                FROM hypotheses h
                LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status = 'backtesting'
                GROUP BY h.hypothesis_id
                ORDER BY sigs DESC, events DESC
                LIMIT 15
            """)
            for r in await cursor.fetchall():
                top_hypos.append(f"  {r[1]} [{r[3]}]: {r[4]} signals / {r[5]} events, avg_edge={r[6] or 0:.4f}")
        except Exception:
            pass

        prompt = (
            f"CALLISTO AUTONOMOUS SYSTEM — DEEP WORK CYCLE #{self._cycles}\n"
            f"You are the reasoning engine for an autonomous research system.\n"
            f"Your output MUST be structured JSON that the system can parse and act on.\n\n"
            f"PIPELINE STATE:\n"
            f"  Backtest events: {metrics.get('bt_events', 0)} total, "
            f"{metrics.get('bt_signals', 0)} signals ({(metrics.get('bt_signals',0) / max(1,metrics.get('bt_events',1))) * 100:.1f}%)\n"
            f"  Hypothesis status: {json.dumps(metrics.get('hypo_status', {}))}\n"
            f"  Odds cache: {json.dumps(metrics.get('odds_cache', {}))}\n"
            f"  Promotions: {self._promotions} | Rejections: {self._rejections}\n"
            f"  Cycles: {self._cycles} | Data collections: {self._data_collections}\n\n"
            f"TOP HYPOTHESES BY SIGNALS:\n"
            + ("\n".join(top_hypos) if top_hypos else "  (none with signals)") + "\n\n"
            f"RESPOND WITH EXACTLY THIS JSON STRUCTURE (no other text):\n"
            f'{{"reject_ids": ["hypothesis_id1", ...], '
            f'"promising_sports": ["sport1", ...], '
            f'"new_hypotheses": [{{"name": "...", "thesis": "...", "sport": "...", '
            f'"market_type": "...", "edge_threshold": 0.03}}], '
            f'"pipeline_issues": ["issue description", ...]}}\n\n'
            f"RULES:\n"
            f"- reject_ids: hypotheses with 0 signals after 50+ events (data disproves them)\n"
            f"- new_hypotheses: 3-5 NOVEL, testable with NBA/NFL game-level odds data\n"
            f"- pipeline_issues: specific, actionable problems you observe in the metrics\n"
            f"- Only include fields you have actionable items for\n"
        )

        try:
            result = await claude_code_query(prompt)
            self._last_claude_call = _time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                logger.info(f"Research: Claude deep work response — {len(content)} chars")

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
                        except Exception:
                            pass
                    if rejected:
                        logger.info(f"Research: Claude rejected {rejected} hopeless hypotheses")

                    # Act 2: Create new hypotheses
                    created = 0
                    for nh in actions.get("new_hypotheses", []):
                        try:
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", "claude_generated"),
                                thesis=nh.get("thesis", ""),
                                sport=nh.get("sport", "basketball_nba"),
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.03),
                                model_config={"source": "claude_deep_work", "cycle": self._cycles},
                            )
                            created += 1
                        except Exception:
                            pass
                    if created:
                        self._hypotheses_generated += created
                        logger.info(f"Research: Claude created {created} new hypotheses")

                    # Act 3: Log pipeline issues for next self-diagnostic
                    for issue in actions.get("pipeline_issues", []):
                        logger.warning(f"Research: Claude identified issue — {issue}")

                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Claude deep work returned non-JSON: {e}")
                    # Still store the raw analysis as fallback
                    try:
                        await db.execute(
                            "INSERT INTO game_contexts "
                            "(sport, game_date, home_team, away_team, context, embedded) "
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
                    except Exception:
                        pass

            elif result.get("rate_limited"):
                logger.info("Research: Claude rate limited — will retry next cycle")
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
