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
import os
import time
from datetime import datetime, timedelta, timezone
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
                logger.error(f"Autonomous loop error: {e}", exc_info=True)
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
            logger.error(f"Autonomous: session failed for {team} {market}: {e}", exc_info=True)

    def _cleanup_dedup(self) -> None:
        """Remove old entries from the dedup cache."""
        now = time.time()
        expired = [
            k for k, t in self._analyzed_edges.items()
            if now - t > EDGE_DEDUP_WINDOW * 1.5
        ]
        for k in expired:
            del self._analyzed_edges[k]
        # Hard cap: if cache grows beyond 500 entries, keep only newest 250
        if len(self._analyzed_edges) > 500:
            sorted_keys = sorted(self._analyzed_edges, key=self._analyzed_edges.get)
            for k in sorted_keys[:len(sorted_keys) - 250]:
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

# ── Temporal isolation defaults ──
# Hypotheses train on data before the cutoff, backtest on data after.
# This prevents look-ahead bias / circular testing.
DEFAULT_TRAINING_WINDOW_DAYS = 30    # Train on everything before (today - N days)
BACKTEST_GAP_DAYS = 1                # Gap between training end and backtest start (avoids leakage)

# ── Sport priority for backtest queue ──
# Sports with more historical data get tested first.
# This ensures NBA/NFL hypotheses (abundant data) are validated before
# MLB (season just started, sparse data). Lower number = higher priority.
SPORT_PRIORITY = {
    "basketball_nba": 1,
    "americanfootball_nfl": 2,
    "icehockey_nhl": 3,
    "baseball_mlb": 4,
    "basketball_ncaab": 5,
    "basketball_ncaaw": 6,
    "basketball_wnba": 7,
    "golf_pga": 8,
}

# Domains to research (ordered by data availability)
RESEARCH_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "basketball_ncaaw",
    "basketball_wnba",
    "icehockey_nhl",
    "baseball_mlb",
    "golf_pga",
]

# ── Research Focus Areas ──
# Priority sports/topics that the loop should preferentially work on.
# These are the defaults; they are overridden by DB-persisted focus areas
# once loaded. Use the /research/focus API to update at runtime.
DEFAULT_FOCUS_AREAS = [
    {"sport": "baseball_mlb", "priority": 1, "subtopic": None, "reason": "Season starting, time-sensitive"},
    {"sport": "golf_pga", "priority": 1, "subtopic": "masters", "reason": "Masters in April, time-sensitive"},
    {"sport": "basketball_ncaaw", "priority": 1, "subtopic": "identity_cohesion", "reason": "Core thesis, thin markets"},
    {"sport": "basketball_wnba", "priority": 2, "subtopic": "identity_cohesion", "reason": "Core thesis extension"},
]


class FocusAreaManager:
    """Manages research focus areas — loads from DB, provides sorting helpers."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        self._focus_areas: list[dict] = list(DEFAULT_FOCUS_AREAS)
        self._loaded = False

    async def load_from_db(self) -> None:
        """Load focus areas from the database. Falls back to defaults if empty."""
        import aiosqlite
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT sport, priority, subtopic, reason FROM research_focus_areas "
                    "WHERE active = 1 ORDER BY priority ASC"
                )
                rows = await cursor.fetchall()
                if rows:
                    self._focus_areas = [
                        {"sport": r[0], "priority": r[1], "subtopic": r[2], "reason": r[3]}
                        for r in rows
                    ]
                    logger.info(f"Focus areas loaded from DB: {len(self._focus_areas)} active")
                else:
                    # Seed defaults into DB on first load
                    await self._seed_defaults(db)
                    logger.info(f"Focus areas seeded from defaults: {len(self._focus_areas)}")
                self._loaded = True
        except Exception as e:
            logger.warning(f"Failed to load focus areas from DB, using defaults: {e}")
            self._focus_areas = list(DEFAULT_FOCUS_AREAS)

    async def _seed_defaults(self, db) -> None:
        """Insert default focus areas into the DB."""
        for fa in DEFAULT_FOCUS_AREAS:
            await db.execute(
                "INSERT OR IGNORE INTO research_focus_areas (sport, priority, subtopic, reason) "
                "VALUES (?, ?, ?, ?)",
                (fa["sport"], fa["priority"], fa.get("subtopic"), fa.get("reason")),
            )
        await db.commit()

    async def get_focus_areas(self) -> list[dict]:
        """Return current focus areas, loading from DB if needed."""
        if not self._loaded:
            await self.load_from_db()
        return list(self._focus_areas)

    async def set_focus_areas(self, areas: list[dict]) -> list[dict]:
        """Replace all focus areas with new ones. Persists to DB."""
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            # Deactivate all existing
            await db.execute("UPDATE research_focus_areas SET active = 0")
            # Insert new ones
            for fa in areas:
                await db.execute(
                    "INSERT INTO research_focus_areas (sport, priority, subtopic, reason, active) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (fa["sport"], fa.get("priority", 1), fa.get("subtopic"), fa.get("reason")),
                )
            await db.commit()
        self._focus_areas = list(areas)
        logger.info(f"Focus areas updated: {len(areas)} areas set")
        return self._focus_areas

    def get_focus_sport_priority(self, sport: str) -> int:
        """Return the priority for a sport (lower = higher priority). 999 if not a focus area."""
        for fa in self._focus_areas:
            if fa["sport"] == sport:
                return fa["priority"]
        return 999

    def is_focus_sport(self, sport: str) -> bool:
        """Return True if the sport is in the current focus areas."""
        return any(fa["sport"] == sport for fa in self._focus_areas)

    def get_focus_sports(self) -> list[str]:
        """Return list of focus sport keys, ordered by priority."""
        sorted_areas = sorted(self._focus_areas, key=lambda x: x.get("priority", 999))
        return [fa["sport"] for fa in sorted_areas]

    def sort_by_focus(self, items: list[dict], sport_key: str = "sport") -> list[dict]:
        """Sort a list of dicts by focus area priority (focus sports first, then the rest)."""
        def sort_key(item):
            sport = item.get(sport_key, "")
            focus_priority = self.get_focus_sport_priority(sport)
            return focus_priority
        return sorted(items, key=sort_key)

    def get_ordered_research_sports(self) -> list[str]:
        """Return RESEARCH_SPORTS reordered: focus sports first by priority, then the rest."""
        focus_sports = self.get_focus_sports()
        # Focus sports first (in priority order), then remaining RESEARCH_SPORTS
        ordered = []
        for fs in focus_sports:
            if fs in RESEARCH_SPORTS and fs not in ordered:
                ordered.append(fs)
            elif fs not in RESEARCH_SPORTS and fs not in ordered:
                # Focus area sport not in default list — add it
                ordered.append(fs)
        for rs in RESEARCH_SPORTS:
            if rs not in ordered:
                ordered.append(rs)
        return ordered

    def get_focus_context_for_prompt(self) -> str:
        """Generate a text description of focus areas for Claude prompts."""
        if not self._focus_areas:
            return ""
        lines = ["PRIORITY FOCUS AREAS (generate hypotheses for these FIRST):"]
        for fa in sorted(self._focus_areas, key=lambda x: x.get("priority", 999)):
            subtopic = f" [{fa['subtopic']}]" if fa.get("subtopic") else ""
            reason = f" — {fa['reason']}" if fa.get("reason") else ""
            lines.append(f"  P{fa['priority']}: {fa['sport']}{subtopic}{reason}")
        return "\n".join(lines)


# Global focus area manager instance
focus_manager = FocusAreaManager()


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

    NEVER IDLE: When Claude is unavailable, work is deferred to a
    persistent queue AND local model fallbacks keep the loop productive.
    When Claude returns, the deferred queue drains immediately.
    """

    def __init__(
        self,
        hypothesis_manager,
        hypothesis_generator,
        backtest_engine,
        data_collector,
        vector_store,
        orchestrator=None,
        focus_area_manager: "FocusAreaManager | None" = None,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.hypothesis_generator = hypothesis_generator
        self.backtest_engine = backtest_engine
        self.data_collector = data_collector
        self.vector_store = vector_store
        self.orchestrator = orchestrator
        self.focus_manager = focus_area_manager or focus_manager

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

        # Deferred work queue + downtime tracker (never-idle loop)
        from tools.work_queue import get_work_queue, get_downtime_tracker
        self._work_queue = get_work_queue()
        self._downtime_tracker = get_downtime_tracker()
        self._was_claude_available = True  # track transitions

    async def start(self) -> None:
        """Start the research loop."""
        if self._running:
            return
        self._running = True
        # Load focus areas from DB before starting the loop
        await self.focus_manager.load_from_db()
        focus_sports = self.focus_manager.get_focus_sports()
        logger.info(f"Research focus areas loaded: {focus_sports}")
        # One-time backfill of temporal metadata on legacy hypotheses
        await self._backfill_temporal_metadata()
        # One-time: lower edge_thresholds that are too high (real edges cap at ~2.5%)
        await self._migrate_edge_thresholds()
        # One-time: requeue hypotheses falsely rejected by high-threshold bug
        await self._requeue_threshold_rejections()
        self._task = asyncio.create_task(self._loop())
        logger.info("Research loop started — autonomous hypothesis machine online")

    async def _backfill_temporal_metadata(self) -> None:
        """Backfill training_period_end on legacy hypotheses that lack temporal metadata.

        Sets reasonable defaults so the backtest engine can enforce temporal isolation
        on the 231 hypotheses created before the temporal split system existed.
        """
        db = self.hypothesis_manager._db
        if db is None:
            logger.warning("Cannot backfill temporal metadata — hypothesis DB not initialized")
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE model_config NOT LIKE '%training_period_end%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Temporal metadata backfill: no legacy hypotheses need updating")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            config["training_period_end"] = "2026-02-22"
            config["training_period_start"] = "2023-01-01"
            config["temporal_split_gap_days"] = 7

            await db.execute(
                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Temporal metadata backfill complete: updated {count} legacy hypotheses "
            f"(training_period_end=2026-02-22, training_period_start=2023-01-01, gap=7d)"
        )

    async def _migrate_edge_thresholds(self) -> None:
        """One-time migration: lower edge_thresholds that exceed real market edge range.

        Real market edges top out at ~2.5%. Hypotheses with thresholds at 3%+
        will NEVER fire signals, causing false rejections. Lower to 1.5% so
        the backtest engine can actually detect edges.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0

        if count == 0:
            logger.info("Edge threshold migration: no hypotheses need lowering")
            return

        await db.execute(
            "UPDATE hypotheses SET edge_threshold = 0.015 "
            "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
        )
        await db.commit()
        logger.info(
            f"Edge threshold migration: lowered {count} hypotheses from ≥2.5% to 1.5% "
            f"(real market edges cap at ~2.5%, signals need room below that)"
        )

    async def _requeue_threshold_rejections(self) -> None:
        """Requeue hypotheses that were rejected due to the high-threshold bug.

        These hypotheses were rejected with 'no_edge_after_backtest' because their
        edge_threshold was ≥3% while real market edges cap at ~2.5%. With thresholds
        now lowered, they deserve a second chance.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE status = 'rejected' "
            "AND promoted_by LIKE '%no_edge_after_backtest%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Threshold rejection requeue: no hypotheses to requeue")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            # Reset eval cycles so they get a fresh evaluation
            config["evaluate_cycles"] = 0
            config["requeued_from_threshold_bug"] = True

            await db.execute(
                "UPDATE hypotheses SET status = 'backtesting', "
                "edge_threshold = 0.015, model_config = ? "
                "WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Threshold rejection requeue: moved {count} hypotheses from rejected → backtesting "
            f"(were victims of edge_threshold ≥ 3% bug, now set to 1.5%)"
        )

    async def stop(self) -> None:
        """Stop the research loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Record final downtime stats
        await self._downtime_tracker.record_to_hermes()
        logger.info(
            f"Research loop stopped — {self._cycles} cycles, "
            f"{self._hypotheses_generated} hypotheses generated, "
            f"{self._backtests_run} backtests run, "
            f"{self._promotions} promoted, {self._rejections} rejected"
        )

    async def _drain_deferred_queue(self) -> None:
        """If Claude is available and we have queued work, drain it first.

        This is the critical path: when Claude comes back online after a
        rate-limit window, all deferred hypothesis generation, interpretation,
        and deep work gets executed immediately before the normal cycle.
        """
        from tools.claude_code import is_available as claude_available, claude_code_query

        claude_up = claude_available()

        # Track Claude availability transitions
        if claude_up and not self._was_claude_available:
            self._downtime_tracker.mark_available()
        elif not claude_up and self._was_claude_available:
            self._downtime_tracker.mark_unavailable()
        self._was_claude_available = claude_up

        if not claude_up:
            return

        pending = await self._work_queue.size()
        if pending == 0:
            return

        logger.info(f"Claude available -- draining {pending} deferred items")
        drained = await self._work_queue.drain(max_items=5)

        for item in drained:
            if not self._running:
                break
            try:
                result = await claude_code_query(
                    item["prompt"], hermes_caller=item["work_type"]
                )
                self._last_claude_call = time.time()
                self._claude_escalations += 1

                if result.get("content") and not result.get("error"):
                    # Process based on work type
                    await self._process_drained_item(item, result["content"])
                    await self._work_queue.mark_done(item["id"], result["content"][:500])
                    logger.info(
                        f"Drained item {item['id']} ({item['work_type']}): success"
                    )
                elif result.get("rate_limited"):
                    # Claude went away again -- put item back
                    await self._work_queue.mark_failed(item["id"], "rate_limited_during_drain")
                    logger.info("Claude rate-limited during drain -- stopping drain")
                    break
                else:
                    await self._work_queue.mark_done(
                        item["id"], f"error: {result.get('error', 'unknown')}"
                    )
            except Exception as e:
                await self._work_queue.mark_failed(item["id"], str(e))
                logger.warning(f"Drain item {item['id']} failed: {e}")

        # Record downtime pattern every 10 cycles
        if self._cycles % 10 == 0:
            await self._downtime_tracker.record_to_hermes()

    async def _process_drained_item(self, item: dict, content: str) -> None:
        """Process the result of a drained deferred work item."""
        work_type = item["work_type"]
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

            if work_type == "hypothesis_gen":
                created = 0
                for nh in parsed.get("hypotheses", []):
                    try:
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", f"deferred_gen_{self._cycles}"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config={"source": "deferred_queue_claude", "cycle": self._cycles},
                        )
                        created += 1
                    except Exception as e:
                        logger.warning(f"Failed to create deferred hypothesis: {e}")
                if created:
                    self._hypotheses_generated += created
                    logger.info(f"Deferred drain: created {created} hypotheses")

            elif work_type == "deep_work":
                # Same processing as _phase_claude_deep_work
                rejected = 0
                for hid in parsed.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_claude_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                created = 0
                for nh in parsed.get("new_hypotheses", []):
                    try:
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", "deferred_deep"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config={"source": "deferred_deep_work", "cycle": self._cycles},
                        )
                        created += 1
                    except Exception:
                        pass
                if rejected or created:
                    self._hypotheses_generated += created
                    logger.info(
                        f"Deferred drain deep_work: rejected {rejected}, created {created}"
                    )

                # Route pipeline_issues to self-repair (same as _phase_claude_deep_work)
                pipeline_issues = parsed.get("pipeline_issues", [])
                if pipeline_issues:
                    findings = []
                    for issue in pipeline_issues:
                        issue_lower = issue.lower() if isinstance(issue, str) else ""
                        if any(kw in issue_lower for kw in ["identical", "same games", "filtering bug", "broken"]):
                            severity = "CRITICAL"
                        elif any(kw in issue_lower for kw in ["prioritize", "threshold", "zero promotion", "low sample"]):
                            severity = "HIGH"
                        else:
                            severity = "LOW"
                        findings.append({"severity": severity, "description": issue})
                    try:
                        from tools.self_repair import get_repair_engine
                        engine = get_repair_engine()
                        repair_results = await engine.handle_claude_findings(findings)
                        for r in repair_results:
                            if r["fixed"]:
                                logger.info(f"Deferred deep work auto-fix: {r['action']} — {r['detail']}")
                            else:
                                logger.warning(f"Deferred deep work unfixed: {r['action']} — {r['detail']}")
                    except Exception as e:
                        logger.warning(f"Deferred drain: failed to route findings to self-repair: {e}")

            elif work_type == "interpret_backtests":
                # Same processing as _phase_interpret_backtests
                db = self.data_collector._db
                rejected = 0
                for hid in parsed.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_interpret"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                modified = 0
                for mod in parsed.get("modify", []):
                    try:
                        hid = mod.get("id")
                        new_thresh = mod.get("new_threshold")
                        if hid and new_thresh is not None and db:
                            await db.execute(
                                "UPDATE hypotheses SET edge_threshold = ? WHERE hypothesis_id = ?",
                                (new_thresh, hid),
                            )
                            await db.commit()
                            modified += 1
                    except Exception:
                        pass
                if rejected or modified:
                    logger.info(
                        f"Deferred drain interpret: rejected {rejected}, modified {modified}"
                    )

            elif work_type == "system_improvement":
                db = self.data_collector._db
                stored = 0
                for imp in parsed.get("improvements", []):
                    try:
                        if db:
                            await db.execute(
                                "INSERT INTO system_improvements "
                                "(cycle, category, suggestion, priority) VALUES (?, ?, ?, ?)",
                                (self._cycles, imp.get("category", "general"),
                                 imp.get("suggestion", ""), imp.get("priority", "medium")),
                            )
                            stored += 1
                    except Exception:
                        pass
                if stored and db:
                    await db.commit()
                    logger.info(f"Deferred drain: stored {stored} system improvements")

            elif work_type == "diagnostic_escalation":
                logger.info(f"Deferred diagnostic processed: {content[:200]}")

        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Deferred item {work_type} response not valid JSON: {e}")

    async def _loop(self) -> None:
        """Main research cycle."""
        # Brief delay to let other systems start
        await asyncio.sleep(15)

        while self._running:
            try:
                self._cycles += 1
                logger.info(f"Research cycle #{self._cycles} starting")

                # ── Queue drain: if Claude just became available, burn through deferred work ──
                await self._drain_deferred_queue()

                # Phase 0: Self-repair (detect, fix, verify, record)
                await self._phase_self_repair()

                if not self._running:
                    break

                # Phase 0a: Self-diagnose pipeline health
                await self._phase_self_diagnose()

                if not self._running:
                    break

                # Phase 0b: Refresh signals (retroactive threshold updates)
                await self._phase_refresh_signals()

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

                if not self._running:
                    break

                # Phase 9: Pipeline integrity check (every N cycles)
                await self._phase_integrity_check()

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

    async def _phase_self_repair(self) -> None:
        """
        Self-repair phase — detect issues, fix them autonomously, verify,
        and record to Hermes. Runs every 5 cycles to avoid overhead.
        """
        if self._cycles % 5 != 1:
            return  # Only run every 5 cycles (cycle 1, 6, 11, ...)

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

        if not issues:
            logger.info("DIAG: all pipeline health checks passed")

    async def _phase_refresh_signals(self) -> None:
        """Retroactively update signal_generated when thresholds change.

        Claude deep work can lower edge_threshold on hypotheses AFTER backtests
        have already run and stored signal_generated=0. This phase catches those
        events and upgrades them to signal=1 so the pipeline sees them.
        """
        import aiosqlite

        db_path = self.backtest_engine.db_path
        try:
            async with aiosqlite.connect(db_path) as db:
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
        except Exception as e:
            logger.warning(f"Signal refresh failed: {e}")

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

        # Use focus-area-ordered sports: focus sports first for fresher data
        ordered_sports = self.focus_manager.get_ordered_research_sports()
        for sport in ordered_sports:
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
                    await self.data_collector.resolve_game_level_outcomes(sport, date_fmt)

                self._data_collections += 1
            except Exception as e:
                logger.warning(f"Data collection failed for {sport}: {e}")

        # Collect pre-calculated value bets from Odds-API.io Pro
        # These are updated every 5 seconds with EV computed from consensus
        try:
            from tools.odds_api_io import get_value_bets
            for book in ["DraftKings", "Fanatics"]:
                vb = await get_value_bets(book)
                if vb.get("count", 0) > 0:
                    logger.info(
                        f"Research: {vb['count']} value bets from {book} "
                        f"(top EV: {max(b['ev_pct'] for b in vb['bets']):.1%})"
                    )
                    # Store in ev_opportunities table for edge scanner
                    try:
                        db = self.data_collector._db
                        if db:
                            for bet in vb["bets"]:
                                if bet["ev_pct"] >= 0.01:  # Only store 1%+ EV
                                    await db.execute(
                                        "INSERT OR REPLACE INTO ev_opportunities "
                                        "(event_id, book, market, side, ev_pct, "
                                        "source, updated_at) "
                                        "VALUES (?, ?, ?, ?, ?, 'odds_api_io_pro', ?)",
                                        (
                                            bet["event_id"], bet["bookmaker"],
                                            bet["market"], bet["side"],
                                            bet["ev_pct"], bet["updated_at"],
                                        ),
                                    )
                            await db.commit()
                    except Exception as e:
                        logger.debug(f"Value bet storage: {e}")
        except Exception as e:
            logger.warning(f"Value bets collection failed: {e}")

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
                    except Exception as e:
                        logger.warning(f"Failed to query historical_odds_cache date ranges: {e}")

                # Build focus area context for the prompt
                focus_context = self.focus_manager.get_focus_context_for_prompt()

                prompt = (
                    f"CALLISTO HYPOTHESIS GENERATION — Cycle #{self._cycles}\n\n"
                    f"You are a skeptical quantitative researcher. Your default stance: "
                    f"most hypotheses are noise. Your job is to find the rare ones that aren't.\n\n"
                    f"BEFORE GENERATING: scrutinize the pipeline state below. If something "
                    f"is broken or data quality is insufficient, say so in a 'pipeline_warning' "
                    f"field instead of generating garbage hypotheses.\n\n"
                    f"PIPELINE STATE:\n"
                    f"  Total hypotheses: {len(all_hypos)} "
                    f"({draft_count} draft, {active_count} active, {rejected_count} rejected)\n"
                    f"  Rejection rate: {rejected_count}/{max(1, rejected_count + active_count)}"
                    f" — if this is >90%, challenge whether the pipeline can test ANY hypothesis\n"
                    f"  Sports: {', '.join(RESEARCH_SPORTS)}\n"
                    f"  Data ranges: {json.dumps(date_ranges)}\n"
                    f"  Collection stats: {json.dumps(data_stats)}\n"
                    f"  Model: consensus devig (power method) — needs 3+ books to be reliable. "
                    f"If most events show books_used=1, the devig is meaningless.\n\n"
                    f"EXISTING HYPOTHESIS NAMES (avoid duplicates):\n"
                    f"  {json.dumps(existing_names[:50])}\n\n"
                    f"{focus_context}\n\n"
                    f"EDGE TYPES (edges that persist hours, not speed arb):\n"
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
                    f'"edge_threshold": 0.015}}\n'
                    f'], "pipeline_warning": "optional — flag if data quality makes testing pointless"}}\n\n'
                    f"RULES:\n"
                    f"- Generate 3-5 hypotheses per call\n"
                    f"- At least 2 hypotheses MUST be for the PRIORITY FOCUS AREAS listed above\n"
                    f"- Each must be testable with the ACTUAL data we have (check collection stats)\n"
                    f"- Names must be unique (not in existing list)\n"
                    f"- Thesis must be specific and falsifiable\n"
                    f"- Steelman each hypothesis before including it: what is the strongest "
                    f"case that this edge exists? If you can't make that case, drop it.\n"
                    f"- If the pipeline state shows systemic issues (high rejection rate, "
                    f"thin data, broken resolution), flag them — do NOT just generate more "
                    f"hypotheses into a broken funnel\n"
                )

                result = await claude_code_query(prompt, hermes_caller="hypothesis_gen")
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
                                    edge_threshold=nh.get("edge_threshold", 0.015),
                                    model_config={
                                        "source": "claude_primary_gen",
                                        "cycle": self._cycles,
                                        "training_period_start": training_period_start,
                                        "training_period_end": training_period_end,
                                        "forward_test_start": forward_test_start,
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

        # ── FALLBACK: Template + local model generation when Claude unavailable ──
        if not used_claude:
            # Defer the Claude prompt so it runs when Claude comes back
            from tools.claude_code import is_available as _ca
            if not _ca():
                try:
                    all_hypos = await self.hypothesis_manager.list_hypotheses()
                    existing_names = [h["name"] for h in all_hypos]
                    focus_context = self.focus_manager.get_focus_context_for_prompt()
                    # Build and enqueue the same prompt Claude would have gotten
                    deferred_prompt = (
                        f"CALLISTO HYPOTHESIS GENERATION — Deferred from Cycle #{self._cycles}\n\n"
                        f"Generate 3-5 novel, testable sports betting hypotheses.\n\n"
                        f"EXISTING NAMES (avoid duplicates): {json.dumps(existing_names[:30])}\n\n"
                        f"{focus_context}\n\n"
                        f"RESPOND WITH JSON: {{\"hypotheses\": [{{\"name\": \"...\", \"thesis\": \"...\", "
                        f"\"sport\": \"...\", \"market_type\": \"...\", \"edge_threshold\": 0.03}}]}}"
                    )
                    await self._work_queue.enqueue("hypothesis_gen", deferred_prompt, priority=2)
                    self._downtime_tracker.item_queued()
                    logger.info("Research: hypothesis gen deferred to work queue (Claude unavailable)")
                except Exception as e:
                    logger.warning(f"Failed to enqueue deferred hypothesis gen: {e}")

                # Try local model fallback for quick hypothesis ideas
                try:
                    from tools.work_queue import local_fallback_hypothesis_gen
                    pipeline_state = (
                        f"Cycles: {self._cycles}, Hypotheses: {self._hypotheses_generated}, "
                        f"Backtests: {self._backtests_run}"
                    )
                    all_hypos = await self.hypothesis_manager.list_hypotheses()
                    existing_names = [h["name"] for h in all_hypos]
                    focus_context = self.focus_manager.get_focus_context_for_prompt()
                    local_hypos = await local_fallback_hypothesis_gen(
                        pipeline_state, existing_names, focus_context
                    )
                    for nh in local_hypos:
                        try:
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", f"local_gen_{self._cycles}"),
                                thesis=nh.get("thesis", ""),
                                sport=nh.get("sport", "basketball_nba"),
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config={
                                    "source": "local_fallback_gen",
                                    "cycle": self._cycles,
                                    "training_period_start": training_period_start,
                                    "training_period_end": training_period_end,
                                    "forward_test_start": forward_test_start,
                                },
                            )
                            total_created += 1
                        except Exception as e:
                            logger.debug(f"Local fallback hypothesis creation failed: {e}")
                    if local_hypos:
                        logger.info(f"Research: local model generated {len(local_hypos)} hypotheses")
                except Exception as e:
                    logger.debug(f"Local fallback hypothesis gen failed: {e}")

            # Template fallback always runs when Claude didn't
            logger.info("Research: using template fallback for hypothesis generation")
            ordered_sports = self.focus_manager.get_ordered_research_sports()
            for sport in ordered_sports:
                try:
                    # Focus sports get 2x hypothesis quota
                    quota = 40 if self.focus_manager.is_focus_sport(sport) else 20
                    created = await self.hypothesis_generator.generate_from_templates(
                        sport=sport,
                        max_hypotheses=quota,
                        training_cutoff_date=training_period_end,
                    )
                    total_created += len(created)
                except Exception as e:
                    logger.warning(f"Template generation failed for {sport}: {e}")

        self._hypotheses_generated += total_created
        logger.info(f"Research: generated {total_created} new hypotheses")

    async def _phase_backtest(self) -> None:
        """Backtest draft hypotheses — enforcing temporal isolation.

        The correct lifecycle:
          1. Hypothesis was generated using data from [training_period_start .. training_period_end]
          2. Backtest MUST only use data AFTER training_period_end + gap
          3. This prevents circular testing (training and testing on same data)

        Legacy hypotheses without temporal metadata get a conservative default:
        backtest only the last 30 days (assumed to be unseen).
        """
        # Bridge live odds_snapshots into historical_odds_cache so backtests
        # can use recently-collected multi-book data
        try:
            bridge_result = await self.backtest_engine.historical_fetcher.bridge_snapshots_to_cache()
            if bridge_result.get("bridged", 0) > 0:
                logger.info(f"Research: bridged {bridge_result['bridged']} snapshot-days into historical cache")
        except Exception as e:
            logger.warning(f"Research: snapshot bridge failed: {e}")

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

        # Sort drafts by focus area priority (focus sports first), then by sport
        # data availability (NBA/NFL before MLB — more historical data = better backtests)
        drafts = self.focus_manager.sort_by_focus(drafts, sport_key="sport")
        drafts.sort(key=lambda h: SPORT_PRIORITY.get(h.get("sport", ""), 99))
        to_test = drafts[:BACKTEST_BATCH_SIZE]
        logger.info(f"Research: backtesting {len(to_test)} hypotheses (sport-priority + focus-area sorted)")

        for h in to_test:
            if not self._running:
                break

            sport = h.get("sport", "")
            market = h.get("market_type", "")

            # Player prop hypotheses can't be backtested (no historical prop data).
            # Move to backtesting status — they will accumulate paper trade data
            # over time and be promoted only when actual evidence exists.
            if market.startswith("player_"):
                try:
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "backtesting", "auto:awaiting_prop_data"
                    )
                    self._backtests_run += 1
                    logger.info(
                        f"Research: moved {h['hypothesis_id']} ({market}) "
                        f"to backtesting — awaiting prop data collection"
                    )
                except Exception as e:
                    logger.warning(f"Failed to promote prop hypothesis {h['hypothesis_id']}: {e}")
                continue

            # Skip hypotheses where most context conditions are unfilterable.
            # These produce identical event sets across different hypotheses
            # because game-level conditions (pitcher stats, weather, etc.) can't
            # be applied — the backtest just tests ALL games in the sport/market.
            model_cfg = h.get("model_config", {})
            if isinstance(model_cfg, str):
                try:
                    model_cfg = json.loads(model_cfg)
                except (json.JSONDecodeError, TypeError):
                    model_cfg = {}
            from tools.backtest import BacktestEngine
            ctx_coverage = BacktestEngine.compute_context_coverage(model_cfg)
            # Also infer context needs from thesis/name BEFORE running backtest
            # (same inference run_backtest does internally). This prevents wasting
            # a backtest cycle on hypotheses that will just return "untestable".
            if ctx_coverage >= 0.5 and not model_cfg.get("context_factors"):
                h_thesis = h.get("thesis", "")
                h_name_for_ctx = h.get("name", "")
                inferred_pre = BacktestEngine._infer_context_needs(h_thesis, h_name_for_ctx)
                if inferred_pre:
                    ctx_coverage = 0.0
                    logger.info(
                        f"Research: pre-backtest inference for {h['hypothesis_id']} "
                        f"({h_name_for_ctx}) detected unfilterable needs: {inferred_pre}"
                    )
            if ctx_coverage < 0.5:
                ctx_factors = model_cfg.get("context_factors", [])
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"context_coverage={ctx_coverage:.0%}. Needs game context enrichment."
                )
                continue

            # Skip hypotheses for sports with no usable multi-book data
            if sports_with_odds and sport not in sports_with_odds:
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"{sport} has no multi-book odds data yet"
                )
                continue

            try:
                # ── Temporal isolation: determine forward-test date range ──
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                has_temporal = (
                    "training_period_end" in model_config
                    and model_config["training_period_end"]
                )

                if has_temporal:
                    # Forward-only backtest: start AFTER training period + gap
                    training_end = model_config["training_period_end"]
                    try:
                        te_date = datetime.strptime(training_end, "%Y-%m-%d").date()
                    except ValueError:
                        te_date = datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                    start_date = str(te_date + timedelta(days=BACKTEST_GAP_DAYS))
                    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} forward-only "
                        f"[{start_date} .. {end_date}] (trained up to {training_end})"
                    )
                else:
                    # Legacy hypothesis without temporal metadata — conservative default
                    logger.warning(
                        f"Research: hypothesis {h['hypothesis_id']} has NO temporal metadata. "
                        f"Defaulting to last {DEFAULT_TRAINING_WINDOW_DAYS} days only (conservative)."
                    )
                    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    start_date = str(
                        datetime.now(timezone.utc).date()
                        - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                    )

                # Never backtest against today — games haven't finished
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if end_date >= today:
                    end_date = str(datetime.now(timezone.utc).date() - timedelta(days=1))

                # ── Constrain to date range where historical data EXISTS ──
                # Without this, backtests target dates with no cached odds
                # and produce 0 events every time.
                cached_range = await self.backtest_engine.historical_fetcher.get_cached_date_range(sport)
                if cached_range and cached_range[0] and cached_range[1]:
                    cache_start, cache_end = cached_range
                    # Clamp start_date and end_date to the cached range
                    if start_date < cache_start:
                        start_date = cache_start
                    if end_date > cache_end:
                        end_date = cache_end
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} date range "
                        f"clamped to cached data [{start_date} .. {end_date}]"
                    )
                else:
                    logger.info(
                        f"Research: skipping backtest for {h['hypothesis_id']} — "
                        f"no historical odds cached for {sport}"
                    )
                    continue

                if start_date > end_date:
                    logger.info(
                        f"Research: skipping backtest for {h['hypothesis_id']} — "
                        f"no historical date range available (start={start_date} > end={end_date})"
                    )
                    continue

                result = await self.backtest_engine.run_backtest(
                    hypothesis_id=h["hypothesis_id"],
                    start_date=start_date,
                    end_date=end_date,
                    credit_budget=30,  # Enough for ~10 dates × 3 markets
                )

                # Handle untestable hypotheses — context filtering not available
                if result.get("error") == "untestable":
                    logger.warning(
                        f"Research: hypothesis {h['hypothesis_id']} ({h.get('name', '?')}) "
                        f"is UNTESTABLE — {result.get('detail', 'no context data')}. "
                        f"Moving back to draft."
                    )
                    try:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "draft", "auto:untestable"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to revert {h['hypothesis_id']} to draft: {e}")
                    continue

                # Handle spring training — don't penalize, just skip until season starts
                if result.get("error") == "spring_training":
                    logger.info(
                        f"Research: skipping {h['hypothesis_id']} ({h.get('name', '?')}) — "
                        f"MLB spring training, will retry after season start"
                    )
                    continue

                # Store temporal metadata in backtest result for integrity checking
                self._backtests_run += 1
                signals = result.get("signals_generated", 0)

                # Update model_config with actual backtest range for audit trail
                if has_temporal:
                    model_config["backtest_period_start"] = start_date
                    model_config["backtest_period_end"] = end_date
                    model_config["temporal_isolation"] = True
                else:
                    model_config["backtest_period_start"] = start_date
                    model_config["backtest_period_end"] = end_date
                    model_config["temporal_isolation"] = False
                    model_config["temporal_isolation_note"] = "legacy_hypothesis_conservative_default"

                # Persist updated model_config
                try:
                    db = self.data_collector._db
                    if db:
                        await db.execute(
                            "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                            (json.dumps(model_config), h["hypothesis_id"]),
                        )
                        await db.commit()
                except Exception as e:
                    logger.warning(f"Failed to update temporal metadata for {h['hypothesis_id']}: {e}")

                total_events = result.get("total_events", 0)
                if total_events == 0:
                    logger.warning(
                        f"Research: backtest {h['hypothesis_id']} produced 0 events "
                        f"({start_date} to {end_date}) — no historical odds data for {sport}?"
                    )
                else:
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} — "
                        f"{total_events} events, {signals} signals"
                    )
            except Exception as e:
                logger.warning(
                    f"Backtest failed for {h['hypothesis_id']}: {e}"
                )

    @staticmethod
    def _check_temporal_overlap(model_config: dict) -> Optional[str]:
        """Check if training and backtest periods overlap. Returns error message or None."""
        if isinstance(model_config, str):
            try:
                model_config = json.loads(model_config)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(model_config, dict):
            return None

        training_end = model_config.get("training_period_end")
        backtest_start = model_config.get("backtest_period_start")

        if not training_end or not backtest_start:
            return None  # Can't check without both dates

        try:
            te = datetime.strptime(str(training_end), "%Y-%m-%d").date()
            bs = datetime.strptime(str(backtest_start), "%Y-%m-%d").date()
            if bs <= te:
                return (
                    f"TEMPORAL OVERLAP: backtest starts {bs} but training ends {te}. "
                    f"Backtest results are contaminated by training data."
                )
        except ValueError:
            pass

        return None

    async def _phase_evaluate(self) -> None:
        """Evaluate backtesting hypotheses for promotion or rejection.

        Enforces temporal isolation: a hypothesis can only be promoted if
        its backtest period does NOT overlap its training period. This
        prevents circular testing from ever reaching paper trading or live.
        """
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
                # ── Temporal isolation gate ──
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                overlap_err = self._check_temporal_overlap(model_config)
                if overlap_err:
                    logger.error(
                        f"Research: REJECTING {h['hypothesis_id']} — {overlap_err}"
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "rejected",
                        f"auto:temporal_overlap — {overlap_err}"
                    )
                    self._rejections += 1
                    continue

                # ── Context coverage gate ──
                # If a hypothesis was backtested before the context coverage check
                # was added, its results are noise. Move back to draft so it can
                # be properly evaluated when game context enrichment is available.
                from tools.backtest import BacktestEngine
                ctx_coverage = BacktestEngine.compute_context_coverage(model_config)

                # Also infer context needs from thesis/name (same logic as
                # run_backtest). Without this, hypotheses with empty
                # context_factors appear "fully filterable" (coverage=1.0)
                # even when their name implies unfilterable conditions.
                if ctx_coverage >= 0.5 and not model_config.get("context_factors"):
                    thesis = h.get("thesis", "")
                    h_name = h.get("name", "")
                    inferred = BacktestEngine._infer_context_needs(thesis, h_name)
                    if inferred:
                        ctx_coverage = 0.0
                        logger.info(
                            f"Research: {h['hypothesis_id']} ({h_name}) — inferred "
                            f"unfilterable context needs: {inferred}"
                        )

                # Also check needs_unique_data flag from self-repair
                if model_config.get("needs_unique_data"):
                    logger.warning(
                        f"Research: demoting {h['hypothesis_id']} to draft — "
                        f"flagged as needs_unique_data (duplicate event set)"
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft",
                        "auto:needs_unique_data — stale backtest with duplicate event set"
                    )
                    continue

                if ctx_coverage < 0.5:
                    ctx_factors = model_config.get("context_factors", [])
                    logger.warning(
                        f"Research: demoting {h['hypothesis_id']} to draft — "
                        f"context_coverage={ctx_coverage:.0%} ({len(ctx_factors)} "
                        f"factors, most unfilterable). Backtest results are noise."
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft",
                        f"auto:low_context_coverage ({ctx_coverage:.0%}) — needs game context enrichment"
                    )
                    continue

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

        # Also evaluate paper trading hypotheses — require BOTH backtest
        # significance AND paper trading data on temporally isolated data
        paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")
        for h in paper:
            try:
                # ── Temporal isolation gate for live promotion ──
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                # Paper trades must be AFTER hypothesis creation date
                # (this is inherently true since paper trades use live odds,
                # but we verify the hypothesis has temporal isolation)
                has_temporal = bool(model_config.get("training_period_end"))
                has_backtest = bool(model_config.get("temporal_isolation"))

                if not has_temporal and not has_backtest:
                    # Legacy hypothesis — allow promotion but log warning
                    logger.warning(
                        f"Research: hypothesis {h['hypothesis_id']} lacks temporal "
                        f"isolation metadata — allowing paper trade eval but flagging"
                    )

                result = await self.hypothesis_manager.auto_promote(h["hypothesis_id"])
                action = result.get("action", "held")
                if action == "promoted":
                    self._promotions += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} PROMOTED TO LIVE"
                    )
                    # Alert Marco — this thesis is proven
                    try:
                        await telegram.alert_system(
                            f"HYPOTHESIS PROVEN: {h['name']}\n"
                            f"Thesis: {h['thesis'][:200]}\n"
                            f"Status: LIVE — ready for real money\n"
                            f"Temporal isolation: {'YES' if has_temporal else 'LEGACY (no metadata)'}"
                        )
                    except Exception as e:
                        logger.warning(f"Telegram notification failed for proven hypothesis {h['name']}: {e}")
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

        When Claude is unavailable: defers the prompt to the work queue AND
        runs a local rules-based interpretation as fallback.
        """
        from tools.claude_code import is_available as claude_available, claude_code_query

        now = time.time()
        if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
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
                       SUM(CASE WHEN be.actual_result='won' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN be.actual_result='lost' THEN 1 ELSE 0 END) as losses,
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
            f"You are a statistician reviewing backtest results. Your bias is toward "
            f"skepticism: most patterns are noise, and you must prove otherwise.\n\n"
            f"Before evaluating any hypothesis, ask: was this a FAIR test?\n"
            f"- If events=15 and signals=0, that is NOT enough data to reject — hold it.\n"
            f"- If avg_edge is computed from 1 book, the entire edge is an artifact.\n"
            f"- If all hypotheses show similar event counts, the backtest filter is broken.\n\n"
            f"HYPOTHESIS BACKTEST RESULTS (top 10 by signal count):\n"
            f"{json.dumps(hypo_data, indent=2)}\n\n"
            f"STATISTICAL CONTEXT:\n"
            f"- A fair coin has ~50% hit rate. Signal needs to beat that consistently.\n"
            f"- With <30 resolved bets, results are noise. DO NOT reject on thin data.\n"
            f"- avg_edge > 0.03 with hit_rate > 0.53 over 50+ resolved is promising.\n"
            f"- 0 signals after 50+ events means the hypothesis never fires — reject it.\n"
            f"- Low signal rate (<5%) with poor hit rate: lower the threshold, don't kill it.\n"
            f"- Before rejecting: steelman the hypothesis. What is the strongest case it's real?\n"
            f"  Only reject if you can refute that case with the data.\n\n"
            f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
            f'{{"data_quality_assessment": "honest 1-sentence verdict on whether these backtests are reliable", '
            f'"reject": ["hypothesis_id — ONLY with 50+ events AND fair test conditions"], '
            f'"modify": [{{"id": "hypothesis_id", "new_threshold": 0.025, "reason": "..."}}], '
            f'"insights": "What patterns are working, what isn\'t, and what the pipeline should change"}}\n\n'
            f"RULES:\n"
            f"- data_quality_assessment FIRST: are these results trustworthy?\n"
            f"- reject: ONLY hypotheses with clear disproof (0 signals after 50+ events with 3+ books)\n"
            f"- modify: lower thresholds on promising hypotheses rather than killing them\n"
            f"- If data quality is poor, say so and recommend holding rather than rejecting\n"
        )

        if not claude_available():
            # Defer to queue for when Claude returns
            await self._work_queue.enqueue("interpret_backtests", prompt, priority=2)
            self._downtime_tracker.item_queued()
            logger.info("Research: backtest interpretation deferred to work queue (Claude unavailable)")

            # Run local rules-based interpretation as fallback
            try:
                from tools.work_queue import local_fallback_interpret
                local_actions = await local_fallback_interpret(hypo_data)
                rejected = 0
                for hid in local_actions.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "local_fallback_interpret"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                if rejected:
                    logger.info(
                        f"Research: local fallback interpretation rejected {rejected} "
                        f"noise hypotheses"
                    )
                insights = local_actions.get("insights", "")
                if insights:
                    logger.info(f"Research: local interpretation — {insights[:300]}")
            except Exception as e:
                logger.debug(f"Local fallback interpretation failed: {e}")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
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
                        except Exception as e:
                            logger.warning(f"Failed to reject hypothesis {hid}: {e}")
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
                        except Exception as e:
                            logger.warning(f"Failed to modify threshold for hypothesis {mod.get('id', '?')}: {e}")
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
                        if live_odds.get("error"):
                            logger.warning(
                                f"Paper trade: Odds API failed for {sport} props: "
                                f"{live_odds.get('error')} — skipping prop hypotheses"
                            )
                        elif not live_odds.get("games"):
                            logger.warning(
                                f"Paper trade: Odds API returned 0 games for {sport}"
                            )
                        else:
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
                                    # Also insert into signals table
                                    edge_val = round(edge_info.get("edge_pct", 0) / 100, 6)
                                    if edge_val > 0.05:
                                        sig_confidence = "high"
                                    elif edge_val > 0.03:
                                        sig_confidence = "medium"
                                    else:
                                        sig_confidence = "low"
                                    await db.execute(
                                        "INSERT INTO signals "
                                        "(event_id, sport, signal_type, team, market, book, "
                                        "odds_american, fair_probability, fair_prob_source, "
                                        "edge_pct, ev_pct, confidence, kelly_fraction, "
                                        "recommended_stake, status, notes) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            event_id,
                                            sport,
                                            "paper_trade",
                                            edge_info.get("side", ""),
                                            market,
                                            "draftkings",
                                            edge_info.get("target_price", 0),
                                            edge_info.get("fair_probability", 0),
                                            "cross_book_devig",
                                            edge_val,
                                            round(edge_info.get("ev_per_100", 0) / 100, 6),
                                            sig_confidence,
                                            edge_info.get("kelly_fraction", 0),
                                            None,
                                            "paper",
                                            f"hypothesis_id={h['hypothesis_id']}, trade_id={trade_id}",
                                        ),
                                    )
                                await db.commit()
                        except Exception as e:
                            logger.warning(f"Prop scan failed for {event_id}: {e}", exc_info=True)
                    continue

                # For game-level markets: use DK scraper (free), Odds API as fallback,
                # line_monitor cached snapshots as last resort
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

                    # Last resort: use line monitor's cached snapshot
                    if live_odds.get("error") or not live_odds.get("games"):
                        if hasattr(self, 'line_monitor') and self.line_monitor:
                            snap = self.line_monitor._snapshots.get(sport, {})
                            if snap and not snap.get("error") and snap.get("games"):
                                live_odds = snap
                                logger.info(
                                    f"Paper trade: using line_monitor cached snapshot for {sport} "
                                    f"({len(snap.get('games', []))} games)"
                                )

                    if live_odds.get("error") or not live_odds.get("games"):
                        logger.warning(
                            f"Paper trade: no odds available for {sport} — "
                            f"DK scraper, Odds API, and line_monitor all failed"
                        )
                    else:
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

        NO cooldown gate — deep work is the most valuable phase. If Claude
        is available, use it. The rate limiter handles the rest.

        When Claude is unavailable: defers the prompt to work queue AND
        runs local model fallback for basic maintenance (reject zero-signal
        hypotheses, gather pipeline metrics).
        """
        from tools.claude_code import is_available as claude_available, claude_code_query
        import time as _time

        now = _time.time()
        # No cooldown check — deep work should always fire if Claude is available
        if not claude_available():
            # Local fallback: basic rule-based deep work
            try:
                from tools.work_queue import local_fallback_deep_work
                db = self.data_collector._db
                actions = await local_fallback_deep_work(db)

                rejected = 0
                for hid in actions.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "local_fallback_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                if rejected:
                    logger.info(f"Research: local fallback deep work rejected {rejected} hypotheses")
                for issue in actions.get("pipeline_issues", []):
                    logger.info(f"Research: local fallback identified — {issue}")
            except Exception as e:
                logger.debug(f"Local fallback deep work failed: {e}")

            # NOTE: prompt is deferred below AFTER it's built (need metrics first)
            # We'll set a flag and enqueue at the end
            _defer_deep_work = True
        else:
            _defer_deep_work = False

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
        except Exception as e:
            logger.warning(f"Failed to query top hypotheses for deep work prompt: {e}")

        # Self-scrutiny: check if hypotheses are testing the same games
        scrutiny_info = ""
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name, COUNT(DISTINCT be.event_id) as unique_games,
                       COUNT(*) as total_events
                FROM hypotheses h
                JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status = 'backtesting'
                GROUP BY h.hypothesis_id
                HAVING total_events > 0
                ORDER BY total_events DESC
                LIMIT 10
            """)
            game_sets = []
            for r in await cursor.fetchall():
                game_sets.append(f"  {r[1]}: {r[2]} unique games, {r[3]} events")

            # Check for duplicate game sets (different hypotheses testing identical games)
            cursor2 = await db.execute("""
                SELECT GROUP_CONCAT(DISTINCT h.name) as hypo_names,
                       COUNT(DISTINCT be.event_id) as unique_games,
                       COUNT(*) as total_events
                FROM hypotheses h
                JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status = 'backtesting'
                GROUP BY h.hypothesis_id
                HAVING total_events > 10
            """)
            event_counts = {}
            for r in await cursor2.fetchall():
                key = f"{r[1]}g_{r[2]}e"
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

        # Include focus area context
        focus_context = self.focus_manager.get_focus_context_for_prompt()

        prompt = (
            f"CALLISTO AUTONOMOUS SYSTEM — DEEP WORK CYCLE #{self._cycles}\n"
            f"You are a critical analyst auditing an autonomous research pipeline.\n"
            f"Your primary obligation is honesty about what is working and what is broken.\n"
            f"pipeline_issues takes PRIORITY over new_hypotheses — fix the funnel before "
            f"pouring more hypotheses into it.\n\n"
            f"PIPELINE STATE:\n"
            f"  Backtest events: {metrics.get('bt_events', 0)} total, "
            f"{metrics.get('bt_signals', 0)} signals ({(metrics.get('bt_signals',0) / max(1,metrics.get('bt_events',1))) * 100:.1f}%)\n"
            f"  Hypothesis status: {json.dumps(metrics.get('hypo_status', {}))}\n"
            f"  Odds cache: {json.dumps(metrics.get('odds_cache', {}))}\n"
            f"  Promotions: {self._promotions} | Rejections: {self._rejections}\n"
            f"  Cycles: {self._cycles} | Data collections: {self._data_collections}\n\n"
            f"CRITICAL QUESTIONS (answer these honestly before generating anything):\n"
            f"  1. What is the promotion rate? If 0%, why — bad hypotheses or broken pipeline?\n"
            f"  2. Signal rate {(metrics.get('bt_signals',0) / max(1,metrics.get('bt_events',1))) * 100:.1f}% — "
            f"is this because edges don't exist, or because devig uses too few books?\n"
            f"  3. Are hypotheses getting a fair trial, or dying before results are collected?\n\n"
            f"{focus_context}\n\n"
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
        )

        # If Claude unavailable, defer the fully-built prompt and return
        if _defer_deep_work:
            await self._work_queue.enqueue("deep_work", prompt, priority=3)
            self._downtime_tracker.item_queued()
            logger.info("Research: deep work prompt deferred to work queue (Claude unavailable)")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
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
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", "claude_generated"),
                                thesis=nh.get("thesis", ""),
                                sport=nh.get("sport", "basketball_nba"),
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config={
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
                                },
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
                "SUM(CASE WHEN actual_result='won' THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN actual_result='lost' THEN 1 ELSE 0 END) losses, "
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

        if not claude_available():
            # Defer system improvement to queue for when Claude returns
            await self._work_queue.enqueue("system_improvement", prompt, priority=4)
            self._downtime_tracker.item_queued()
            logger.info("Research: system improvement deferred to work queue (Claude unavailable)")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
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

    async def _phase_integrity_check(self) -> None:
        """
        Pipeline integrity check — detects silent failures that the
        standard health check misses.

        Runs every INTEGRITY_CHECK_INTERVAL_CYCLES cycles. Checks that
        pipelines are not just running but producing valid, changing,
        non-zero output.
        """
        from tools.pipeline_integrity import get_checker, INTEGRITY_CHECK_INTERVAL_CYCLES

        if self._cycles % INTEGRITY_CHECK_INTERVAL_CYCLES != 0:
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

    def get_status(self) -> dict:
        """Return research loop status."""
        from tools.claude_code import get_usage_stats as claude_stats
        from tools.pipeline_integrity import get_checker

        # Include pipeline integrity info
        integrity_report = get_checker().get_latest_report()

        # Include work queue status (async call — best-effort)
        work_queue_status = {}
        try:
            import asyncio
            work_queue_status = asyncio.get_event_loop().run_until_complete(
                self._work_queue.get_status()
            ) if not asyncio.get_event_loop().is_running() else {}
        except Exception:
            pass

        return {
            "running": self._running,
            "cycles_completed": self._cycles,
            "data_collections": self._data_collections,
            "hypotheses_generated": self._hypotheses_generated,
            "backtests_run": self._backtests_run,
            "claude_escalations": self._claude_escalations,
            "promotions": self._promotions,
            "rejections": self._rejections,
            "focus_areas": self.focus_manager._focus_areas,
            "claude_code": claude_stats(),
            "pipeline_integrity": integrity_report,
            "work_queue": work_queue_status,
            "claude_downtime": self._downtime_tracker.get_status(),
            "intervals": {
                "research_cycle_seconds": RESEARCH_CYCLE_INTERVAL,
                "data_collection_seconds": DATA_COLLECTION_INTERVAL,
                "hypothesis_gen_seconds": HYPOTHESIS_GEN_INTERVAL,
                "claude_cooldown_seconds": CLAUDE_ESCALATION_COOLDOWN,
            },
        }
