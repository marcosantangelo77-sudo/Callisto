"""
Line movement monitor — autonomous odds snapshot engine.

Takes periodic snapshots of live odds across sports and detects
significant line movements. This is where edges are found:
- Large movement after observable event = potential +EV
- Movement direction vs event impact = gauge market efficiency
- Cross-bookmaker divergence = arbitrage or soft book edge

Runs as a background task within the Callisto API lifecycle.
Stores snapshots in SQLite for historical analysis.
"""

import asyncio
import json
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
from tools.action_network_scraper import scrape_action_network
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

# Movement thresholds — what counts as "significant"
# Tightened from 10/1.0: at -110, +5 is ~2% implied prob change which
# is a meaningful sharp move. 0.5 captures key-number crosses (3, 7) that
# can flip cover probability by 5-10%.
PRICE_MOVEMENT_THRESHOLD = 5     # American odds points (~2% implied prob)
POINT_MOVEMENT_THRESHOLD = 0.5   # Spread/total half-points (key number sensitivity)

# Minimum edge for alert
MIN_EDGE_ALERT = 0.03  # 3% edge minimum to flag as interesting

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

# Map odds-api.io WS sport slugs back to the-odds-api.com sport keys used in
# odds_snapshots rows. A single WS sport fans out to multiple leagues.
WS_SPORT_TO_MONITORED: dict[str, list[str]] = {
    "basketball": ["basketball_nba", "basketball_ncaab", "basketball_ncaaw"],
    "american-football": ["americanfootball_nfl", "americanfootball_ncaaf"],
    "baseball": ["baseball_mlb"],
    "ice-hockey": ["icehockey_nhl"],
    "soccer": ["soccer_mls", "soccer_epl"],
}


def _ws_sport_to_monitored(ws_sport: str, ws_league: str = "") -> Optional[str]:
    """Map odds-api.io WS (sport, league) to the-odds-api.com sport key.

    WS messages carry e.g. sport='basketball', league='NBA'. We convert
    that back to 'basketball_nba' so every downstream consumer (edge
    scanner, movement detector, odds_snapshots rows) sees the same
    canonical sport key regardless of whether the event arrived by WS,
    incremental poll, or 15-min snapshot.
    """
    s = (ws_sport or "").lower().strip()
    lg = (ws_league or "").lower().strip().replace(" ", "_")
    # Preferred: combine sport + league so basketball_ncaab and
    # basketball_nba don't collide in edge-scan output.
    if s == "basketball":
        if "ncaa" in lg and "w" in lg:
            return "basketball_ncaaw"
        if "ncaa" in lg:
            return "basketball_ncaab"
        return "basketball_nba"
    if s in ("american-football", "american_football", "football"):
        if "ncaa" in lg:
            return "americanfootball_ncaaf"
        return "americanfootball_nfl"
    if s == "baseball":
        return "baseball_mlb"
    if s in ("ice-hockey", "ice_hockey", "hockey"):
        return "icehockey_nhl"
    if s == "soccer":
        return "soccer_mls"
    # Last resort — first matching entry in WS_SPORT_TO_MONITORED.
    first = WS_SPORT_TO_MONITORED.get(s, [])
    if first:
        return first[0]
    return None


def _ws_update_to_snapshot(data: dict) -> Optional[tuple[str, dict]]:
    """Convert a single odds-api.io WS/incremental message into a snapshot.

    Returns (sport_key, snapshot_dict) or None if the message lacks enough
    structure to route. The snapshot dict is shaped like get_odds() output
    so _process_snapshot can consume it unchanged — one game, one
    bookmaker, and the subset of markets that actually changed.
    """
    if not isinstance(data, dict):
        return None
    event_id = data.get("id") or data.get("event_id")
    if not event_id:
        return None
    ws_sport = data.get("sport", "") or data.get("sport_key", "")
    ws_league = data.get("league", "")
    sport_key = _ws_sport_to_monitored(str(ws_sport), str(ws_league))
    if not sport_key:
        return None

    bookie_name = data.get("bookie") or data.get("bookmaker") or ""
    if not bookie_name:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    # Build an odds-api-shaped bookmaker entry. odds-api.io WS markets look
    # like {"name": "ML"|"Spread"|"Totals", "outcomes": [{"name", "price",
    # "point"?}]} — map onto the-odds-api.com's "key" vocabulary.
    _WS_MARKET_MAP = {
        "ml": "h2h", "moneyline": "h2h",
        "spread": "spreads", "spreads": "spreads", "runline": "spreads",
        "totals": "totals", "total": "totals", "ou": "totals",
    }
    bm_markets = []
    for m in data.get("markets", []) or []:
        raw = str(m.get("name", "")).lower()
        key = _WS_MARKET_MAP.get(raw, raw)
        outcomes = []
        for oc in m.get("outcomes", []) or []:
            outcomes.append({
                "name": oc.get("name", ""),
                "price": oc.get("price", 0),
                "point": oc.get("point"),
                "fetched_at": now_iso,
            })
        if outcomes:
            bm_markets.append({"key": key, "outcomes": outcomes})
    if not bm_markets:
        return None

    snapshot = {
        "sport": sport_key,
        "game_count": 1,
        "source": "odds_api_io",
        "fetched_at": now_iso,
        "games": [{
            "id": str(event_id),
            "sport_key": sport_key,
            "home_team": data.get("home", "") or data.get("home_team", ""),
            "away_team": data.get("away", "") or data.get("away_team", ""),
            "commence_time": data.get("commence") or data.get("commence_time"),
            "bookmakers": [{
                "key": canonicalize_book_top(bookie_name),
                "title": bookie_name,
                "last_update": now_iso,
                "fetched_at": now_iso,
                "markets": bm_markets,
            }],
        }],
    }
    return sport_key, snapshot


def canonicalize_book_top(name: str) -> str:
    """Thin wrapper — imports lazily to avoid circular imports at module load."""
    try:
        from tools.book_keys import canonicalize_book as _cb
        return _cb(name)
    except Exception:
        return (name or "").lower().replace(" ", "_")


def _merge_delta_into_snapshot(base: dict, delta: dict, now_iso: str) -> dict:
    """Splice a single-book WS/incremental delta onto the full snapshot.

    Keeps every game + book from `base`, then for each game in `delta`
    replaces OR appends the matching bookmaker entry. The returned dict is
    a shallow copy — callers may mutate per-game entries in place.

    Crucially, this preserves multi-book consensus: when DK pushes a WS
    update, the returned snapshot still has every other book's quote from
    the last 15-min snapshot (aged but weighted-down via fetched_at decay
    in edge_scanner), and DK's entry is replaced with the fresh quote.
    """
    import copy
    merged = {
        "sport": base.get("sport", delta.get("sport", "")),
        "game_count": base.get("game_count", 0),
        "source": delta.get("source", base.get("source", "odds_api")),
        "fetched_at": now_iso,
        "ingest_source": delta.get("ingest_source", "ws"),
        "games": [copy.deepcopy(g) for g in base.get("games", [])],
    }
    # Index base games by id for O(1) splice.
    by_id: dict[str, dict] = {}
    for g in merged["games"]:
        gid = str(g.get("id", ""))
        if gid:
            by_id[gid] = g

    for dgame in delta.get("games", []) or []:
        gid = str(dgame.get("id", ""))
        if not gid or gid not in by_id:
            # New event that hasn't been seen in base — append wholesale.
            merged["games"].append(copy.deepcopy(dgame))
            continue
        target = by_id[gid]
        target.setdefault("bookmakers", [])
        existing = target["bookmakers"]
        for dbm in dgame.get("bookmakers", []) or []:
            dkey = (dbm.get("key") or "").lower()
            dtitle = (dbm.get("title") or "").lower()
            replaced = False
            for i, bm in enumerate(existing):
                bmkey = (bm.get("key") or "").lower()
                bmtitle = (bm.get("title") or "").lower()
                if dkey and bmkey == dkey:
                    existing[i] = copy.deepcopy(dbm)
                    replaced = True
                    break
                if dtitle and bmtitle == dtitle:
                    existing[i] = copy.deepcopy(dbm)
                    replaced = True
                    break
            if not replaced:
                existing.append(copy.deepcopy(dbm))
    merged["game_count"] = len(merged["games"])
    return merged


def _stamp_snapshot_fetched_at(snapshot: dict, now_iso: str) -> None:
    """Stamp `fetched_at` on every bookmaker entry in a snapshot.

    Prefers an existing `fetched_at` (so WS-delivered deltas retain their
    true ingest time even when later merged into a 15-min snapshot frame)
    and falls back to `last_update` → `now_iso` otherwise. The outermost
    snapshot dict also receives `fetched_at` so per-provider tooling
    (scraper fallback, incremental poll) can pass the stamp through without
    digging into every bookmaker.
    """
    snapshot.setdefault("fetched_at", now_iso)
    for game in snapshot.get("games", []) or []:
        for bm in game.get("bookmakers", []) or []:
            # Bookmaker-level fetched_at — don't overwrite WS stamps
            if not bm.get("fetched_at"):
                bm["fetched_at"] = bm.get("last_update") or now_iso
            # Outcome-level for granular freshness (WS messages deliver a
            # single outcome change; stamp that outcome)
            for mkt in bm.get("markets", []) or []:
                for oc in mkt.get("outcomes", []) or []:
                    if not oc.get("fetched_at"):
                        oc["fetched_at"] = bm.get("fetched_at", now_iso)


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
        self._kl_cache: dict[str, dict] = {}  # "sport:event_id:market" -> KL metrics (capped in _put_kl)
        self._KL_CACHE_MAX = 2000  # was 10000 — 5x reduction to limit arena fragmentation
        # Self-healing: track consecutive all-source failures per sport.
        # Alert via Telegram only after 3+ consecutive failures.
        self._consecutive_failures: dict[str, int] = {}  # sport -> count
        self._FAILURE_ALERT_THRESHOLD = 3

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
            mapped = _ws_update_to_snapshot(data)
            if not mapped:
                return
            sport_key, snap = mapped
            snap["ingest_source"] = "ws"
            # Run through the normal pipeline — this writes fetched_at,
            # triggers edge rescoring for the affected market, and invokes
            # _evaluate_movement for changed prices.
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
                        mapped = _ws_update_to_snapshot(upd)
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
        Priority cascade:
          1. Odds-API.io Pro (PRIMARY — 15 books, 30K req/hr)
          2. DraftKings scraper (supplementary — DK-specific lines)
          3. Action Network scraper (supplementary — up to 9 books)
          4. FanDuel scraper (supplementary)
          5. BetMGM scraper (supplementary)
          6. OddsPapi (last resort — 250/month)
        Merges all successful sources for maximum multi-book coverage.
        """
        scraped = {}  # source_name -> data

        # 1. Odds-API.io Pro — PRIMARY source (15 books, 30K req/hr)
        # This is now the best multi-book source by far.
        try:
            usage = odds_api_io_usage()
            if usage.get("requests_remaining_this_hour", usage.get("requests_remaining", 0)) > 0 and usage.get("api_key_set"):
                io_data = await odds_api_io_get_odds(sport)
                if not io_data.get("error") and io_data.get("game_count", 0) > 0:
                    scraped["odds_api_io"] = io_data
                    logger.info(f"Odds-API.io Pro {sport}: {io_data['game_count']} games ({len(io_data['games'][0]['bookmakers']) if io_data['games'] else 0} books/game)")
        except Exception as e:
            logger.warning(f"Odds-API.io Pro failed for {sport}: {e}")

        # 2. DraftKings — free, supplementary for DK-specific alt lines
        try:
            dk_data = await scrape_dk_odds(sport)
            if not dk_data.get("error") and dk_data.get("game_count", 0) > 0:
                scraped["dk"] = dk_data
        except Exception as e:
            logger.warning(f"DK scraper failed for {sport}: {e}")

        # 3. Action Network — free, up to 9 books per game
        try:
            an_data = await scrape_action_network(sport)
            if not an_data.get("error") and an_data.get("game_count", 0) > 0:
                scraped["action_network"] = an_data
        except Exception as e:
            logger.warning(f"Action Network scraper failed for {sport}: {e}")

        # 4. FanDuel — free and unlimited
        try:
            fd_data = await scrape_fd_odds(sport)
            if not fd_data.get("error") and fd_data.get("game_count", 0) > 0:
                scraped["fd"] = fd_data
        except Exception as e:
            logger.warning(f"FanDuel scraper failed for {sport}: {e}")

        # 4b. Fanatics — secondary book per project_sportsbooks. Free public
        # endpoint; cookie-optional (CALLISTO_FANATICS_SESSION_COOKIE upgrades
        # to authed reads). Skip sports Fanatics doesn't carry (golf, MLS).
        try:
            from tools.fanatics_scraper import FANATICS_LEAGUE_KEYS
            if sport in FANATICS_LEAGUE_KEYS:
                fan_data = await fetch_fanatics_odds(sport)
                if not fan_data.get("error") and fan_data.get("game_count", 0) > 0:
                    scraped["fanatics"] = fan_data
        except Exception as e:
            logger.warning(f"Fanatics scraper failed for {sport}: {e}")

        # 5. BetMGM — DISABLED: redundant with odds-api.io Pro (includes BetMGM).
        # Scraped endpoint returns 400/403 consistently, generating log noise.
        # Re-enable only if odds-api.io loses BetMGM coverage.

        # 6. OddsPapi — REMOVED 2026-04-18. odds-api.io Pro covers the same
        # books at higher quota; the oddspapi free tier was throwing 429 on
        # every call. Do not reintroduce without an explicit decision.

        # Merge all successful sources
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

        sources = list(scraped.values())
        new_snapshot = sources[0]
        for extra in sources[1:]:
            new_snapshot = self._merge_free_snapshots(new_snapshot, extra)

        new_snapshot["source"] = f"free_cascade_{'_'.join(scraped.keys())}"
        logger.info(
            f"Fallback snapshot {sport}: merged {list(scraped.keys())} = "
            f"{new_snapshot.get('game_count', 0)} games"
        )

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
        """Merge fresh DK scraper data into an Odds API snapshot.

        For each game in the snapshot, if DK scraper has data for the same
        matchup, update (or add) the DraftKings bookmaker entry with the
        fresher scraped lines. This is free and gives us the target book's
        actual current lines rather than potentially cached API data.
        """
        try:
            dk_data = await scrape_dk_odds(sport)
            if dk_data.get("error") or not dk_data.get("games"):
                return snapshot

            # Build lookup: normalize team names for matching
            dk_by_matchup = {}
            for dk_game in dk_data["games"]:
                key = self._matchup_key(dk_game.get("home_team", ""), dk_game.get("away_team", ""))
                if key:
                    dk_by_matchup[key] = dk_game

            enriched = 0
            for game in snapshot.get("games", []):
                key = self._matchup_key(game.get("home_team", ""), game.get("away_team", ""))
                if not key or key not in dk_by_matchup:
                    continue

                dk_game = dk_by_matchup[key]
                dk_bookmaker = None
                for bm in dk_game.get("bookmakers", []):
                    if bm.get("key") == "draftkings":
                        dk_bookmaker = bm
                        break

                if not dk_bookmaker:
                    continue

                # Find and replace existing DK entry, or append
                replaced = False
                for i, bm in enumerate(game.get("bookmakers", [])):
                    if bm.get("key", "").lower() in ("draftkings", "draft_kings"):
                        game["bookmakers"][i] = dk_bookmaker
                        replaced = True
                        break

                if not replaced:
                    game.setdefault("bookmakers", []).append(dk_bookmaker)

                enriched += 1

            if enriched > 0:
                logger.info(f"DK enrichment {sport}: updated {enriched}/{len(snapshot.get('games', []))} games")

        except Exception as e:
            logger.warning(f"DK enrichment failed for {sport}: {e}", exc_info=True)

        return snapshot

    async def _enrich_with_fd(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh FanDuel scraper data into an odds snapshot.

        Same pattern as _enrich_with_dk: for each game in the snapshot,
        if the FanDuel scraper has data for the same matchup, update (or add)
        the FanDuel bookmaker entry with the fresher scraped lines.
        """
        try:
            fd_data = await scrape_fd_odds(sport)
            if fd_data.get("error") or not fd_data.get("games"):
                return snapshot

            # Build lookup: normalize team names for matching
            fd_by_matchup = {}
            for fd_game in fd_data["games"]:
                key = self._matchup_key(fd_game.get("home_team", ""), fd_game.get("away_team", ""))
                if key:
                    fd_by_matchup[key] = fd_game

            enriched = 0
            for game in snapshot.get("games", []):
                key = self._matchup_key(game.get("home_team", ""), game.get("away_team", ""))
                if not key or key not in fd_by_matchup:
                    continue

                fd_game = fd_by_matchup[key]
                fd_bookmaker = None
                for bm in fd_game.get("bookmakers", []):
                    if bm.get("key") == "fanduel":
                        fd_bookmaker = bm
                        break

                if not fd_bookmaker:
                    continue

                # Find and replace existing FanDuel entry, or append
                replaced = False
                for i, bm in enumerate(game.get("bookmakers", [])):
                    if bm.get("key", "").lower() in ("fanduel", "fan_duel"):
                        game["bookmakers"][i] = fd_bookmaker
                        replaced = True
                        break

                if not replaced:
                    game.setdefault("bookmakers", []).append(fd_bookmaker)

                enriched += 1

            if enriched > 0:
                logger.info(f"FD enrichment {sport}: updated {enriched}/{len(snapshot.get('games', []))} games")

        except Exception as e:
            logger.warning(f"FD enrichment failed for {sport}: {e}", exc_info=True)

        return snapshot

    async def _enrich_with_mgm(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh BetMGM scraper data into an odds snapshot.

        Same pattern as _enrich_with_dk/_enrich_with_fd.
        """
        try:
            mgm_data = await scrape_betmgm_odds(sport)
            if mgm_data.get("error") or not mgm_data.get("games"):
                return snapshot

            mgm_by_matchup = {}
            for mgm_game in mgm_data["games"]:
                key = self._matchup_key(mgm_game.get("home_team", ""), mgm_game.get("away_team", ""))
                if key:
                    mgm_by_matchup[key] = mgm_game

            enriched = 0
            for game in snapshot.get("games", []):
                key = self._matchup_key(game.get("home_team", ""), game.get("away_team", ""))
                if not key or key not in mgm_by_matchup:
                    continue

                mgm_game = mgm_by_matchup[key]
                mgm_bookmaker = None
                for bm in mgm_game.get("bookmakers", []):
                    if bm.get("key") == "betmgm":
                        mgm_bookmaker = bm
                        break

                if not mgm_bookmaker:
                    continue

                replaced = False
                for i, bm in enumerate(game.get("bookmakers", [])):
                    if bm.get("key", "").lower() in ("betmgm", "bet_mgm"):
                        game["bookmakers"][i] = mgm_bookmaker
                        replaced = True
                        break

                if not replaced:
                    game.setdefault("bookmakers", []).append(mgm_bookmaker)

                enriched += 1

            if enriched > 0:
                logger.info(f"BetMGM enrichment {sport}: updated {enriched}/{len(snapshot.get('games', []))} games")

        except Exception as e:
            logger.warning(f"BetMGM enrichment failed for {sport}: {e}", exc_info=True)

        return snapshot

    async def _enrich_with_fanatics(self, sport: str, snapshot: dict) -> dict:
        """Merge fresh Fanatics scraper data into an odds snapshot.

        Same pattern as _enrich_with_dk/_enrich_with_fd. Fanatics is the
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

        try:
            fan_data = await fetch_fanatics_odds(sport)
            if fan_data.get("error") or not fan_data.get("games"):
                return snapshot

            fan_by_matchup: dict[str, dict] = {}
            for fan_game in fan_data["games"]:
                key = self._matchup_key(fan_game.get("home_team", ""), fan_game.get("away_team", ""))
                if key:
                    fan_by_matchup[key] = fan_game

            enriched = 0
            for game in snapshot.get("games", []):
                key = self._matchup_key(game.get("home_team", ""), game.get("away_team", ""))
                if not key or key not in fan_by_matchup:
                    continue

                fan_game = fan_by_matchup[key]
                fan_bookmaker = None
                for bm in fan_game.get("bookmakers", []):
                    if bm.get("key") == "fanatics":
                        fan_bookmaker = bm
                        break

                if not fan_bookmaker:
                    continue

                # Replace any existing Fanatics entry (including spelling
                # variants) with the scraped one, or append if absent.
                replaced = False
                for i, bm in enumerate(game.get("bookmakers", [])):
                    if bm.get("key", "").lower() in ("fanatics", "fanatics_sportsbook"):
                        game["bookmakers"][i] = fan_bookmaker
                        replaced = True
                        break

                if not replaced:
                    game.setdefault("bookmakers", []).append(fan_bookmaker)

                enriched += 1

            if enriched > 0:
                logger.info(f"Fanatics enrichment {sport}: updated {enriched}/{len(snapshot.get('games', []))} games")

        except Exception as e:
            logger.warning(f"Fanatics enrichment failed for {sport}: {e}", exc_info=True)

        return snapshot

    def _merge_free_snapshots(self, base_data: dict, extra_data: dict) -> dict:
        """Merge two odds snapshots into one multi-book snapshot.

        Uses base_data as the foundation, then adds bookmaker entries from
        extra_data to matching games. Extra-only games are appended.
        Works with any pair of sources (DK+FD, DK+MGM, etc.).
        """
        merged = {
            "sport": base_data.get("sport", extra_data.get("sport", "")),
            "games": [dict(g) for g in base_data.get("games", [])],
            "source": "merged",
            "credits": {"remaining": None, "used": None, "api_key_set": True},
        }

        # Build matchup lookup from base games
        base_by_matchup = {}
        for i, game in enumerate(merged["games"]):
            key = self._matchup_key(game.get("home_team", ""), game.get("away_team", ""))
            if key:
                base_by_matchup[key] = i

        extra_only_games = []
        for extra_game in extra_data.get("games", []):
            key = self._matchup_key(extra_game.get("home_team", ""), extra_game.get("away_team", ""))
            if key and key in base_by_matchup:
                idx = base_by_matchup[key]
                # Add bookmakers from extra source, skipping duplicates.
                # A duplicate = same bookmaker key already present in base.
                existing_keys = {
                    bm.get("key", "").lower()
                    for bm in merged["games"][idx].get("bookmakers", [])
                }
                for bm in extra_game.get("bookmakers", []):
                    bm_key = bm.get("key", "").lower()
                    if bm_key and bm_key in existing_keys:
                        continue  # Skip — this book already has an entry
                    merged["games"][idx].setdefault("bookmakers", []).append(bm)
                    if bm_key:
                        existing_keys.add(bm_key)
            else:
                extra_only_games.append(extra_game)

        merged["games"].extend(extra_only_games)
        merged["game_count"] = len(merged["games"])

        # Enforce sport_key on all games to prevent cross-sport contamination
        sport_key = merged["sport"]
        if sport_key:
            for g in merged["games"]:
                if not g.get("sport_key"):
                    g["sport_key"] = sport_key

        return merged

    @staticmethod
    def _matchup_key(home: str, away: str) -> str:
        """Normalize team names into a matchup key for cross-source matching."""
        if not home or not away:
            return ""
        # Lowercase, strip common suffixes, sort for consistency
        h = home.lower().strip()
        a = away.lower().strip()
        return f"{min(a, h)}|{max(a, h)}"

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
        _stamp_snapshot_fetched_at(new_snapshot, now)

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
                new_snapshot = _merge_delta_into_snapshot(prior, new_snapshot, now)

        # Store snapshot — use retry on both execute and commit since autonomous
        # loop does NOT acquire the write lock, so SQLite-level contention can occur.
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "INSERT INTO odds_snapshots "
            "(sport, timestamp, snapshot_json, game_count, credits_remaining, "
            "fetched_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sport, now, json.dumps(new_snapshot), game_count, credits_remaining,
             now, ingest_source),
            max_retries=10,
            operation="snapshot_insert",
        )
        await commit_with_retry(self._db, max_retries=10, operation="snapshot_store")

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
        # Every live snapshot becomes backtest-grade data. Even single-book
        # snapshots are worth archiving — they provide game context data
        # and can be cross-referenced with other snapshots from the same date.
        # This is the primary mechanism for building historical depth.
        book_count = 0
        for g in new_snapshot.get("games", []):
            book_count = max(book_count, len(g.get("bookmakers", [])))
            if not g.get("sport_key"):
                g["sport_key"] = sport
        if game_count > 0:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                from tools.db_utils import execute_with_retry, commit_with_retry
                await execute_with_retry(
                    self._db,
                    "INSERT OR REPLACE INTO historical_odds_cache "
                    "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, fetched_at) "
                    "VALUES (?, ?, NULL, 'h2h,spreads,totals', ?, 0, ?)",
                    (sport, today, json.dumps(new_snapshot), now),
                    max_retries=10,
                    operation=f"historical_odds_cache insert {sport}",
                )
                await commit_with_retry(
                    self._db,
                    max_retries=10,
                    operation=f"historical_odds_cache commit {sport}",
                )
                logger.info(f"Cached multi-book snapshot for backtest: {sport} {today} ({book_count} books)")
            except Exception as e:
                logger.warning(f"Failed to cache snapshot for backtest: {e}")

        # Run edge scanner on every snapshot
        edge_report = full_edge_scan(new_snapshot)
        self._latest_edge_reports[sport] = edge_report
        total_edges = edge_report.get("total_edges", 0)
        if total_edges > 0:
            logger.info(f"Edge scan {sport}: {total_edges} edges found")

        # Store market microstructure metrics from edge scan
        try:
            from tools.db_utils import execute_with_retry, commit_with_retry
            stored = 0
            for market_key in ["cross_book_h2h", "cross_book_spreads", "cross_book_totals"]:
                edges = edge_report.get(market_key, [])
                for edge in edges:
                    hhi_val = edge.get("hhi")
                    entropy_val = edge.get("entropy")
                    if hhi_val is not None or entropy_val is not None:
                        await execute_with_retry(
                            self._db,
                            "INSERT OR REPLACE INTO market_microstructure "
                            "(sport, game_id, market_type, timestamp, hhi_overall, "
                            "entropy_overall, num_books) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                sport,
                                edge.get("game_id", ""),
                                market_key.replace("cross_book_", ""),
                                now,
                                hhi_val,
                                entropy_val,
                                edge.get("num_bookmakers", 0),
                            ),
                            max_retries=5,
                            operation="microstructure_insert",
                        )
                        stored += 1
            if stored > 0:
                await commit_with_retry(self._db, max_retries=5, operation="microstructure_store")
                logger.info(f"Stored {stored} microstructure metrics for {sport}")
        except Exception as e:
            logger.warning(f"Market microstructure store failed: {e}")

        # NOTE: Raw edges are NOT sent to Telegram here.
        # The autonomous loop analyzes candidates via full AGP sessions
        # and only alerts after the Architect confirms the edge is real.

        # Compare with previous snapshot
        old_snapshot = self._snapshots.get(sport)
        if old_snapshot:
            movements = detect_line_movement(old_snapshot, new_snapshot)
            significant = [
                m for m in movements
                if abs(m["price_movement"]) >= PRICE_MOVEMENT_THRESHOLD
                or abs(m["point_movement"]) >= POINT_MOVEMENT_THRESHOLD
            ]

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

        For each game starting within the next snapshot interval + buffer,
        extract the consensus/sharp closing line and record it. This bridges
        the line_monitor → CLV tracker gap that was previously dead code.
        """
        try:
            # Import CLV tracker from the global API state
            from api import clv_tracker as _clv
            if _clv is None:
                return

            now = datetime.now(timezone.utc)
            closing_window_seconds = max(SNAPSHOT_INTERVAL + 300, 3600)  # at least 1hr window

            games = snapshot.get("games", [])
            closing_count = 0

            for game in games:
                commence_time_str = game.get("commence_time", "")
                if not commence_time_str:
                    continue

                try:
                    commence = datetime.fromisoformat(
                        commence_time_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    continue

                seconds_until_start = (commence - now).total_seconds()

                # Game starts within closing window and hasn't already started
                if 0 < seconds_until_start <= closing_window_seconds:
                    event_id = game.get("id", "")
                    home = game.get("home_team", "")
                    away = game.get("away_team", "")

                    # Extract odds from all bookmakers for each market
                    for bm in game.get("bookmakers", []):
                        book_name = bm.get("title", bm.get("key", ""))
                        for market_data in bm.get("markets", []):
                            market_key = market_data.get("key", "")
                            for outcome in market_data.get("outcomes", []):
                                team = outcome.get("name", "")
                                price = outcome.get("price")
                                point = outcome.get("point")

                                if price is None:
                                    continue

                                # Normalize source identifier to the key-style used by
                                # _RELIABLE_CLOSE_SOURCES in clv_tracker (lowercase, no
                                # spaces). Odds-api-io returns titles like "Pinnacle",
                                # "Betfair Exchange", "BetOnline.ag"; stored mixed-case,
                                # every reliable book later tests as unreliable.
                                src_key = (book_name or "").lower().replace(" ", "_")
                                try:
                                    await _clv.record_closing_line(
                                        event_id=event_id,
                                        market=market_key,
                                        team=team,
                                        closing_odds=int(price),
                                        closing_point=float(point) if point is not None else None,
                                        source=src_key,
                                        sport=sport,
                                        line=float(point) if point is not None else None,
                                        commence_time=commence_time_str,
                                    )
                                    closing_count += 1
                                except Exception as e:
                                    logger.debug(f"CLV closing line record failed: {e}")

            if closing_count > 0:
                logger.info(
                    f"CLV: captured {closing_count} closing lines for {sport} "
                    f"(games starting within {closing_window_seconds}s)"
                )
        except ImportError:
            pass  # CLV tracker not available
        except Exception as e:
            logger.warning(f"CLV closing line capture failed for {sport}: {e}")

    async def _record_movement(self, sport: str, movement: dict) -> None:
        """Record a line movement to the database."""
        from tools.db_utils import execute_with_retry, commit_with_retry
        now = datetime.now(timezone.utc).isoformat()
        await execute_with_retry(
            self._db,
            "INSERT INTO line_movements "
            "(sport, detected_at, team, market, bookmaker, old_price, new_price, "
            "price_movement, old_point, new_point, point_movement, direction) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sport, now, movement["team"], movement["market"],
                movement["bookmaker"], movement["old_price"], movement["new_price"],
                movement["price_movement"], movement.get("old_point"),
                movement.get("new_point"), movement.get("point_movement", 0),
                movement["direction"],
            ),
            max_retries=5,
            operation=f"line_movement insert {sport}",
        )
        await commit_with_retry(self._db, max_retries=5, operation=f"line_movement commit {sport}")

        self._alerts.append({
            "sport": sport,
            "detected_at": now,
            **movement,
        })
        # Keep only last 100 alerts in memory
        if len(self._alerts) > 100:
            self._alerts = self._alerts[-100:]

    async def _compute_and_store_kl(self, sport: str, old_snapshot: dict, new_snapshot: dict) -> None:
        """Compute KL divergence between two consecutive snapshots per game.

        For each game present in both snapshots, extract implied probability
        distributions from each bookmaker and compute KL(new || old) and
        Jensen-Shannon divergence. High KL = significant price discovery
        between snapshots. Stores results in kl_metrics table.

        Also caches latest KL per (sport, event_id) in memory for fast
        lookups by edge_confidence scoring.
        """
        try:
            old_games = {g.get("id"): g for g in old_snapshot.get("games", []) if g.get("id")}
            new_games = {g.get("id"): g for g in new_snapshot.get("games", []) if g.get("id")}

            common_ids = set(old_games.keys()) & set(new_games.keys())
            if not common_ids:
                return

            metrics_batch = []
            for event_id in common_ids:
                old_game = old_games[event_id]
                new_game = new_games[event_id]

                for market_type in ("h2h", "spreads", "totals"):
                    old_probs = self._extract_implied_probs(old_game, market_type)
                    new_probs = self._extract_implied_probs(new_game, market_type)

                    if len(old_probs) < 2 or len(new_probs) < 2:
                        continue

                    # Normalize to same length (use min of both)
                    n = min(len(old_probs), len(new_probs))
                    old_sorted = sorted(old_probs)[:n]
                    new_sorted = sorted(new_probs)[:n]

                    kl = kl_divergence(new_sorted, old_sorted)
                    js = jensen_shannon(new_sorted, old_sorted)

                    # Only store if there's meaningful divergence
                    if kl < 1e-8 and js < 1e-8:
                        continue

                    metric = {
                        "event_id": event_id,
                        "sport": sport,
                        "market_type": market_type,
                        "kl_divergence": round(kl, 6),
                        "js_divergence": round(js, 6),
                        "n_books": n,
                        "opening_entropy": round(shannon_entropy(old_sorted), 6),
                        "closing_entropy": round(shannon_entropy(new_sorted), 6),
                    }
                    metrics_batch.append(metric)

                    # Cache in memory for edge_confidence lookups (capped to prevent leak)
                    cache_key = f"{sport}:{event_id}:{market_type}"
                    if len(self._kl_cache) >= self._KL_CACHE_MAX:
                        # Evict ~20% oldest entries
                        evict_n = self._KL_CACHE_MAX // 5
                        for _ in range(evict_n):
                            try:
                                self._kl_cache.pop(next(iter(self._kl_cache)))
                            except (StopIteration, KeyError):
                                break
                    self._kl_cache[cache_key] = metric

            if metrics_batch:
                stored = await store_kl_metrics(self.db_path, metrics_batch)
                logger.info(f"KL metrics {sport}: {stored} game-markets computed (max KL={max(m['kl_divergence'] for m in metrics_batch):.4f})")

        except Exception as e:
            logger.warning(f"KL divergence computation failed for {sport}: {e}")

    @staticmethod
    def _extract_implied_probs(game: dict, market_type: str) -> list[float]:
        """Extract implied probabilities for the first outcome across all bookmakers."""
        probs = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != market_type:
                    continue
                outcomes = mkt.get("outcomes", [])
                if not outcomes:
                    continue
                price = outcomes[0].get("price", 0)
                if price == 0:
                    continue
                if price > 0:
                    prob = 100.0 / (price + 100.0)
                else:
                    prob = abs(price) / (abs(price) + 100.0)
                probs.append(prob)
        return probs

    def get_kl_for_game(self, sport: str, event_id: str, market_type: str = "h2h") -> Optional[dict]:
        """Look up cached KL metrics for a game. Used by edge_confidence scoring."""
        cache_key = f"{sport}:{event_id}:{market_type}"
        return self._kl_cache.get(cache_key)

    async def _evaluate_movement(self, sport: str, movement: dict, snapshot: dict) -> None:
        """Evaluate whether a line movement creates a +EV opportunity.

        Core overreaction logic:
        - If a line moved hard in one direction, estimate whether the market overreacted
        - Use implied probability from NEW line vs cross-bookmaker consensus
        - Flag if estimated edge > MIN_EDGE_ALERT
        """
        # Find the game in the snapshot
        target_team = movement["team"]
        market = movement["market"]
        new_price = movement["new_price"]

        # Get cross-bookmaker comparison for this game
        for game in snapshot.get("games", []):
            # Check if this game contains the team
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            if target_team.lower() not in home.lower() and target_team.lower() not in away.lower():
                continue

            best = find_best_line(game, market=market, team=target_team)
            if best.get("error"):
                continue

            all_lines = best.get("all_lines", [])
            if len(all_lines) < 2:
                continue

            # ── Sanity checks (mirrors edge_scanner.py) ──

            # H2H contamination: if lines contain both large positive AND large
            # negative prices, both sides of the market leaked into one team's
            # set (e.g. favorite -750 mixed with underdog +610). Skip.
            if market == "h2h":
                prices = [l["price"] for l in all_lines]
                has_big_pos = any(p > 150 for p in prices)
                has_big_neg = any(p < -150 for p in prices)
                if has_big_pos and has_big_neg:
                    logger.warning(
                        f"Edge eval: H2H contamination for {target_team} — "
                        f"prices span {min(prices)} to {max(prices)}, skipping"
                    )
                    continue

            # ── Devigged consensus: power-devig each book's two-outcome
            # market, then average the target-side fair probs ──
            #
            # The naive approach (averaging raw implied probs) counts the
            # vig as edge — power devig removes it first.
            moved_book = movement["bookmaker"]
            devigged_fair_probs = []
            for bm in game.get("bookmakers", []):
                if bm.get("title", bm.get("key", "")) == moved_book:
                    continue  # exclude the book that moved
                for mkt in bm.get("markets", []):
                    if mkt["key"] != market:
                        continue
                    outcomes = mkt.get("outcomes", [])
                    if len(outcomes) < 2:
                        continue
                    # Find the target team's outcome and build the pair
                    target_idx = None
                    for i, oc in enumerate(outcomes):
                        if target_team.lower() in oc.get("name", "").lower():
                            target_idx = i
                            break
                    if target_idx is None:
                        continue
                    # Convert to decimal odds for devig
                    try:
                        decimal_odds = [
                            american_to_decimal(oc["price"]) for oc in outcomes
                        ]
                        if any(d <= 1.0 for d in decimal_odds):
                            continue
                        fair_probs, _k = power_devig(decimal_odds)
                        devigged_fair_probs.append(fair_probs[target_idx])
                    except (ValueError, ZeroDivisionError):
                        continue

            if len(devigged_fair_probs) < 2:
                continue  # need at least 2 books for reliable consensus

            # Implied range sanity on devigged probs.
            # Tightened to 12% (was 25%) — 12% range across multi-book devig
            # already indicates contamination. Dedup warning per (team,market)
            # to prevent log spam (was firing 1300+/hr on Lakers/Suns h2h).
            fair_range = max(devigged_fair_probs) - min(devigged_fair_probs)
            if fair_range > 0.12:
                _warn_key = f"{target_team}|{market}"
                if not hasattr(self, "_devig_warn_dedup"):
                    self._devig_warn_dedup = {}
                _last = self._devig_warn_dedup.get(_warn_key, 0)
                _now = time.monotonic()
                if _now - _last > 600:  # warn at most once per 10 min per team+market
                    logger.warning(
                        f"Edge eval: implausible devigged range {fair_range:.1%} "
                        f"for {target_team} {market}, skipping (will dedup for 10min)"
                    )
                    self._devig_warn_dedup[_warn_key] = _now
                continue

            consensus_prob = sum(devigged_fair_probs) / len(devigged_fair_probs)

            # The moved line's implied probability (raw — this is what the book offers)
            moved_implied = calculate_implied_probability(new_price)

            # Edge = devigged fair prob - book's implied prob
            edge = consensus_prob - moved_implied

            # Edge cap: real market edges top out ~15%. Anything above 20%
            # is almost certainly a data/calculation bug.
            if edge > 0.20:
                logger.warning(
                    f"Edge eval: implausible edge {edge:.1%} for {target_team} "
                    f"{market} @ {movement['bookmaker']}, skipping"
                )
                continue

            if abs(edge) >= MIN_EDGE_ALERT:
                ev_result = calculate_ev(
                    probability=consensus_prob,
                    american_odds=new_price,
                )

                if ev_result["is_positive_ev"]:
                    # MODEL AGREEMENT GATE (audit fix): before this check,
                    # every consensus-based edge became an ev_opportunities
                    # row — which meant we were steam-chasing whatever the
                    # books themselves were agreeing on. Require at least
                    # one independent model (pace, props, sim) to agree
                    # with the direction.
                    model_ok, model_label = await self._check_model_agreement(
                        sport=sport, game=game, team=target_team,
                        market=market, direction=("up" if edge > 0 else "down"),
                    )
                    steam_only = False
                    if REQUIRE_MODEL_AGREEMENT and not model_ok:
                        steam_only = True
                        logger.info(
                            f"STEAM-ONLY (model disagrees): {target_team} "
                            f"{market} @ {new_price} edge={edge:.1%} "
                            f"models={model_label}"
                        )

                    from tools.db_utils import execute_with_retry, commit_with_retry
                    now = datetime.now(timezone.utc).isoformat()
                    await execute_with_retry(
                        self._db,
                        "INSERT INTO ev_opportunities "
                        "(detected_at, sport, game_id, team, market, bookmaker, "
                        "american_odds, implied_probability, estimated_true_prob, "
                        "edge, expected_value, kelly_fraction, steam_only) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            now, sport, game.get("id", ""), target_team, market,
                            movement["bookmaker"], new_price,
                            round(moved_implied, 4), round(consensus_prob, 4),
                            round(edge, 4), ev_result["expected_value"],
                            ev_result["kelly_fraction"],
                            1 if steam_only else 0,
                        ),
                        max_retries=5,
                        operation=f"ev_opportunity insert {sport}",
                    )
                    await commit_with_retry(self._db, max_retries=5, operation=f"ev_opportunity commit {sport}")

                    logger.info(
                        f"+EV OPPORTUNITY ({'STEAM' if steam_only else 'MODEL-RATIFIED'}):"
                        f" {target_team} {market} @ {new_price} "
                        f"(edge={edge:.1%}, EV=${ev_result['expected_value']}, "
                        f"Kelly={ev_result['kelly_fraction']:.1%}, "
                        f"devig_books={len(devigged_fair_probs)})"
                    )
                    # Autonomous loop will pick this up and analyze via AGP
            break

    async def _check_model_agreement(
        self, *, sport: str, game: dict, team: str, market: str, direction: str,
    ) -> tuple[bool, str]:
        """Return (ok, label) indicating whether any registered model agrees.

        "Agrees" currently means: at least one of (pace model total edge,
        simulation-validated edge, prop-model edge) flags the same
        (game_id, team, market) with the same direction. We don't retrain
        the models here — we just re-read the edge_scan report that
        _process_snapshot already computed and cached in
        self._latest_edge_reports.

        A future version can tighten this into a quantitative directional
        agreement check (e.g. |model_prob - consensus_prob| > 2%). For now
        the gate is binary: model surfaced THIS game + market at all.
        """
        report = self._latest_edge_reports.get(sport) or {}
        game_id = str(game.get("id", ""))
        if not game_id:
            return False, "no-game-id"

        def _match(edges: list, want_market: str) -> bool:
            for e in edges or []:
                if str(e.get("game_id", "")) != game_id:
                    continue
                if e.get("market") and e["market"] != want_market:
                    continue
                # Team match (best effort — simulation/pace edges don't
                # always carry team; a game-level match still counts).
                e_team = (e.get("team") or "").lower()
                if e_team and team and e_team != team.lower():
                    continue
                return True
            return False

        # Pace model totals fire for totals markets specifically.
        if market == "totals" and _match(report.get("pace_model_totals", []), "totals"):
            return True, "pace_model"
        # Simulation-validated edges confirm spreads + totals.
        if _match(report.get("simulation_validated", []), market):
            return True, "simulation"
        # Cross-book + low-vig edges are themselves consensus-based — they
        # don't count as INDEPENDENT confirmation. Intentionally omitted.
        return False, "none"

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
