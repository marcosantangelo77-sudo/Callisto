"""Snapshot pipeline — per-sport odds fetch, enrichment, storage and analysis.

This is the remaining core of the old LineMonitor body, extracted from
tools/line_monitor.py (slice 5). Everything here operates on a *monitor*
object (the LineMonitor instance) whose private attributes form the
contract:

    mon.db_path                 sqlite path
    mon._db                     aiosqlite connection (initialized)
    mon._snapshots              sport -> latest snapshot dict
    mon._latest_edge_reports    sport -> latest edge scan report
    mon._alerts                 bounded deque of alerts
    mon._kl_tracker             KLDivergenceTracker
    mon._evaluator              MovementEvaluator | None (lazy)
    mon._in_flight_db           True while a DB write is in progress
    mon._snapshot_lock          asyncio.Lock guarding _process_snapshot
    mon._process_snapshot       shared entry point (lock wrapper in
                                process_snapshot() below)

No network calls happen at import time and nothing here touches paper-trade
signal statuses or live betting paths.
"""

import logging
from datetime import datetime, timezone

from tools.lines.ingest import (
    enrich_with_scraper,
    merge_delta_into_snapshot,
    stamp_snapshot_fetched_at,
)
from tools.lines.movement import filter_significant
from tools.lines.monitor_loop import (
    handle_sharp_signals,
    record_significant_movements,
    snapshot_sport_fallback,
)
from tools.lines.snapshot_ops import (
    cache_snapshot_for_backtest,
    default_closing_window,
    insert_snapshot_record,
    store_market_microstructure,
)
from tools.lines.edge_report import (
    MovementEvaluator,
    check_model_agreement as _check_model_agreement_core,
)

logger = logging.getLogger("callisto.line_monitor")


# ── Scraper enrichment -------------------------------------------------------

async def enrich_with_dk(sport: str, snapshot: dict) -> dict:
    """Merge fresh DK scraper data into an Odds API snapshot."""
    from tools.dk_scraper import scrape_dk_odds
    return await enrich_with_scraper(
        sport, snapshot, scrape_dk_odds, "draftkings", ("draft_kings",),
    )


async def enrich_with_fd(sport: str, snapshot: dict) -> dict:
    """Merge fresh FanDuel scraper data into an odds snapshot."""
    from tools.fanduel_scraper import scrape_fd_odds
    return await enrich_with_scraper(
        sport, snapshot, scrape_fd_odds, "fanduel", ("fan_duel",),
    )


async def enrich_with_mgm(sport: str, snapshot: dict) -> dict:
    """Merge fresh BetMGM scraper data into an odds snapshot."""
    from tools.betmgm_scraper import scrape_betmgm_odds
    return await enrich_with_scraper(
        sport, snapshot, scrape_betmgm_odds, "betmgm", ("bet_mgm",),
    )


async def enrich_with_fanatics(sport: str, snapshot: dict) -> dict:
    """Merge fresh Fanatics scraper data into an odds snapshot.

    Same pattern as the other enrichment helpers. Fanatics is the
    secondary book (per project_sportsbooks) so we always pull a
    fresh scrape when the sport is supported. Silent on failure — the
    Fanatics endpoints are UNDOCUMENTED and we expect them to break
    periodically; @tracked_ingestion records the outage.

    Note the import-failure guard lives here rather than at call time so
    unsupported deployments degrade to a plain passthrough.
    """
    try:
        from tools.fanatics_scraper import FANATICS_LEAGUE_KEYS
    except Exception:
        return snapshot
    if sport not in FANATICS_LEAGUE_KEYS:
        return snapshot
    from tools.fanatics_scraper import fetch_fanatics_odds
    return await enrich_with_scraper(
        sport, snapshot, fetch_fanatics_odds, "fanatics", ("fanatics_sportsbook",),
    )


# ── Primary / fallback snapshot fetch ----------------------------------------

async def snapshot_sport(monitor, sport: str) -> None:
    """Take an odds snapshot for a sport and compare with previous.

    Always enriches with DK + FanDuel + Fanatics scraper data (free) to ensure:
    1. DK/FD lines are fresh from source (target books)
    2. More bookmakers in the snapshot = better devig consensus
    3. If Odds API data is stale, scrapers overwrite it
    """
    from tools.odds_api_io import get_odds as odds_api_io_get_odds

    try:
        # Primary: Odds-API.io Pro (15 books, 30K req/hr)
        # the-odds-api.com is out of credits — skip it entirely.
        new_snapshot = await odds_api_io_get_odds(sport)

        if new_snapshot.get("error") or not new_snapshot.get("games"):
            logger.warning(
                f"Snapshot error for {sport}: {new_snapshot.get('error', 'no games')}"
                " — trying fallbacks"
            )
            await fallback_snapshot(monitor, sport)
            return

        # Enrich with fresh scraper data from all free sources (always)
        new_snapshot = await monitor._enrich_with_dk(sport, new_snapshot)
        new_snapshot = await monitor._enrich_with_fd(sport, new_snapshot)
        new_snapshot = await monitor._enrich_with_fanatics(sport, new_snapshot)

        await monitor._process_snapshot(sport, new_snapshot)

    except Exception as e:
        logger.error(f"Snapshot failed for {sport}: {e}")


async def fallback_snapshot(monitor, sport: str) -> None:
    """Take an odds snapshot using all available sources.

    Called when Odds API credits are exhausted or unavailable.
    Delegates to tools.lines.monitor_loop.snapshot_sport_fallback.
    """
    from tools.odds_api_io import (
        get_odds as odds_api_io_get_odds,
        get_usage_status as odds_api_io_usage,
    )
    await snapshot_sport_fallback(
        monitor,
        sport,
        odds_api_io_get_odds=odds_api_io_get_odds,
        odds_api_io_usage=odds_api_io_usage,
    )


# ── Snapshot processing pipeline ----------------------------------------------

async def process_snapshot(monitor, sport: str, new_snapshot: dict) -> None:
    """Lock + in-flight guard around snapshot processing.

    Acquires ``monitor._snapshot_lock`` so ``wait_for_drain()`` can
    guarantee no in-flight snapshot is running, then sets
    ``_in_flight_db`` for legacy callers. Dispatches to
    ``monitor._process_snapshot_inner`` so tests can override the inner
    method on the facade instance.
    """
    async with monitor._snapshot_lock:
        monitor._in_flight_db = True
        try:
            await monitor._process_snapshot_inner(sport, new_snapshot)
        finally:
            monitor._in_flight_db = False


async def process_snapshot_inner(monitor, sport: str, new_snapshot: dict) -> None:
    """Inner snapshot processing — separated so _in_flight_db wraps all DB ops.

    Caller must hold monitor._snapshot_lock and manage _in_flight_db.
    """
    now = datetime.now(timezone.utc).isoformat()
    game_count = new_snapshot.get("game_count", 0)
    credits_remaining = new_snapshot.get("credits", {}).get("remaining")
    source = new_snapshot.get("source", "odds_api")

    # Stamp fetched_at on every bookmaker entry in the snapshot JSON so
    # downstream consumers can compute freshness decay even when the outer
    # row timestamp has drifted from the actual fetch time. Idempotent — see
    # _stamp_now for details.
    stamp_snapshot_fetched_at(new_snapshot, now)

    # ingest_source defaults to the snapshot's 'ingest_source' tag; callers
    # in the WS/incremental paths set this to 'ws' or 'incremental'. The
    # legacy 'source' field above is the provider name ('odds_api',
    # 'draftkings', etc.) and is a different axis.
    ingest_source = new_snapshot.get("ingest_source", "interval")

    # WS/incremental deltas arrive as SINGLE-bookmaker, single-game
    # snapshots. If we hand that to process_snapshot_inner as-is it would
    # overwrite the multi-book _snapshots[sport] with the delta, breaking
    # the next consensus scan. Merge instead: take the most recent full
    # snapshot for this sport and splice the WS delta onto it so downstream
    # edge scanning still has every book present.
    if ingest_source in ("ws", "incremental"):
        prior = monitor._snapshots.get(sport)
        if prior is not None and new_snapshot.get("games"):
            new_snapshot = merge_delta_into_snapshot(prior, new_snapshot, now)

    # Store snapshot (retry-wrapped; see tools.lines.snapshot_ops).
    await insert_snapshot_record(
        monitor._db,
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
    await cache_snapshot_for_backtest(
        monitor._db, sport=sport, snapshot=new_snapshot, now_iso=now,
    )

    # Run edge scanner on every snapshot
    from tools.edge_scanner import full_edge_scan
    edge_report = full_edge_scan(new_snapshot)
    monitor._latest_edge_reports[sport] = edge_report
    total_edges = edge_report.get("total_edges", 0)
    if total_edges > 0:
        logger.info(f"Edge scan {sport}: {total_edges} edges found")

    # Store market microstructure metrics from edge scan
    await store_market_microstructure(
        monitor._db, sport=sport, edge_report=edge_report, now_iso=now,
    )

    # NOTE: Raw edges are NOT sent to Telegram here.
    # The autonomous loop analyzes candidates via full AGP sessions
    # and only alerts after the Architect confirms the edge is real.

    # Compare with previous snapshot
    old_snapshot = monitor._snapshots.get(sport)
    if old_snapshot:
        from tools.odds_api import detect_line_movement
        from tools.edge_scanner import detect_sharp_money
        movements = detect_line_movement(old_snapshot, new_snapshot)
        significant = filter_significant(movements)

        if significant:
            logger.info(
                f"MOVEMENT DETECTED: {sport} — {len(significant)} significant moves"
            )
            await record_significant_movements(monitor, sport, significant, new_snapshot)

        # Detect sharp money (one book moved, others didn't)
        sharp_signals = detect_sharp_money(old_snapshot, new_snapshot)
        if sharp_signals:
            logger.info(f"SHARP MONEY: {sport} — {len(sharp_signals)} signals")
            await handle_sharp_signals(monitor._alerts, sport, sharp_signals)

        # Compute KL divergence between previous and current snapshot
        # Measures information flow — how much the market "learned".
        await monitor._compute_and_store_kl(sport, old_snapshot, new_snapshot)

    # ── CLV bridge: capture closing lines for games about to start ──────
    # If a game starts within the next snapshot interval, this is the
    # last snapshot we'll get before tip-off — treat it as the closing line.
    await capture_closing_lines(monitor, sport, new_snapshot)

    monitor._snapshots[sport] = new_snapshot


# ── CLV bridge -----------------------------------------------------------------

async def capture_closing_lines(monitor, sport: str, snapshot: dict) -> None:
    """Push closing lines to CLV tracker for games about to start."""
    from tools.lines.snapshot_ops import (
        capture_closing_lines as capture_impl,
    )
    try:
        # Import CLV tracker from the global API state
        from api import clv_tracker as _clv
        if _clv is None:
            return

        await capture_impl(
            _clv,
            sport=sport,
            snapshot=snapshot,
            closing_window_seconds=default_closing_window(_snapshot_interval()),
        )
    except ImportError:
        pass  # CLV tracker not available
    except Exception as e:
        logger.warning(f"CLV closing line capture failed for {sport}: {e}")


def _snapshot_interval() -> int:
    """Lazy SNAPSHOT_INTERVAL lookup — avoids a circular module-level import."""
    from tools.line_monitor import SNAPSHOT_INTERVAL
    return SNAPSHOT_INTERVAL


# ── Movement recording + EV evaluation ------------------------------------------

async def record_movement(monitor, sport: str, movement: dict) -> None:
    """Record a line movement (delegates to tools.lines.snapshot_ops)."""
    from tools.lines.snapshot_ops import record_line_movement as record_impl
    await record_impl(monitor._db, monitor._alerts, sport=sport, movement=movement)


def get_or_create_evaluator(monitor):
    """Lazily construct the MovementEvaluator bound to this monitor's DB.

    Returns the evaluator instance stored on monitor._evaluator; builds it
    on first use with an ev_opportunities INSERT callback.
    """
    if monitor._evaluator is None:

        async def _insert_ev(row: dict) -> None:
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                monitor._db,
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
                monitor._db, max_retries=5,
                operation=f"ev_opportunity commit {row['sport']}",
            )

        monitor._evaluator = MovementEvaluator(
            insert_ev=_insert_ev,
            get_edge_report=lambda s: monitor._latest_edge_reports.get(s),
        )

    return monitor._evaluator


async def evaluate_movement(
    monitor, sport: str, movement: dict, snapshot: dict,
    require_model_agreement: bool,
) -> None:
    """Evaluate whether a line movement creates a +EV opportunity."""
    evaluator = get_or_create_evaluator(monitor)
    await evaluator.evaluate(
        sport, movement, snapshot,
        require_model_agreement=require_model_agreement,
    )


def model_agreement(
    monitor, *, sport: str, game: dict, team: str, market: str, direction: str,
) -> tuple[bool, str]:
    """Return (ok, label) indicating whether any registered model agrees.

    Uses the latest cached edge report for this sport. `direction` is kept
    in the signature for API compatibility with the pre-extraction method.
    """
    report = monitor._latest_edge_reports.get(sport) or {}
    game_id = str(game.get("id", ""))
    return _check_model_agreement_core(
        report=report, game_id=game_id, team=team, market=market,
    )
