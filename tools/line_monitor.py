"""
Line movement monitor — autonomous odds snapshot engine.

Takes periodic snapshots of live odds across sports and detects
significant line movements. This is where edges are found:
- Large movement after observable event = potential +EV
- Movement direction vs event impact = gauge market efficiency
- Cross-bookmaker divergence = arbitrage or soft book edge

Runs as a background task within the Callisto API lifecycle.
Stores snapshots in SQLite for historical analysis.

Internals live in tools/lines/: ingest (snapshot conversion/merging),
edge_report (consensus + +EV evaluation), movement (tracking/KL),
monitor_loop (cycle body), schema (DDL bootstrap), snapshot_ops
(snapshot storage/CLV/microstructure), ws_stream (WS + incremental
ingestion) and process_snapshot (per-sport fetch, enrichment, storage,
movement/EV pipeline — slice 5).
The LineMonitor import path is unchanged.
"""

import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import aiosqlite

from tools.odds_api import (
    get_credit_status,
)
from tools.action_network_scraper import scrape_action_network  # noqa: F401 (back-compat re-export)
# oddspapi removed 2026-04-18 (per Marco: "NO MORE ODDS-PAPI"). We already have
# odds-api.io Pro with superior coverage + DK/FD/Action Network scrapers as
# fallbacks; oddspapi was redundant and was spending our 250/month quota on
# sports we cover elsewhere.
from tools.prop_scraper_free import ensure_prop_schema  # noqa: F401 (back-compat re-export)
from tools.lines.ingest import (
    WS_SPORT_TO_MONITORED,
    canonicalize_book_top,
    enrich_with_scraper,
    merge_delta_into_snapshot,
    merge_free_snapshots,
    matchup_key,
    stamp_snapshot_fetched_at,
    ws_sport_to_monitored,
    ws_update_to_snapshot,
)
from tools.lines.edge_report import (
    MIN_EDGE_ALERT,
    MovementEvaluator,
    check_model_agreement,
    compute_devig_consensus,
    extract_implied_probs,
)
from tools.lines.movement import (
    POINT_MOVEMENT_THRESHOLD,
    PRICE_MOVEMENT_THRESHOLD,
    KLDivergenceTracker,
    MovementRecorder,
    extract_probs,
    filter_significant,
)
from tools.lines.monitor_loop import (
    collect_status_counts,
    compute_adaptive_interval,
    fetch_ev_opportunities,
    fetch_recent_movements,
    fetch_snapshot_history,
    handle_sharp_signals,
    record_significant_movements,
    run_monitor_cycle,
    snapshot_props,
    snapshot_sport_fallback,
)
from tools.lines.process_snapshot import (
    capture_closing_lines as _capture_closing_lines_impl,
    enrich_with_dk as _enrich_with_dk_impl,
    enrich_with_fanatics as _enrich_with_fanatics_impl,
    enrich_with_fd as _enrich_with_fd_impl,
    enrich_with_mgm as _enrich_with_mgm_impl,
    evaluate_movement as _evaluate_movement_impl,
    fallback_snapshot as _fallback_snapshot_impl,
    model_agreement as _model_agreement_impl,
    process_snapshot_inner as _process_snapshot_inner_impl,
    record_movement as _record_movement_core,
    snapshot_sport as _snapshot_sport_impl,
)
from tools.lines.schema import connect_and_tag, ensure_line_schema
from tools.lines.snapshot_ops import (
    capture_closing_lines as _capture_closing_lines_ops_impl,
    cache_snapshot_for_backtest,
    default_closing_window,
    insert_snapshot_record,
    normalize_close_source,
    record_line_movement as _record_movement_impl,
    store_market_microstructure,
)
from tools.lines.ws_stream import (
    handle_ws_update as _handle_ws_update_impl,
    incremental_loop as _incremental_loop_impl,
    start_ws as _start_ws_impl,
    stop_ws_and_incremental as _stop_ws_and_incremental_impl,
    ws_status_fields as _ws_status_fields_impl,
)

load_dotenv()

logger = logging.getLogger("callisto.line_monitor")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Snapshot interval in seconds — balance freshness vs credit burn
# 500 credits/month ≈ 16/day. Each snapshot = markets × regions credits.
# Default: 15 min intervals, 3 markets, 1 region = 3 credits/snap = ~5 snaps/day budget
SNAPSHOT_INTERVAL = int(os.getenv("ODDS_SNAPSHOT_INTERVAL", "900"))

# Sports to monitor — configurable via env, comma-separated
MONITORED_SPORTS = os.getenv(
    "ODDS_MONITORED_SPORTS",
    "basketball_nba,icehockey_nhl,americanfootball_nfl,baseball_mlb,basketball_ncaab,basketball_ncaaw,soccer_mls,golf_pga",
).split(",")

# --- Event-driven odds update config ----------------------------------------
# These knobs flip Callisto from "poll every 15 min" to event-driven freshness:
#   * WS_SPORTS — odds-api.io sport slugs to stream live (comma-separated).
#     Maps many-to-one onto MONITORED_SPORTS via WS_SPORT_TO_MONITORED below.
#   * WS_ENABLED — flip to 0 to disable WS entirely (fall back to 15-min poll).
#   * INCREMENTAL_ENABLED — /odds/updated?since=X polling every 60s as a
#     gap-filler between WS drops.
#   * REQUIRE_MODEL_AGREEMENT — gate ev_opportunities on independent model
#     confirmation. Default on; set to 0 to revert to steam-only emission.
WS_SPORTS = os.getenv(
    "CALLISTO_WS_SPORTS", "basketball,american-football,baseball,ice-hockey"
)
WS_ENABLED = os.getenv("CALLISTO_WS_ENABLED", "1") == "1"
INCREMENTAL_ENABLED = os.getenv("CALLISTO_INCREMENTAL_ENABLED", "1") == "1"
INCREMENTAL_INTERVAL = int(os.getenv("CALLISTO_INCREMENTAL_INTERVAL_S", "60"))
REQUIRE_MODEL_AGREEMENT = os.getenv("CALLISTO_REQUIRE_MODEL_AGREEMENT", "1") == "1"


def _ws_sport_to_monitored(ws_sport: str, ws_league: str = "") -> Optional[str]:
    """Back-compat wrapper — see tools.lines.ingest.ws_sport_to_monitored."""
    return ws_sport_to_monitored(ws_sport, ws_league)


def _ws_update_to_snapshot(data: dict) -> Optional[tuple[str, dict]]:
    """Back-compat wrapper — see tools.lines.ingest.ws_update_to_snapshot."""
    return ws_update_to_snapshot(data)


def _merge_delta_into_snapshot(base: dict, delta: dict, now_iso: str) -> dict:
    """Back-compat wrapper — see tools.lines.ingest.merge_delta_into_snapshot."""
    return merge_delta_into_snapshot(base, delta, now_iso)


def _stamp_snapshot_fetched_at(snapshot: dict, now_iso: str) -> None:
    """Back-compat wrapper — see tools.lines.ingest.stamp_snapshot_fetched_at."""
    return stamp_snapshot_fetched_at(snapshot, now_iso)


class LineMonitor:
    """Autonomous line movement detection engine."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False  # Set True to pause snapshot writes (during backtests)
        self._pause_ack = asyncio.Event()  # Signals when monitor has entered paused state (no in-flight DB ops)
        self._in_flight_db = False  # True while a snapshot DB write is in progress
        self._snapshot_lock = asyncio.Lock()  # Atomic guard around _process_snapshot
        self._snapshots: dict[str, dict] = {}  # sport -> last snapshot (only latest per sport)
        self._alerts: deque = deque(maxlen=100)  # Hard-capped at 100 (was unbounded list)
        self._latest_edge_reports: dict[str, dict] = {}  # sport -> latest edge scan (only latest per sport)
        # Self-healing: track consecutive all-source failures per sport.
        # Alert via Telegram only after 3+ consecutive failures.
        self._consecutive_failures: dict[str, int] = {}  # sport -> count
        self._FAILURE_ALERT_THRESHOLD = 3

        # Extracted collaborators (tools/lines/)
        self._kl_tracker = KLDivergenceTracker(db_path=db_path)
        self._evaluator: Optional[MovementEvaluator] = None

        # Event-driven odds state (WS + incremental poll) -------------------
        # _ws_client holds the odds-api.io WebSocket handle; _incremental_task
        # holds the /odds/updated?since=X poller. Both are None when the
        # feature is disabled or failed to initialize; the 15-min snapshot
        # loop runs regardless so WS outages degrade gracefully.
        self._ws_client = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_updates_received = 0
        self._ws_last_update_at: Optional[float] = None
        self._incremental_task: Optional[asyncio.Task] = None
        # Unix seconds of the last /odds/updated poll, per sport. Used as
        # the `since` cursor on the next call.
        self._last_incremental_since: dict[str, int] = {}

    async def initialize(self) -> None:
        """Create tables for odds snapshots and alerts.

        Delegates DDL to tools.lines.schema (per-statement DDL avoids
        EXCLUSIVE lock contention — security audit C-6).
        """
        self._db = await connect_and_tag(self.db_path)
        await ensure_line_schema(self._db)
        # Ensure prop_snapshots table exists
        await ensure_prop_schema(self.db_path)
        logger.info("Line monitor initialized (with prop snapshots)")

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if self._running:
            return
        self._running = True
        # Take immediate startup snapshots before the loop begins.
        # The autonomous loop has a 15s startup delay — use that window
        # to get at least one round of fresh data before it pauses us.
        for sport in MONITORED_SPORTS:
            try:
                await self._snapshot_sport(sport.strip())
            except Exception as e:
                logger.warning(f"Startup snapshot for {sport} failed: {e}")
        self._task = asyncio.create_task(self._monitor_loop())

        # Event-driven paths — non-blocking: failure to open WS must NOT
        # prevent the 15-min safety loop from running.
        if WS_ENABLED:
            try:
                await self._start_ws()
            except Exception as e:
                logger.warning(f"WS startup failed (will retry in background): {e}")
        if INCREMENTAL_ENABLED:
            self._incremental_task = asyncio.create_task(self._incremental_loop())

        logger.info(
            f"Line monitor started — {len(MONITORED_SPORTS)} sports, "
            f"{SNAPSHOT_INTERVAL}s interval "
            f"(ws={'on' if WS_ENABLED else 'off'}, "
            f"incremental={'on' if INCREMENTAL_ENABLED else 'off'})"
        )

    async def stop(self) -> None:
        """Stop the monitoring loop and close DB."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Stop WS and incremental tasks (each teardown isolated — see ws_stream).
        await _stop_ws_and_incremental_impl(self)
        if self._db:
            await self._db.close()
        logger.info("Line monitor stopped")

    # --- WebSocket path -----------------------------------------------------
    async def _start_ws(self) -> None:
        """Open the odds-api.io WebSocket and wire updates into _process_ws_update.

        Delegates to tools.lines.ws_stream.start_ws.
        """
        await _start_ws_impl(self)

    async def _handle_ws_update(self, data: dict) -> None:
        """WS callback — merge a single delta into our latest snapshot.

        Delegates to tools.lines.ws_stream.handle_ws_update; this wrapper
        owns the update counters and isolates detector failures from
        odds ingestion.
        """
        self._ws_updates_received += 1
        self._ws_last_update_at = time.time()
        try:

            async def _eval_detectors(event_id: str):
                from tools.live_state import evaluate_detectors_for_event
                await evaluate_detectors_for_event(event_id, db_path=self.db_path)

            await _handle_ws_update_impl(
                self, data,
                process_snapshot=self._process_snapshot,
                evaluate_live_detectors=_eval_detectors,
            )
        except Exception as e:
            logger.warning(f"WS update handler failed: {e}")

    # --- Incremental /odds/updated path -------------------------------------
    async def _incremental_loop(self) -> None:
        """Poll /odds/updated?since=X — delegates to tools.lines.ws_stream."""
        await _incremental_loop_impl(self, monitored_sports=MONITORED_SPORTS)

    def get_ws_status(self) -> dict:
        """Telemetry snapshot — exposed via /health and /system/full-status."""
        return _ws_status_fields_impl(self)

    async def wait_for_drain(self, timeout: float = 60) -> bool:
        """Pause the monitor and wait until all in-flight DB ops complete.

        Sets _paused, waits for _pause_ack, then ACQUIRES the snapshot lock
        to guarantee no new snapshot can start. Returns True if drained.
        Caller MUST eventually call resume() to release the lock.
        """
        self._paused = True
        deadline = time.monotonic() + timeout
        # SECURITY (audit C-8): acquire the lock FIRST, then verify under-lock that
        # ack is set and no DB op is in flight. Previously we checked ack outside the
        # lock and then acquired — between those two operations a snapshot could start
        # and set _in_flight_db=True, leaving the caller with the lock but a live writer
        # racing it. By holding the lock during verification we guarantee mutual
        # exclusion: if someone else has the lock we wait; once we hold it no new
        # snapshot can begin (the loop body acquires _snapshot_lock before doing work).
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(
                    self._snapshot_lock.acquire(),
                    timeout=max(1.0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                break
            # Lock held — re-check invariants under it.
            if self._pause_ack.is_set() and not self._in_flight_db:
                return True
            # Caller hasn't fully drained; release and retry after a short sleep.
            try:
                self._snapshot_lock.release()
            except RuntimeError:
                pass
            await asyncio.sleep(0.5)
        logger.warning(
            f"wait_for_drain timed out after {timeout}s "
            f"(ack={self._pause_ack.is_set()}, in_flight={self._in_flight_db})"
        )
        return False

    def resume(self) -> None:
        """Release the drain lock and unpause the monitor.

        Must be called after wait_for_drain() succeeds, in a try/finally
        block to guarantee the lock is released.
        """
        self._paused = False
        if self._snapshot_lock.locked():
            try:
                self._snapshot_lock.release()
            except RuntimeError:
                # Already released — non-fatal
                pass

    async def _monitor_loop(self) -> None:
        """Main monitoring loop — snapshot, compare, alert.

        Delegates each cycle body to tools.lines.monitor_loop.run_monitor_cycle
        (credit-aware fallback switch, adaptive interval stretch, per-sport
        backoff, free prop cascade). This wrapper owns the pause/ack
        handshake and the inter-cycle sleep.
        """
        while self._running:
            # Yield to backtests when paused
            if self._paused:
                self._pause_ack.set()
                await asyncio.sleep(5)
                continue
            self._pause_ack.clear()
            try:
                interval = await run_monitor_cycle(
                    self,
                    monitored_sports=MONITORED_SPORTS,
                    snapshot_interval=SNAPSHOT_INTERVAL,
                    get_credit_status=get_credit_status,
                )

                # If paused mid-cycle (broke out of sport loop), skip the
                # interval sleep and loop back immediately so _pause_ack fires.
                # Without this, autonomous waits 30s, always times out, and
                # proceeds with WAL-contending DB writes.
                if self._paused:
                    continue

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                await asyncio.sleep(30)

    async def _snapshot_props(self) -> None:
        """Scrape player props from all free sources for all monitored sports.

        Delegates to tools.lines.monitor_loop.snapshot_props.
        """
        await snapshot_props(self, MONITORED_SPORTS)

    async def _snapshot_sport_fallback(self, sport: str) -> None:
        """Take an odds snapshot using all available sources.

        Called when Odds API credits are exhausted or unavailable.
        Delegates to tools.lines.process_snapshot.fallback_snapshot
        (which forwards to tools.lines.monitor_loop.snapshot_sport_fallback).
        """
        await _fallback_snapshot_impl(self, sport)

    async def _snapshot_sport(self, sport: str) -> None:
        """Take an odds snapshot for a sport and compare with previous.

        Always enriches with DK + FanDuel scraper data (free) to ensure:
        1. DK/FD lines are fresh from source (target books)
        2. More bookmakers in the snapshot = better devig consensus
        3. If Odds API data is stale, scrapers overwrite it

        Delegates to tools.lines.process_snapshot.snapshot_sport.
        """
        await _snapshot_sport_impl(self, sport)

    async def _enrich_with_dk(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh DK scraper data into an Odds API snapshot (see ingest.enrich_with_scraper)."""
        return await _enrich_with_dk_impl(sport, snapshot)

    async def _enrich_with_fd(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh FanDuel scraper data into an odds snapshot (see ingest.enrich_with_scraper)."""
        return await _enrich_with_fd_impl(sport, snapshot)

    async def _enrich_with_mgm(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh BetMGM scraper data into an odds snapshot (see ingest.enrich_with_scraper)."""
        return await _enrich_with_mgm_impl(sport, snapshot)

    async def _enrich_with_fanatics(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh Fanatics scraper data into an odds snapshot.

        Delegates to tools.lines.process_snapshot.enrich_with_fanatics.
        Silent on failure — the Fanatics endpoints are UNDOCUMENTED and we
        expect them to break periodically; @tracked_ingestion records the outage.
        """
        return await _enrich_with_fanatics_impl(sport, snapshot)

    @staticmethod
    def _matchup_key(home: str, away: str) -> str:
        """Normalize team names into a matchup key for cross-source matching."""
        return matchup_key(home, away)

    async def _process_snapshot(self, sport: str, new_snapshot: dict) -> None:
        """Process an odds snapshot — store, scan edges, detect movements.
        Acquires _snapshot_lock so wait_for_drain() can guarantee no
        in-flight snapshot is running. Sets _in_flight_db for legacy callers.

        Shared pipeline used by both primary (Odds API) and fallback
        (DraftKings scraper) snapshot paths.
        """
        async with self._snapshot_lock:
            self._in_flight_db = True
            try:
                await self._process_snapshot_inner(sport, new_snapshot)
            finally:
                self._in_flight_db = False

    async def _process_snapshot_inner(self, sport: str, new_snapshot: dict) -> None:
        """Inner snapshot processing — separated so _in_flight_db wraps all DB ops.

        Delegates to tools.lines.process_snapshot.process_snapshot_inner.
        """
        await _process_snapshot_inner_impl(self, sport, new_snapshot)

    async def _capture_closing_lines(self, sport: str, snapshot: dict) -> None:
        """Push closing lines to CLV tracker for games about to start.

        Delegates to tools.lines.process_snapshot.capture_closing_lines
        (which forwards to tools.lines.snapshot_ops.capture_closing_lines).
        """
        await _capture_closing_lines_impl(self, sport, snapshot)

    async def _record_movement(self, sport: str, movement: dict) -> None:
        """Record a line movement (delegates to tools.lines.process_snapshot)."""
        await _record_movement_core(self, sport, movement)

    async def _compute_and_store_kl(self, sport: str, old_snapshot: dict, new_snapshot: dict) -> None:
        """Compute KL divergence between two consecutive snapshots per game.

        Delegates to tools.lines.movement.KLDivergenceTracker.
        """
        await self._kl_tracker.compute_and_store(sport, old_snapshot, new_snapshot)

    @staticmethod
    def _extract_implied_probs(game: dict, market_type: str) -> list[float]:
        """Extract implied probabilities for the first outcome across all bookmakers."""
        return extract_implied_probs(game, market_type)

    def get_kl_for_game(self, sport: str, event_id: str, market_type: str = "h2h") -> Optional[dict]:
        """Look up cached KL metrics for a game. Used by edge_confidence scoring."""
        return self._kl_tracker.get_for_game(sport, event_id, market_type)

    async def _evaluate_movement(self, sport: str, movement: dict, snapshot: dict) -> None:
        """Evaluate whether a line movement creates a +EV opportunity.

        Delegates to tools.lines.process_snapshot.evaluate_movement; the
        evaluator itself (tools.lines.edge_report.MovementEvaluator) is
        lazily constructed against this monitor's DB connection.
        """
        await _evaluate_movement_impl(
            self, sport, movement, snapshot,
            require_model_agreement=REQUIRE_MODEL_AGREEMENT,
        )

    async def _check_model_agreement(
        self, *, sport: str, game: dict, team: str, market: str, direction: str,
    ) -> tuple[bool, str]:
        """Return (ok, label) indicating whether any registered model agrees.

        Delegates to tools.lines.edge_report.check_model_agreement using the
        latest cached edge report for this sport.
        """
        return _model_agreement_impl(
            self, sport=sport, game=game, team=team, market=market,
            direction=direction,
        )

    async def get_recent_movements(self, sport: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Get recent line movements from the database."""
        return await fetch_recent_movements(self._db, sport=sport, limit=limit)

    async def get_ev_opportunities(self, status: str = "open", limit: int = 20) -> list[dict]:
        """Get current +EV opportunities."""
        return await fetch_ev_opportunities(self._db, status=status, limit=limit)

    async def get_snapshot_history(self, sport: str, limit: int = 10) -> list[dict]:
        """Get snapshot history for a sport (metadata only, no full JSON)."""
        return await fetch_snapshot_history(self._db, sport, limit=limit)

    async def get_status(self) -> dict:
        """Return monitor status with DB-backed counts."""
        counts = await collect_status_counts(self._db) if self._db else {}

        return {
            "running": self._running,
            "monitored_sports": MONITORED_SPORTS,
            "snapshot_interval_seconds": SNAPSHOT_INTERVAL,
            "cached_snapshots": list(self._snapshots.keys()),
            "db_snapshots_total": counts.get("db_snapshots_total", 0),
            "db_movements_total": counts.get("db_movements_total", 0),
            "db_closing_lines": counts.get("db_closing_lines", 0),
            "latest_snapshot_at": counts.get("latest_snapshot_at"),
            "recent_alerts_in_memory": len(self._alerts),
            "credits": get_credit_status(),
        }

    def get_edge_report(self, sport: Optional[str] = None) -> dict:
        """Get the latest edge scan report."""
        if sport:
            return self._latest_edge_reports.get(sport, {"error": f"No report for {sport}"})
        return self._latest_edge_reports

    async def force_snapshot(self, sport: str) -> dict:
        """Manually trigger a snapshot for a sport. Returns the snapshot data."""
        await self._snapshot_sport(sport)
        return self._snapshots.get(sport, {"error": "No snapshot taken"})
