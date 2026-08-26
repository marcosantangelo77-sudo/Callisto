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
ingestion), process_snapshot (per-sport fetch, enrichment, storage,
movement/EV pipeline — slice 5), lifecycle (pause/drain handshake,
main loop scaffolding, status assembly — slice 6) and core (init
state, start/stop bring-down, WS update wrapper, edge report /
force snapshot accessors — slice 7).
The LineMonitor import path is unchanged.
"""

import logging
from typing import Optional

from dotenv import load_dotenv

from tools.lines import config as _config
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
from tools.lines.lifecycle import (
    build_status as _build_status_impl,
    monitor_loop as _monitor_loop_body,
    resume_monitor as _resume_monitor_impl,
    wait_for_drain as _wait_for_drain_impl,
)
from tools.lines.core import (
    force_snapshot as _force_snapshot_impl,
    get_edge_report as _get_edge_report_impl,
    handle_ws_update as _handle_ws_update_core,
    init_state,
    initialize as _initialize_impl,
    start as _start_impl,
    stop as _stop_impl,
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
    handle_ws_update as _handle_ws_update_impl,  # noqa: F401 (re-exported via core)
    incremental_loop as _incremental_loop_impl,
    start_ws as _start_ws_impl,
    stop_ws_and_incremental as _stop_ws_and_incremental_impl,
)

load_dotenv()

logger = logging.getLogger("callisto.line_monitor")

DB_PATH = _config.DB_PATH

# Snapshot interval in seconds — see tools.lines.config (slice-6 extraction)
SNAPSHOT_INTERVAL = _config.SNAPSHOT_INTERVAL

# Sports to monitor — see tools.lines.config (slice-6 extraction)
MONITORED_SPORTS = _config.MONITORED_SPORTS

# Event-driven odds update config — see tools.lines.config (slice-6 extraction)
WS_SPORTS = _config.WS_SPORTS
WS_ENABLED = _config.WS_ENABLED
INCREMENTAL_ENABLED = _config.INCREMENTAL_ENABLED
INCREMENTAL_INTERVAL = _config.INCREMENTAL_INTERVAL
REQUIRE_MODEL_AGREEMENT = _config.REQUIRE_MODEL_AGREEMENT


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
    """Autonomous line movement detection engine.

    Facade only — every method delegates to tools.lines.*; __init__
    state lives in tools.lines.core.init_state.
    """

    def __init__(self, db_path: str = DB_PATH):
        init_state(self, db_path, monitored_sports=MONITORED_SPORTS)
        # Extracted collaborators (tools/lines/)
        self._kl_tracker = KLDivergenceTracker(db_path=db_path)
        self._evaluator: Optional[MovementEvaluator] = None

    async def initialize(self) -> None:
        """Create tables for odds snapshots and alerts.

        Delegates to tools.lines.core.initialize (DDL in
        tools.lines.schema — per-statement DDL avoids EXCLUSIVE lock
        contention, security audit C-6).
        """
        await _initialize_impl(self, ensure_prop_schema=ensure_prop_schema)

    async def start(self) -> None:
        """Start the background monitoring loop — delegates to tools.lines.core.start."""
        await _start_impl(
            self,
            monitored_sports=MONITORED_SPORTS,
            snapshot_interval=SNAPSHOT_INTERVAL,
            ws_enabled=WS_ENABLED,
            incremental_enabled=INCREMENTAL_ENABLED,
            monitor_loop_fn=self._monitor_loop,
            incremental_loop_fn=self._incremental_loop,
        )

    async def stop(self) -> None:
        """Stop the monitoring loop and close DB — delegates to tools.lines.core.stop."""
        await _stop_impl(self)

    # --- WebSocket path -----------------------------------------------------
    async def _start_ws(self) -> None:
        """Open the odds-api.io WebSocket and wire updates into _process_ws_update.

        Delegates to tools.lines.ws_stream.start_ws.
        """
        await _start_ws_impl(self)

    async def _handle_ws_update(self, data: dict) -> None:
        """WS callback — merge a single delta into our latest snapshot.

        Counters + detector isolation live in tools.lines.core.handle_ws_update;
        the merge itself in tools.lines.ws_stream.
        """
        await _handle_ws_update_core(
            self, data,
            process_snapshot=self._process_snapshot,
        )

    # --- Incremental /odds/updated path -------------------------------------
    async def _incremental_loop(self) -> None:
        """Poll /odds/updated?since=X — delegates to tools.lines.ws_stream."""
        await _incremental_loop_impl(self, monitored_sports=MONITORED_SPORTS)

    def get_ws_status(self) -> dict:
        """Telemetry snapshot — exposed via /health and /system/full-status."""
        from tools.lines.ws_stream import ws_status_fields
        return ws_status_fields(self)

    async def wait_for_drain(self, timeout: float = 60) -> bool:
        """Pause the monitor and wait until all in-flight DB ops complete.

        Delegates to tools.lines.lifecycle.wait_for_drain.
        """
        return await _wait_for_drain_impl(self, timeout=timeout)

    def resume(self) -> None:
        """Release the drain lock and unpause the monitor.

        Must be called after wait_for_drain() succeeds, in a try/finally
        block to guarantee the lock is released.

        Delegates to tools.lines.lifecycle.resume_monitor.
        """
        _resume_monitor_impl(self)

    async def _monitor_loop(self) -> None:
        """Main monitoring loop — snapshot, compare, alert.

        Delegates each cycle body to tools.lines.monitor_loop.run_monitor_cycle
        (credit-aware fallback switch, adaptive interval stretch, per-sport
        backoff, free prop cascade). The loop scaffolding lives in
        tools.lines.lifecycle.monitor_loop; this wrapper owns the running flag.
        """
        await _monitor_loop_body(
            self,
            monitored_sports=MONITORED_SPORTS,
            snapshot_interval=SNAPSHOT_INTERVAL,
            get_credit_status=get_credit_status,
        )

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
        """Return monitor status with DB-backed counts.

        Delegates to tools.lines.lifecycle.build_status.
        """
        return await _build_status_impl(
            self,
            monitored_sports=MONITORED_SPORTS,
            snapshot_interval=SNAPSHOT_INTERVAL,
            get_credit_status=get_credit_status,
        )

    def get_edge_report(self, sport: Optional[str] = None) -> dict:
        """Get the latest edge scan report — delegates to tools.lines.core."""
        return _get_edge_report_impl(self, sport)

    async def force_snapshot(self, sport: str) -> dict:
        """Manually trigger a snapshot for a sport. Returns the snapshot data.

        Delegates to tools.lines.core.force_snapshot.
        """
        return await _force_snapshot_impl(self, sport, snapshot_sport=self._snapshot_sport)
