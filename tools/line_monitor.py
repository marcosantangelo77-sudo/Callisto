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
edge_report (consensus + +EV evaluation), movement (tracking/KL).
The LineMonitor import path is unchanged.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.odds_api import (
    get_odds,
    get_scores,
    find_best_line,
    detect_line_movement,
    calculate_ev,
    calculate_implied_probability,
    get_credit_status,
)
from tools.devig import power_devig
from tools.math_utils import american_to_decimal
from tools.edge_scanner import full_edge_scan, detect_sharp_money
from tools.kl_divergence import kl_divergence, jensen_shannon, shannon_entropy, store_kl_metrics
from tools.parlay_scanner import find_correlated_parlay_edges, analyze_live_overreaction
from tools import telegram
from tools.dk_scraper import scrape_dk_odds
from tools.action_network_scraper import scrape_action_network  # noqa: F401 (back-compat re-export)
from tools.fanduel_scraper import scrape_fd_odds
from tools.betmgm_scraper import scrape_betmgm_odds
from tools.fanatics_scraper import fetch_fanatics_odds
from tools.odds_api_io import (
    get_odds as odds_api_io_get_odds,
    get_usage_status as odds_api_io_usage,
    get_value_bets as odds_api_io_value_bets,
)
# oddspapi removed 2026-04-18 (per Marco: "NO MORE ODDS-PAPI"). We already have
# odds-api.io Pro with superior coverage + DK/FD/Action Network scrapers as
# fallbacks; oddspapi was redundant and was spending our 250/month quota on
# sports we cover elsewhere.
from tools.prop_scraper_free import scrape_all_props, store_prop_snapshot, ensure_prop_schema
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
from tools.lines.snapshot_ops import (
    cache_snapshot_for_backtest,
    capture_closing_lines as _capture_closing_lines_impl,
    default_closing_window,
    insert_snapshot_record,
    normalize_close_source,
    record_line_movement as _record_movement_impl,
    store_market_microstructure,
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
        from collections import deque
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
        """Create tables for odds snapshots and alerts."""
        self._db = await aiosqlite.connect(self.db_path)
        # Tag for WriteCoordinator routing (single-writer pattern).
        from tools.db_writer import tag_connection as _tag
        _tag(self._db, self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 120000")  # 2 min — 5 min caused cascading WAL stalls
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (
            """CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                game_count INTEGER DEFAULT 0,
                credits_remaining INTEGER,
                -- fetched_at records our ingest time (not the book's
                -- last_update). Used by edge_scanner.weighted_sharp_consensus
                -- to decay stale lines. See schema.py migration for details.
                fetched_at TEXT,
                -- source tracks origin: 'interval' (default 15-min poll),
                -- 'ws' (WebSocket push), 'incremental' (/odds/updated),
                -- 'scraper_fallback'. Useful for debugging freshness tiers.
                source TEXT DEFAULT 'interval'
            )""",
            """CREATE TABLE IF NOT EXISTS line_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                team TEXT,
                market TEXT,
                bookmaker TEXT,
                old_price INTEGER,
                new_price INTEGER,
                price_movement INTEGER,
                old_point REAL,
                new_point REAL,
                point_movement REAL,
                direction TEXT,
                ev_analysis TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ev_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at TEXT NOT NULL,
                sport TEXT,
                game_id TEXT,
                team TEXT,
                market TEXT,
                bookmaker TEXT,
                american_odds INTEGER,
                implied_probability REAL,
                estimated_true_prob REAL,
                edge REAL,
                expected_value REAL,
                kelly_fraction REAL,
                status TEXT DEFAULT 'open',
                -- source distinguishes signal provenance: 'line_movement' (default,
                -- from line_monitor edge scan), 'odds_api_io_pro' (value bets from
                -- the provider's pre-computed +EV feed), or 'arbitrage' (cross-book
                -- guaranteed-profit opportunities). Added 2026-04-18 to unify the
                -- two writer paths (line_monitor + autonomous) on one schema.
                source TEXT DEFAULT 'line_movement'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_snapshots_sport_ts ON odds_snapshots(sport, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_movements_sport ON line_movements(sport, detected_at)",
            "CREATE INDEX IF NOT EXISTS idx_ev_status ON ev_opportunities(status, detected_at)",
        ):
            await self._db.execute(stmt)
        await self._db.commit()
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
        # Stop WS and incremental tasks. Each wrapped in its own try so a
        # failure in one doesn't block the other.
        if self._ws_client is not None:
            try:
                await self._ws_client.stop()
            except Exception as e:
                logger.warning(f"WS stop error: {e}")
            self._ws_client = None
        if self._incremental_task is not None:
            self._incremental_task.cancel()
            try:
                await self._incremental_task
            except (asyncio.CancelledError, Exception):
                pass
            self._incremental_task = None
        if self._db:
            await self._db.close()
        logger.info("Line monitor stopped")

    # --- WebSocket path -----------------------------------------------------
    async def _start_ws(self) -> None:
        """Open the odds-api.io WebSocket and wire updates into _process_ws_update.

        The WS client has its own reconnect loop (5s→60s backoff with jitter)
        inside tools/odds_ws.py, so we just hand it a callback and let it run.
        """
        # Imported locally to avoid a hard dep at module import time — lets
        # CALLISTO_WS_ENABLED=0 environments skip the websockets package.
        from tools.odds_ws import OddsWebSocket

        self._ws_client = OddsWebSocket(
            on_update=self._handle_ws_update,
            sports=WS_SPORTS,
        )
        await self._ws_client.start()

    async def _handle_ws_update(self, data: dict) -> None:
        """WS callback — merge a single delta into our latest snapshot.

        Each WS message covers ONE bookmaker's quotes for ONE event across
        several markets. We turn it into a minimal snapshot-shaped payload
        and route through the same _process_snapshot pipeline so edge
        detection and movement evaluation fire on every delta.

        Additionally fires live-edge detectors for the event if a live
        game state exists — the 30s poller may have stale odds while the
        WS already has a new price, so piggy-backing here catches rapid
        reactive edges between polls.
        """
        self._ws_updates_received += 1
        self._ws_last_update_at = time.time()

        try:
            mapped = ws_update_to_snapshot(data)
            if not mapped:
                return
            sport_key, snap = mapped
            snap["ingest_source"] = "ws"
            # Run through the normal pipeline — this writes fetched_at,
            # triggers edge rescoring for the affected market, and invokes
            # movement evaluation for changed prices.
            await self._process_snapshot(sport_key, snap)
            # Piggy-back live-edge detector eval. Only fires when the
            # event has a live_game_state row — function no-ops otherwise,
            # so there's no penalty for pre-game events. Isolate errors:
            # detector failures must NOT break odds ingestion.
            try:
                for game in (snap.get("games") or [])[:5]:
                    eid = str(game.get("id") or "").strip()
                    if not eid:
                        continue
                    from tools.live_state import evaluate_detectors_for_event
                    await evaluate_detectors_for_event(eid, db_path=self.db_path)
            except Exception as e:
                logger.debug(f"WS-path live-edge eval failed: {e}")
        except Exception as e:
            logger.warning(f"WS update handler failed: {e}")

    # --- Incremental /odds/updated path -------------------------------------
    async def _incremental_loop(self) -> None:
        """Poll /odds/updated?since=X every INCREMENTAL_INTERVAL seconds.

        This is the gap-filler between the WS firehose and the 15-min
        safety snapshot: if WS drops for 30s we still catch the delta on
        the next incremental tick. `since` is tracked per-sport so a
        crash-restart still resumes roughly where it left off.
        """
        try:
            from tools.odds_api_io import get_odds_updated as _incremental_fetch
        except Exception:
            logger.warning("odds_api_io.get_odds_updated unavailable — disabling incremental loop")
            return

        while self._running:
            try:
                await asyncio.sleep(INCREMENTAL_INTERVAL)
                if self._paused:
                    continue
                now_unix = int(time.time())
                for sport in MONITORED_SPORTS:
                    sport = sport.strip()
                    since = self._last_incremental_since.get(sport, now_unix - 60)
                    try:
                        result = await _incremental_fetch(since, sport=sport)
                    except Exception as e:
                        logger.debug(f"Incremental fetch failed for {sport}: {e}")
                        continue
                    self._last_incremental_since[sport] = now_unix
                    if not isinstance(result, dict):
                        continue
                    updates = result.get("updates") or []
                    if not updates:
                        continue
                    # Each update has the same shape as a WS message —
                    # reuse the same converter.
                    for upd in updates:
                        mapped = ws_update_to_snapshot(upd)
                        if not mapped:
                            continue
                        s_key, snap = mapped
                        snap["ingest_source"] = "incremental"
                        try:
                            await self._process_snapshot(s_key, snap)
                        except Exception as e:
                            logger.debug(f"Incremental _process_snapshot failed: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"Incremental loop error: {e}")

    def get_ws_status(self) -> dict:
        """Telemetry snapshot — exposed via /health and /system/full-status."""
        base = {
            "ws_enabled": WS_ENABLED,
            "incremental_enabled": INCREMENTAL_ENABLED,
            "ws_updates_received": self._ws_updates_received,
            "ws_last_update_ago_s": (
                round(time.time() - self._ws_last_update_at, 1)
                if self._ws_last_update_at else None
            ),
            "require_model_agreement": REQUIRE_MODEL_AGREEMENT,
        }
        if self._ws_client is not None:
            try:
                base.update({"ws_client": self._ws_client.get_status()})
            except Exception:
                pass
        return base

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

        When Odds API credits are low (<10 remaining), switches to free
        fallback sources instead of pausing for an hour:
        1. DraftKings scraper (free, unlimited)
        2. OddsPapi (250/month free tier)
        """
        while self._running:
            # Yield to backtests when paused
            if self._paused:
                self._pause_ack.set()
                await asyncio.sleep(5)
                continue
            self._pause_ack.clear()
            try:
                credits = get_credit_status()
                use_fallback = False

                if not credits.get("api_key_set"):
                    # No Odds API key at all — go straight to fallbacks
                    logger.info("ODDS_API_KEY not set — using free fallback sources")
                    use_fallback = True

                remaining = credits.get("remaining")
                if remaining is not None and remaining < 50:
                    logger.info(f"Odds API credits low ({remaining}) — switching to free scrapers (DK + FanDuel)")
                    use_fallback = True

                # Adaptive interval: stretch credits across the month
                # ~9 credits per full cycle (3 sports × 3 markets)
                interval = SNAPSHOT_INTERVAL
                if not use_fallback and remaining is not None:
                    if remaining < 50:
                        interval = max(SNAPSHOT_INTERVAL, 3600)  # 1hr when low
                        logger.info(f"Credits low ({remaining}) — slowing to {interval}s")
                    elif remaining < 100:
                        interval = max(SNAPSHOT_INTERVAL, 1800)  # 30min when moderate

                # Cycle counter for backoff scheduling
                if not hasattr(self, "_cycle_n"):
                    self._cycle_n = 0
                self._cycle_n += 1

                for sport in MONITORED_SPORTS:
                    if self._paused:
                        break  # Exit early — autonomous loop waiting for us
                    s = sport.strip()
                    # Backoff for out-of-season / chronically failing sports:
                    # 5+ consecutive failures → skip 3 cycles between attempts
                    # 10+ → skip 7 cycles between attempts
                    fail_count = self._consecutive_failures.get(s, 0)
                    if fail_count >= 10 and self._cycle_n % 8 != 0:
                        continue
                    if fail_count >= 5 and self._cycle_n % 4 != 0:
                        continue
                    try:
                        if use_fallback:
                            await asyncio.wait_for(self._snapshot_sport_fallback(s), timeout=120)
                        else:
                            await asyncio.wait_for(self._snapshot_sport(s), timeout=120)
                    except asyncio.TimeoutError:
                        logger.warning(f"Snapshot for {s} timed out after 120s — skipping")
                        self._consecutive_failures[s] = self._consecutive_failures.get(s, 0) + 1

                # Prop snapshots — free cascade (DK + FD + BetMGM), no credits
                if not self._paused:
                    try:
                        await asyncio.wait_for(self._snapshot_props(), timeout=180)
                    except asyncio.TimeoutError:
                        logger.warning("Prop snapshot timed out after 180s — skipping")

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

        Runs the DK + FanDuel + BetMGM prop cascade for each sport,
        stores results in prop_snapshots table. Zero credit cost.
        """
        # Only scrape props for sports that have prop markets
        prop_sports = [s.strip() for s in MONITORED_SPORTS
                       if s.strip() in ("basketball_nba", "baseball_mlb",
                                        "icehockey_nhl", "americanfootball_nfl")]
        if not prop_sports:
            return

        total_stored = 0
        for sport in prop_sports:
            if self._paused:
                break
            try:
                result = await scrape_all_props(sport)
                if result.get("error") or not result.get("props"):
                    continue
                if self._paused:
                    break
                async with self._snapshot_lock:
                    self._in_flight_db = True
                    try:
                        stored = await store_prop_snapshot(result["props"], sport, self.db_path)
                    finally:
                        self._in_flight_db = False
                total_stored += stored
                logger.info(
                    f"Props {sport}: {stored} lines stored "
                    f"({result.get('multi_book_count', 0)} multi-book)"
                )
            except Exception as e:
                logger.warning(f"Prop snapshot failed for {sport}: {e}")

        if total_stored > 0:
            logger.info(f"Prop snapshot cycle complete: {total_stored} total lines stored")

    async def _snapshot_sport_fallback(self, sport: str) -> None:
        """Take an odds snapshot using all available sources.

        Called when Odds API credits are exhausted or unavailable.
        Delegates the scraper cascade to tools.lines.fallback_cascade
        (priority: odds-api.io Pro -> DK -> Action Network -> FD ->
        Fanatics). Merges all successful sources for maximum
        multi-book coverage. BetMGM is disabled and OddsPapi removed —
        see fallback_cascade module docstring.
        """
        from tools.lines.fallback_cascade import collect_free_sources, merge_free_sources

        scraped = await collect_free_sources(
            sport,
            odds_api_io_get_odds=odds_api_io_get_odds,
            odds_api_io_usage=odds_api_io_usage,
        )

        if not scraped:
            # Track consecutive failures for self-healing alerts
            self._consecutive_failures[sport] = self._consecutive_failures.get(sport, 0) + 1
            count = self._consecutive_failures[sport]
            logger.warning(
                f"All fallback sources failed for {sport} — skipping snapshot "
                f"(consecutive failures: {count})"
            )
            if count >= self._FAILURE_ALERT_THRESHOLD:
                try:
                    await telegram.alert_system(
                        f"ALL odds sources failing for {sport} "
                        f"({count} consecutive cycles). Check DK, FD, Action Network, "
                        f"Odds-API.io connectivity.",
                        is_error=True,
                    )
                except Exception:
                    pass  # Don't let Telegram errors break the monitor
            return

        # Reset consecutive failure counter on success
        self._consecutive_failures[sport] = 0

        new_snapshot = merge_free_sources(scraped, sport)
        await self._process_snapshot(sport, new_snapshot)

    async def _snapshot_sport(self, sport: str) -> None:
        """Take an odds snapshot for a sport and compare with previous.

        Always enriches with DK + FanDuel scraper data (free) to ensure:
        1. DK/FD lines are fresh from source (target books)
        2. More bookmakers in the snapshot = better devig consensus
        3. If Odds API data is stale, scrapers overwrite it
        """
        try:
            # Primary: Odds-API.io Pro (15 books, 30K req/hr)
            # the-odds-api.com is out of credits — skip it entirely.
            new_snapshot = await odds_api_io_get_odds(sport)

            if new_snapshot.get("error") or not new_snapshot.get("games"):
                logger.warning(f"Snapshot error for {sport}: {new_snapshot.get('error', 'no games')} — trying fallbacks")
                await self._snapshot_sport_fallback(sport)
                return

            # Enrich with fresh scraper data from all free sources (always)
            new_snapshot = await self._enrich_with_dk(sport, new_snapshot)
            new_snapshot = await self._enrich_with_fd(sport, new_snapshot)
            new_snapshot = await self._enrich_with_fanatics(sport, new_snapshot)

            await self._process_snapshot(sport, new_snapshot)

        except Exception as e:
            logger.error(f"Snapshot failed for {sport}: {e}")

    async def _enrich_with_dk(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh DK scraper data into an Odds API snapshot (see ingest.enrich_with_scraper)."""
        return await enrich_with_scraper(sport, snapshot, scrape_dk_odds, "draftkings", ("draft_kings",))

    async def _enrich_with_fd(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh FanDuel scraper data into an odds snapshot (see ingest.enrich_with_scraper)."""
        return await enrich_with_scraper(sport, snapshot, scrape_fd_odds, "fanduel", ("fan_duel",))

    async def _enrich_with_mgm(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh BetMGM scraper data into an odds snapshot (see ingest.enrich_with_scraper)."""
        return await enrich_with_scraper(sport, snapshot, scrape_betmgm_odds, "betmgm", ("bet_mgm",))

    async def _enrich_with_fanatics(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh Fanatics scraper data into an odds snapshot.

        Same pattern as the other enrichment helpers. Fanatics is the
        secondary book (per project_sportsbooks) so we always pull a
        fresh scrape when the sport is supported. Silent on failure — the
        Fanatics endpoints are UNDOCUMENTED and we expect them to break
        periodically; @tracked_ingestion records the outage.
        """
        try:
            from tools.fanatics_scraper import FANATICS_LEAGUE_KEYS
        except Exception:
            return snapshot
        if sport not in FANATICS_LEAGUE_KEYS:
            return snapshot
        return await enrich_with_scraper(
            sport, snapshot, fetch_fanatics_odds, "fanatics", ("fanatics_sportsbook",),
        )

    @staticmethod
    def _matchup_key(home: str, away: str) -> str:
        """Normalize team names into a matchup key for cross-source matching."""
        return matchup_key(home, away)

    async def _process_snapshot(self, sport: str, new_snapshot: dict) -> None:
        """Process an odds snapshot — store, scan edges, detect movements.
        Acquires _snapshot_lock so wait_for_drain() can guarantee no
        in-flight snapshot is running. Sets _in_flight_db for legacy callers.

        Shared pipeline used by both primary (Odds API) and fallback
        (DraftKings scraper, OddsPapi) snapshot paths.
        """
        async with self._snapshot_lock:
            self._in_flight_db = True
            try:
                await self._process_snapshot_inner(sport, new_snapshot)
            finally:
                self._in_flight_db = False

    async def _process_snapshot_inner(self, sport: str, new_snapshot: dict) -> None:
        """Inner snapshot processing — separated so _in_flight_db wraps all DB ops."""
        now = datetime.now(timezone.utc).isoformat()
        game_count = new_snapshot.get("game_count", 0)
        credits_remaining = new_snapshot.get("credits", {}).get("remaining")
        source = new_snapshot.get("source", "odds_api")

        # Stamp fetched_at on every bookmaker entry in the snapshot JSON so
        # downstream consumers (backtest replay, CLV backfill, edge rescan)
        # can compute freshness decay even when the outer row timestamp has
        # drifted from the actual fetch time. Idempotent — if a line already
        # has fetched_at (e.g. came from the WS path) we keep the earlier
        # stamp rather than overwriting with a later process-time.
        stamp_snapshot_fetched_at(new_snapshot, now)

        # ingest_source defaults to the snapshot's 'ingest_source' tag; callers
        # in the WS/incremental paths set this to 'ws' or 'incremental'. The
        # legacy 'source' field above is the provider name ('odds_api',
        # 'draftkings', etc.) and is a different axis.
        ingest_source = new_snapshot.get("ingest_source", "interval")

        # WS/incremental deltas arrive as SINGLE-bookmaker, single-game
        # snapshots. If we hand that to _process_snapshot_inner as-is it
        # would overwrite the multi-book _snapshots[sport] with the delta,
        # breaking the next consensus scan. Merge instead: take the most
        # recent full snapshot for this sport and splice the WS delta onto
        # it so downstream edge scanning still has every book present.
        if ingest_source in ("ws", "incremental"):
            prior = self._snapshots.get(sport)
            if prior is not None and new_snapshot.get("games"):
                new_snapshot = merge_delta_into_snapshot(prior, new_snapshot, now)

        # Store snapshot (retry-wrapped; see tools.lines.snapshot_ops).
        await insert_snapshot_record(
            self._db,
            sport=sport,
            snapshot=new_snapshot,
            now_iso=now,
            game_count=game_count,
            credits_remaining=credits_remaining,
            ingest_source=ingest_source,
        )

        logger.info(f"Snapshot {sport} ({source}): {game_count} games, credits={credits_remaining}")

        # Publish snapshot event to event bus
        try:
            from tools.event_bus import get_event_bus, EVENT_SNAPSHOT_TAKEN
            await get_event_bus().publish(EVENT_SNAPSHOT_TAKEN, {
                "sport": sport, "game_count": game_count,
                "source": source, "credits_remaining": credits_remaining,
            })
        except Exception:
            pass  # Event bus not critical

        # ALWAYS cache in historical_odds_cache for backtesting.
        # Every live snapshot becomes backtest-grade data. This is the
        # primary mechanism for building historical depth.
        await cache_snapshot_for_backtest(self._db, sport=sport, snapshot=new_snapshot, now_iso=now)

        # Run edge scanner on every snapshot
        edge_report = full_edge_scan(new_snapshot)
        self._latest_edge_reports[sport] = edge_report
        total_edges = edge_report.get("total_edges", 0)
        if total_edges > 0:
            logger.info(f"Edge scan {sport}: {total_edges} edges found")

        # Store market microstructure metrics from edge scan
        await store_market_microstructure(self._db, sport=sport, edge_report=edge_report, now_iso=now)

        # NOTE: Raw edges are NOT sent to Telegram here.
        # The autonomous loop analyzes candidates via full AGP sessions
        # and only alerts after the Architect confirms the edge is real.

        # Compare with previous snapshot
        old_snapshot = self._snapshots.get(sport)
        if old_snapshot:
            movements = detect_line_movement(old_snapshot, new_snapshot)
            significant = filter_significant(movements)

            if significant:
                logger.info(
                    f"MOVEMENT DETECTED: {sport} — {len(significant)} significant moves"
                )
                for mov in significant:
                    await self._record_movement(sport, mov)
                    await self._evaluate_movement(sport, mov, new_snapshot)
                    # Publish line movement event
                    try:
                        from tools.event_bus import get_event_bus, EVENT_LINE_MOVED
                        await get_event_bus().publish(EVENT_LINE_MOVED, {
                            "sport": sport, **mov,
                        })
                    except Exception:
                        pass

            # Detect sharp money (one book moved, others didn't)
            sharp_signals = detect_sharp_money(old_snapshot, new_snapshot)
            if sharp_signals:
                logger.info(f"SHARP MONEY: {sport} — {len(sharp_signals)} signals")
                for sig in sharp_signals:
                    self._alerts.append({"sport": sport, "type": "sharp_money", **sig})
                    # Alert on high-confidence sharp moves only (3+ stale books)
                    stale = sig.get("stale_books", [])
                    moved = sig.get("moved_books", [])
                    if len(stale) >= 3 and moved:
                        try:
                            from tools.telegram import alert_sharp_move
                            await alert_sharp_move(
                                game=sig.get("game", ""),
                                team=sig.get("team", ""),
                                market=sig.get("market", ""),
                                moved_books=moved,
                                stale_books=stale,
                            )
                        except Exception:
                            pass
                # Cap alerts to prevent unbounded growth
                if len(self._alerts) > 100:
                    self._alerts = self._alerts[-100:]

            # Compute KL divergence between previous and current snapshot
            # Measures information flow — how much the market "learned" between snapshots.
            await self._compute_and_store_kl(sport, old_snapshot, new_snapshot)

        # ── CLV bridge: capture closing lines for games about to start ──
        # If a game starts within the next snapshot interval, this is the
        # last snapshot we'll get before tip-off — treat it as the closing line.
        await self._capture_closing_lines(sport, new_snapshot)

        self._snapshots[sport] = new_snapshot

    async def _capture_closing_lines(self, sport: str, snapshot: dict) -> None:
        """Push closing lines to CLV tracker for games about to start.

        Delegates to tools.lines.snapshot_ops.capture_closing_lines.
        """
        try:
            # Import CLV tracker from the global API state
            from api import clv_tracker as _clv
            if _clv is None:
                return

            await _capture_closing_lines_impl(
                _clv,
                sport=sport,
                snapshot=snapshot,
                closing_window_seconds=default_closing_window(SNAPSHOT_INTERVAL),
            )
        except ImportError:
            pass  # CLV tracker not available
        except Exception as e:
            logger.warning(f"CLV closing line capture failed for {sport}: {e}")

    async def _record_movement(self, sport: str, movement: dict) -> None:
        """Record a line movement (delegates to tools.lines.snapshot_ops)."""
        await _record_movement_impl(self._db, self._alerts, sport=sport, movement=movement)

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

        Delegates to tools.lines.edge_report.MovementEvaluator.
        """
        if self._evaluator is None:

            async def _insert_ev(row: dict) -> None:
                from tools.db_utils import execute_with_retry, commit_with_retry
                await execute_with_retry(
                    self._db,
                    "INSERT INTO ev_opportunities "
                    "(detected_at, sport, game_id, team, market, bookmaker, "
                    "american_odds, implied_probability, estimated_true_prob, "
                    "edge, expected_value, kelly_fraction, steam_only) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["detected_at"], row["sport"], row["game_id"],
                        row["team"], row["market"], row["bookmaker"],
                        row["american_odds"], row["implied_probability"],
                        row["estimated_true_prob"], row["edge"],
                        row["expected_value"], row["kelly_fraction"],
                        row["steam_only"],
                    ),
                    max_retries=5,
                    operation=f"ev_opportunity insert {row['sport']}",
                )
                await commit_with_retry(
                    self._db, max_retries=5,
                    operation=f"ev_opportunity commit {row['sport']}",
                )

            self._evaluator = MovementEvaluator(
                insert_ev=_insert_ev,
                get_edge_report=lambda s: self._latest_edge_reports.get(s),
            )

        await self._evaluator.evaluate(
            sport, movement, snapshot,
            require_model_agreement=REQUIRE_MODEL_AGREEMENT,
        )

    async def _check_model_agreement(
        self, *, sport: str, game: dict, team: str, market: str, direction: str,
    ) -> tuple[bool, str]:
        """Return (ok, label) indicating whether any registered model agrees.

        Delegates to tools.lines.edge_report.check_model_agreement using the
        latest cached edge report for this sport.
        """
        report = self._latest_edge_reports.get(sport) or {}
        game_id = str(game.get("id", ""))
        return check_model_agreement(report=report, game_id=game_id, team=team, market=market)

    async def get_recent_movements(self, sport: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Get recent line movements from the database."""
        if sport:
            cursor = await self._db.execute(
                "SELECT * FROM line_movements WHERE sport = ? ORDER BY detected_at DESC LIMIT ?",
                (sport, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM line_movements ORDER BY detected_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def get_ev_opportunities(self, status: str = "open", limit: int = 20) -> list[dict]:
        """Get current +EV opportunities."""
        cursor = await self._db.execute(
            "SELECT * FROM ev_opportunities WHERE status = ? ORDER BY detected_at DESC LIMIT ?",
            (status, limit),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def get_snapshot_history(self, sport: str, limit: int = 10) -> list[dict]:
        """Get snapshot history for a sport (metadata only, no full JSON)."""
        cursor = await self._db.execute(
            "SELECT id, sport, timestamp, game_count, credits_remaining "
            "FROM odds_snapshots WHERE sport = ? ORDER BY timestamp DESC LIMIT ?",
            (sport, limit),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def get_status(self) -> dict:
        """Return monitor status with DB-backed counts."""
        db_snapshots = 0
        db_movements = 0
        db_closing_lines = 0
        latest_snapshot_at = None
        try:
            if self._db:
                row = await (await self._db.execute(
                    "SELECT COUNT(*), MAX(timestamp) FROM odds_snapshots"
                )).fetchone()
                if row:
                    db_snapshots = row[0] or 0
                    latest_snapshot_at = row[1]
                row2 = await (await self._db.execute(
                    "SELECT COUNT(*) FROM line_movements"
                )).fetchone()
                db_movements = row2[0] if row2 else 0
                try:
                    row3 = await (await self._db.execute(
                        "SELECT COUNT(*) FROM closing_lines"
                    )).fetchone()
                    db_closing_lines = row3[0] if row3 else 0
                except Exception:
                    pass  # Table may not exist yet
        except Exception:
            pass

        return {
            "running": self._running,
            "monitored_sports": MONITORED_SPORTS,
            "snapshot_interval_seconds": SNAPSHOT_INTERVAL,
            "cached_snapshots": list(self._snapshots.keys()),
            "db_snapshots_total": db_snapshots,
            "db_movements_total": db_movements,
            "db_closing_lines": db_closing_lines,
            "latest_snapshot_at": latest_snapshot_at,
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
