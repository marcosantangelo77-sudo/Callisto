"""
Autonomous reasoning loop — makes Callisto think without being asked.

Two loops run concurrently:
  1. AutonomousLoop — real-time edge detection (existing, unchanged)
  2. ResearchLoop — 24/7 hypothesis machine (NEW)

ResearchLoop cycle:
  - Collect post-game data (ESPN scores, box scores) — FREE
  - Embed game contexts and prop outcomes into vector store
  - Generate hypotheses (Claude Code PRIMARY, templates FALLBACK)
  - Backtest hypotheses against historical data
  - Evaluate significance, auto-promote or auto-reject
  - Claude interprets backtest results (signal vs noise, threshold mods)
  - Paper trade promoted hypotheses on live odds
  - Claude deep analysis — actionable hypothesis/rejection work
  - System self-improvement (every 10 cycles) — pipeline optimization

Claude Code is the PRIMARY reasoning engine. Local models stay only
for fast classification (Sentinel) and embeddings.
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
HYPOTHESIS_GEN_INTERVAL = 120       # 2 min between hypothesis generation — Claude drives, smaller batches
BACKTEST_BATCH_SIZE = 20            # Hypotheses to backtest per cycle — higher throughput
CLAUDE_ESCALATION_COOLDOWN = 10     # 10s cooldown — 45 calls/hr means ~80s natural spacing, let rate limiter govern
SYSTEM_IMPROVEMENT_INTERVAL = 10    # Run system improvement every N cycles

# Domains to research (ordered by data availability)
RESEARCH_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "basketball_ncaaw",
    "icehockey_nhl",
    "baseball_mlb",
    "golf_pga",
]


class ResearchLoop:
    """
    24/7 autonomous research engine — Claude Code is the primary reasoning engine.

    Runs independently of AutonomousLoop. While AutonomousLoop handles
    real-time edge detection and alerting, ResearchLoop handles the
    slow, deep work: collecting data, discovering patterns, generating
    and testing hypotheses, interpreting results, and self-improving.

    Claude Code drives hypothesis generation, backtest interpretation,
    and system improvement. Local models handle fast classification
    (Sentinel) and embeddings only. Template generation is the fallback
    when Claude is rate-limited.
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

                # Phase 5b: Claude interprets backtest results (signal vs noise)
                await self._phase_interpret_backtests()

                if not self._running:
                    break

                # Phase 6: Paper trade active hypotheses
                await self._phase_paper_trade()

                if not self._running:
                    break

                # Phase 6b: Execute live bets on proven hypotheses
                await self._phase_live_execute()

                if not self._running:
                    break

                # Phase 7: Claude deep analysis — use remaining budget
                await self._phase_claude_deep_work()

                if not self._running:
                    break

                # Phase 8: System self-improvement (every N cycles)
                await self._phase_system_improvement()

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
        """Generate new hypotheses — Claude Code PRIMARY, templates FALLBACK.

        Claude Code is the primary hypothesis generator. Every cycle where
        Claude is available, we ask it to generate hypotheses based on current
        pipeline state, data stats, and what hasn't been tried. Template
        generation is the fallback when Claude is rate-limited.
        """
        now = time.time()
        if now - self._last_hypothesis_gen < HYPOTHESIS_GEN_INTERVAL:
            return

        logger.info("Research: generating hypotheses (Claude-primary)")
        self._last_hypothesis_gen = now

        total_created = 0
        used_claude = False

        # ── PRIMARY: Claude Code hypothesis generation ──
        from tools.claude_code import is_available as claude_available, claude_code_query

        if (now - self._last_claude_call > CLAUDE_ESCALATION_COOLDOWN
                and claude_available()):
            try:
                # Gather context for Claude
                all_hypos = await self.hypothesis_manager.list_hypotheses()
                existing_names = [h["name"] for h in all_hypos]
                draft_count = sum(1 for h in all_hypos if h["status"] == "draft")
                active_count = sum(
                    1 for h in all_hypos
                    if h["status"] in ("backtesting", "paper_trading", "live")
                )
                rejected_count = sum(1 for h in all_hypos if h["status"] == "rejected")

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
                    except Exception:
                        pass

                prompt = (
                    f"CALLISTO HYPOTHESIS GENERATION — Cycle #{self._cycles}\n\n"
                    f"You are the primary reasoning engine for an autonomous sports betting "
                    f"research system. Generate novel, testable hypotheses.\n\n"
                    f"PIPELINE STATE:\n"
                    f"  Total hypotheses: {len(all_hypos)} "
                    f"({draft_count} draft, {active_count} active, {rejected_count} rejected)\n"
                    f"  Sports: {', '.join(RESEARCH_SPORTS)}\n"
                    f"  Data ranges: {json.dumps(date_ranges)}\n"
                    f"  Collection stats: {json.dumps(data_stats)}\n"
                    f"  Model: consensus devig (power method) across 3+ books\n\n"
                    f"EXISTING HYPOTHESIS NAMES (avoid duplicates):\n"
                    f"  {json.dumps(existing_names[:50])}\n\n"
                    f"FOCUS AREAS (edges that persist hours, not speed arb):\n"
                    f"  - rest days, travel, altitude, back-to-backs\n"
                    f"  - referee tendencies, scheme matchups\n"
                    f"  - public betting % vs sharp money\n"
                    f"  - weather, revenge games, divisional rivalry\n"
                    f"  - line movement timing, closing line value patterns\n"
                    f"  - player prop mispricing (over/under on stats)\n"
                    f"  - situational factors books underweight\n\n"
                    f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
                    f'{{"hypotheses": [\n'
                    f'  {{"name": "unique_snake_case_name", '
                    f'"thesis": "Clear testable statement", '
                    f'"sport": "basketball_nba", '
                    f'"market_type": "spreads|totals|h2h|player_props", '
                    f'"edge_threshold": 0.03}}\n'
                    f"]}}\n\n"
                    f"RULES:\n"
                    f"- Generate 3-5 hypotheses per call\n"
                    f"- Each must be testable with game-level odds data we have\n"
                    f"- Names must be unique (not in existing list)\n"
                    f"- Thesis must be specific and falsifiable\n"
                    f"- Prefer sports with the most data (check date_ranges)\n"
                )

                result = await claude_code_query(prompt)
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
                                await self.hypothesis_manager.create_hypothesis(
                                    name=nh.get("name", f"claude_gen_{self._cycles}"),
                                    thesis=nh.get("thesis", ""),
                                    sport=nh.get("sport", "basketball_nba"),
                                    market_type=nh.get("market_type", "spreads"),
                                    edge_threshold=nh.get("edge_threshold", 0.03),
                                    model_config={
                                        "source": "claude_primary_gen",
                                        "cycle": self._cycles,
                                    },
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

        # ── FALLBACK: Template-based generation when Claude unavailable ──
        if not used_claude:
            logger.info("Research: using template fallback for hypothesis generation")
            for sport in RESEARCH_SPORTS:
                try:
                    created = await self.hypothesis_generator.generate_from_templates(
                        sport=sport, max_hypotheses=20,
                    )
                    total_created += len(created)
                except Exception as e:
                    logger.warning(f"Template generation failed for {sport}: {e}")

        self._hypotheses_generated += total_created
        logger.info(f"Research: generated {total_created} new hypotheses")

    async def _phase_backtest(self) -> None:
        """Backtest draft hypotheses that are ready."""
        # Get draft hypotheses that haven't been backtested
        drafts = await self.hypothesis_manager.list_hypotheses(status="draft")

        if not drafts:
            return

        # Pre-check which sports have usable odds (>=2 books)
        sports_with_odds = set()
        try:
            db = self.data_collector._db
            if db:
                cursor = await db.execute(
                    "SELECT DISTINCT sport FROM historical_odds_cache"
                )
                for (sport,) in await cursor.fetchall():
                    # Quick sample: does this sport have any multi-book records?
                    check = await db.execute(
                        "SELECT response_json FROM historical_odds_cache "
                        "WHERE sport = ? ORDER BY RANDOM() LIMIT 5",
                        (sport,),
                    )
                    for (rj,) in await check.fetchall():
                        try:
                            data = json.loads(rj) if isinstance(rj, str) else rj
                            games = data.get("games", []) if isinstance(data, dict) else data
                            for g in games:
                                if len(g.get("bookmakers", [])) >= 2:
                                    sports_with_odds.add(sport)
                                    break
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if sport in sports_with_odds:
                            break
        except Exception as e:
            logger.warning(f"Data quality pre-check failed: {e}")

        # Backtest up to BACKTEST_BATCH_SIZE per cycle
        to_test = drafts[:BACKTEST_BATCH_SIZE]
        logger.info(f"Research: backtesting {len(to_test)} hypotheses")

        from datetime import datetime, timedelta, timezone

        for h in to_test:
            if not self._running:
                break

            sport = h.get("sport", "")
            market = h.get("market_type", "")

            # Player prop hypotheses can't be backtested (no historical prop data).
            # Skip backtesting and promote directly to paper_trading for live evaluation.
            if market.startswith("player_"):
                try:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "paper_trading", "auto:no_historical_prop_data"
                    )
                    self._backtests_run += 1
                    logger.info(
                        f"Research: promoted {h['hypothesis_id']} ({market}) "
                        f"directly to paper_trading — no historical prop data for backtesting"
                    )
                except Exception as e:
                    logger.warning(f"Failed to promote prop hypothesis {h['hypothesis_id']}: {e}")
                continue

            # Skip hypotheses for sports with no usable multi-book data
            if sports_with_odds and sport not in sports_with_odds:
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"{sport} has no multi-book odds data yet"
                )
                continue

            try:
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

    async def _phase_live_execute(self) -> None:
        """Execute bets on live (proven) hypotheses using the bet executor.

        Scans live odds for signals matching live hypotheses, then places
        real bets via Playwright browser automation on DraftKings.
        Only runs if the executor is enabled and logged in.
        """
        try:
            from tools.bet_executor import BetExecutor
        except ImportError:
            return

        # Check if executor is available (initialized externally)
        executor = getattr(self, "_bet_executor", None)
        if not executor or not executor.is_enabled:
            return

        live = await self.hypothesis_manager.list_hypotheses(status="live")
        if not live:
            return

        logger.info(f"Research: scanning {len(live)} live hypotheses for bet signals")

        # Cache live odds per sport
        odds_cache: dict[str, dict] = {}

        for h in live:
            if not self._running:
                break

            try:
                sport = h["sport"]
                market = h.get("market_type", "")

                # Get live odds (DK scraper for game-level, Odds API for props)
                if sport not in odds_cache:
                    if market.startswith("player_"):
                        from tools.odds_api import get_odds
                        odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
                    else:
                        from tools.dk_scraper import scrape_dk_odds
                        odds_data = await scrape_dk_odds(sport)
                        if odds_data.get("error") or not odds_data.get("games"):
                            from tools.odds_api import get_odds
                            odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")

                    if not odds_data.get("error"):
                        odds_cache[sport] = odds_data

                odds_data = odds_cache.get(sport)
                if not odds_data:
                    continue

                # Generate signals using the backtest engine's paper trade logic
                signals = await self.backtest_engine.generate_paper_trade_signal(
                    hypothesis_id=h["hypothesis_id"],
                    live_odds=odds_data,
                )

                if not signals:
                    continue

                # Execute each signal
                for signal in signals:
                    if not self._running:
                        break

                    result = await executor.execute_bet(
                        sport=sport,
                        team=signal.get("team", ""),
                        market=signal.get("market", market),
                        side=signal.get("side", ""),
                        odds=signal.get("book_odds_american", 0),
                        fair_prob=signal.get("model_fair_prob", 0.5),
                        edge=signal.get("edge", 0),
                        hypothesis_id=h["hypothesis_id"],
                        event_id=signal.get("event_id", ""),
                        game_description=signal.get("game_description", ""),
                    )

                    if result.get("success"):
                        logger.info(
                            f"LIVE BET PLACED: {signal.get('team')} "
                            f"${result.get('stake', 0):.2f} @ {signal.get('book_odds_american')}"
                        )
                    else:
                        logger.warning(
                            f"Live bet failed: {result.get('reason', 'unknown')}"
                        )

            except Exception as e:
                logger.warning(f"Live execution failed for {h['hypothesis_id']}: {e}")

    async def _phase_interpret_backtests(self) -> None:
        """Claude interprets backtest results — signal vs noise, modifications.

        Sends the top 10 hypotheses by signal count with their win/loss/edge
        stats to Claude for interpretation. Claude identifies genuine signals,
        rejects noise, and suggests threshold modifications.
        """
        from tools.claude_code import is_available as claude_available, claude_code_query

        now = time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            return
        if not claude_available():
            return

        db = self.data_collector._db
        if not db:
            return

        # Get top 10 hypotheses by signal count with stats
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name, h.thesis, h.sport, h.market_type,
                       h.edge_threshold, h.status,
                       COUNT(CASE WHEN be.signal_generated=1 THEN 1 END) as sigs,
                       COUNT(*) as events,
                       SUM(CASE WHEN be.actual_result='win' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN be.actual_result='loss' THEN 1 ELSE 0 END) as losses,
                       SUM(CASE WHEN be.actual_result='push' THEN 1 ELSE 0 END) as pushes,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.ev_pct END) as avg_ev
                FROM hypotheses h
                LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status IN ('backtesting', 'paper_trading')
                GROUP BY h.hypothesis_id
                HAVING events > 0
                ORDER BY sigs DESC, events DESC
                LIMIT 10
            """)
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"Failed to query backtest stats for interpretation: {e}")
            return

        if not rows:
            logger.info("Research: no hypotheses with backtest data for interpretation")
            return

        # Format hypothesis data for Claude
        hypo_data = []
        for r in rows:
            h_id, name, thesis, sport, mkt, thresh, status = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
            sigs, events, wins, losses, pushes = r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0, r[11] or 0
            avg_edge, avg_ev = r[12] or 0, r[13] or 0
            resolved = wins + losses + pushes
            hit_rate = wins / max(resolved, 1)
            hypo_data.append({
                "id": h_id, "name": name, "thesis": thesis[:200],
                "sport": sport, "market": mkt, "threshold": thresh,
                "status": status, "signals": sigs, "events": events,
                "wins": wins, "losses": losses, "pushes": pushes,
                "hit_rate": round(hit_rate, 4),
                "avg_edge": round(avg_edge, 5),
                "avg_ev": round(avg_ev, 5),
            })

        prompt = (
            f"CALLISTO BACKTEST INTERPRETATION — Cycle #{self._cycles}\n\n"
            f"You are analyzing backtest results for an autonomous betting research system.\n"
            f"Determine which hypotheses show genuine signal vs random noise.\n\n"
            f"HYPOTHESIS BACKTEST RESULTS (top 10 by signal count):\n"
            f"{json.dumps(hypo_data, indent=2)}\n\n"
            f"STATISTICAL CONTEXT:\n"
            f"- A fair coin has ~50% hit rate. Signal needs to beat that consistently.\n"
            f"- With <30 resolved bets, results are mostly noise (wide confidence intervals).\n"
            f"- avg_edge > 0.03 with hit_rate > 0.53 over 50+ resolved is promising.\n"
            f"- 0 signals after 50+ events means the hypothesis never fires — reject it.\n"
            f"- Low signal rate (<5%) with poor hit rate means the threshold may be wrong.\n\n"
            f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
            f'{{"reject": ["hypothesis_id1", "hypothesis_id2"], '
            f'"modify": [{{"id": "hypothesis_id", "new_threshold": 0.025, "reason": "..."}}], '
            f'"insights": "Brief analysis of what patterns are working and what isn\'t"}}\n\n'
            f"RULES:\n"
            f"- reject: hypotheses that are clearly noise (0 signals, or terrible hit rate with enough data)\n"
            f"- modify: promising hypotheses that need threshold adjustments\n"
            f"- Only include fields you have actionable items for\n"
        )

        try:
            result = await claude_code_query(prompt)
            self._last_claude_call = time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                try:
                    # Extract JSON
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

                    # Act: Reject noise hypotheses
                    rejected = 0
                    for hid in actions.get("reject", []):
                        try:
                            await self.hypothesis_manager.update_status(
                                hid, "rejected", "claude_interpret_backtests"
                            )
                            rejected += 1
                            self._rejections += 1
                        except Exception:
                            pass
                    if rejected:
                        logger.info(
                            f"Research: Claude interpretation rejected {rejected} "
                            f"noise hypotheses"
                        )

                    # Act: Modify thresholds for promising hypotheses
                    modified = 0
                    for mod in actions.get("modify", []):
                        try:
                            hid = mod.get("id")
                            new_thresh = mod.get("new_threshold")
                            reason = mod.get("reason", "claude_threshold_adjust")
                            if hid and new_thresh is not None:
                                await db.execute(
                                    "UPDATE hypotheses SET edge_threshold = ?, "
                                    "notes = COALESCE(notes, '') || ? "
                                    "WHERE hypothesis_id = ?",
                                    (
                                        new_thresh,
                                        f"\n[cycle {self._cycles}] threshold adjusted "
                                        f"to {new_thresh}: {reason}",
                                        hid,
                                    ),
                                )
                                await db.commit()
                                modified += 1
                        except Exception:
                            pass
                    if modified:
                        logger.info(
                            f"Research: Claude modified thresholds on {modified} hypotheses"
                        )

                    # Log insights
                    insights = actions.get("insights", "")
                    if insights:
                        logger.info(f"Research: Claude backtest insights — {insights[:300]}")

                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Claude interpretation response not valid JSON: {e}")

            elif result.get("rate_limited"):
                logger.info("Research: Claude rate-limited during backtest interpretation")
        except Exception as e:
            logger.warning(f"Claude backtest interpretation failed: {e}")

    async def _phase_paper_trade(self) -> None:
        """Generate paper trade signals for promoted hypotheses.

        Uses DK scraper (free) as primary source for the target book's
        current lines, with Odds API as enrichment for cross-book data.
        This saves API credits while keeping paper trades accurate.
        """
        from datetime import datetime, timezone

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
                market = h.get("market_type", "")

                # For player props: use Odds API prop scanner (DK scraper has no props)
                if market.startswith("player_"):
                    from tools.prop_scanner import scan_props_ev
                    from tools.odds_api import get_odds
                    import uuid as _uuid
                    # Get upcoming games for this sport
                    if sport not in odds_cache:
                        live_odds = await get_odds(
                            sport=sport, regions="us", markets="h2h",
                        )
                        if not live_odds.get("error"):
                            odds_cache[sport] = live_odds
                    games = odds_cache.get(sport, {}).get("games", [])
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for game in games[:3]:  # Limit to 3 games to conserve credits
                        event_id = game.get("id")
                        if not event_id:
                            continue
                        try:
                            result = await scan_props_ev(
                                sport=sport,
                                event_id=event_id,
                                target_book="draftkings",
                                edge_threshold=h["edge_threshold"],
                                prop_markets=market,
                            )
                            edges = result.get("edges", [])
                            if edges:
                                logger.info(
                                    f"Research: {len(edges)} prop edges for "
                                    f"{h['hypothesis_id']} in game {event_id}"
                                )
                                # Record each edge as a paper trade
                                db = self.data_collector._db
                                for edge_info in edges:
                                    trade_id = str(_uuid.uuid4())[:12]
                                    await db.execute(
                                        "INSERT OR IGNORE INTO paper_trades "
                                        "(trade_id, hypothesis_id, event_id, sport, player, market, "
                                        "line, side, book, signal_time, signal_odds_american, "
                                        "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                                        "kelly_fraction, game_date) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            trade_id,
                                            h["hypothesis_id"],
                                            event_id,
                                            sport,
                                            edge_info.get("player"),
                                            market,
                                            edge_info.get("line"),
                                            edge_info.get("side", ""),
                                            "draftkings",
                                            now_iso,
                                            edge_info.get("target_price", 0),
                                            edge_info.get("target_implied", 0),
                                            edge_info.get("fair_probability", 0),
                                            round(edge_info.get("edge_pct", 0) / 100, 6),
                                            round(edge_info.get("ev_per_100", 0) / 100, 6),
                                            edge_info.get("kelly_fraction", 0),
                                            today,
                                        ),
                                    )
                                await db.commit()
                        except Exception as e:
                            logger.debug(f"Prop scan failed for {event_id}: {e}")
                    continue

                # For game-level markets: use DK scraper (free), Odds API as fallback
                if sport not in odds_cache:
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

    async def _phase_system_improvement(self) -> None:
        """Self-improvement phase — runs every SYSTEM_IMPROVEMENT_INTERVAL cycles.

        Asks Claude to review pipeline metrics and suggest specific code
        improvements. Stores suggestions in a system_improvements table.
        This is how the system learns to improve itself over time.
        """
        if self._cycles % SYSTEM_IMPROVEMENT_INTERVAL != 0:
            return

        from tools.claude_code import is_available as claude_available, claude_code_query

        now = time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
            return
        if not claude_available():
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
                "SUM(CASE WHEN actual_result='win' THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN actual_result='loss' THEN 1 ELSE 0 END) losses, "
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
            f"You are reviewing the autonomous research pipeline to suggest "
            f"specific, implementable improvements.\n\n"
            f"PIPELINE METRICS:\n{json.dumps(metrics, indent=2)}\n\n"
            f"RECENT SUGGESTIONS (already made, avoid repeating):\n"
            f"{json.dumps(metrics.get('recent_suggestions', []))}\n\n"
            f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
            f'{{"improvements": [\n'
            f'  {{"category": "data_collection|hypothesis_gen|backtesting|evaluation|infrastructure", '
            f'"suggestion": "Specific actionable improvement", '
            f'"priority": "high|medium|low", '
            f'"rationale": "Why this would help based on the metrics"}}\n'
            f"]}}\n\n"
            f"RULES:\n"
            f"- 2-4 suggestions per review\n"
            f"- Each must be specific and implementable (not vague)\n"
            f"- Prioritize based on biggest impact on pipeline throughput\n"
            f"- Focus on: signal rate, data quality, hypothesis diversity, "
            f"promotion rate, resolution rate\n"
            f"- Do NOT repeat recent suggestions\n"
        )

        try:
            result = await claude_code_query(prompt)
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
                        except Exception:
                            pass
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
