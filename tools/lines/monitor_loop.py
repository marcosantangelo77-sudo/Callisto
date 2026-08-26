"""
Monitor loop operations for the line monitor — slice-3 extraction.

Extracted from tools/line_monitor.py so the LineMonitor class stays a thin
facade over tools/lines/ internals. Everything here takes its collaborators
explicitly (the monitor instance for shared state, db handle, config values)
and never imports tools.line_monitor, so there is no import cycle:

- compute_adaptive_interval   — credit-aware fallback switch + interval stretch
- run_monitor_cycle           — one iteration of the main monitoring loop
- snapshot_props              — free prop-scraper cascade across prop sports
- snapshot_sport_fallback     — all-source fallback snapshot with failure
                                counting and Telegram escalation
- record_significant_movements— movement recording + evaluation + event bus
- handle_sharp_signals        — sharp-money alert sink + high-confidence alert
- fetch_recent_movements      — line_movements SELECT (optionally per sport)
- fetch_ev_opportunities      — ev_opportunities SELECT by status
- fetch_snapshot_history      — odds_snapshots metadata SELECT
- collect_status_counts       — aggregate DB counters for get_status()

Behavior is identical to the inline code it was extracted from; only the
plumbing moved.
"""

import asyncio
import logging

from tools.prop_scraper_free import scrape_all_props, store_prop_snapshot

logger = logging.getLogger("callisto.line_monitor.monitor_loop")

# Snapshot timeout per sport — matches the historical inline value.
SNAPSHOT_TIMEOUT_S = 120
PROPS_TIMEOUT_S = 180


def compute_adaptive_interval(credits: dict, snapshot_interval: int) -> tuple[bool, int]:
    """Decide whether to use free fallbacks and how long to sleep.

    Returns (use_fallback, interval_seconds).

    Rules (unchanged from the original inline logic):
    - No Odds API key at all -> straight to fallbacks.
    - Fewer than 50 credits remaining -> free scrapers instead of pausing.
    - Adaptive stretch: <50 credits -> at least 1hr between cycles;
      <100 credits -> at least 30min. Fallback mode keeps base interval.
    """
    use_fallback = False

    if not credits.get("api_key_set"):
        # No Odds API key at all — go straight to fallbacks
        logger.info("ODDS_API_KEY not set — using free fallback sources")
        use_fallback = True

    remaining = credits.get("remaining")
    if remaining is not None and remaining < 50:
        logger.info(
            f"Odds API credits low ({remaining}) — switching to free scrapers "
            f"(DK + FanDuel)"
        )
        use_fallback = True

    interval = snapshot_interval
    if not use_fallback and remaining is not None:
        if remaining < 50:
            interval = max(snapshot_interval, 3600)  # 1hr when low
            logger.info(f"Credits low ({remaining}) — slowing to {interval}s")
        elif remaining < 100:
            interval = max(snapshot_interval, 1800)  # 30min when moderate

    return use_fallback, interval


async def run_monitor_cycle(monitor, *, monitored_sports: list[str], snapshot_interval: int,
                            get_credit_status) -> int:
    """Run ONE iteration of the main monitoring loop body.

    Takes snapshots for every monitored sport (with backoff skipping for
    chronically failing sports), then the free prop cascade, and returns
    the computed interval so the caller can sleep. The caller owns the
    pause/ack handshake — this function assumes it is not paused on entry
    and bails out early (returning immediately) if a pause starts mid-cycle.
    """
    credits = get_credit_status()
    use_fallback, interval = compute_adaptive_interval(credits, snapshot_interval)

    # Cycle counter for backoff scheduling
    if not hasattr(monitor, "_cycle_n"):
        monitor._cycle_n = 0
    monitor._cycle_n += 1

    for sport in monitored_sports:
        if monitor._paused:
            break  # Exit early — autonomous loop waiting for us
        s = sport.strip()
        # Backoff for out-of-season / chronically failing sports:
        # 5+ consecutive failures -> skip 3 cycles between attempts
        # 10+ -> skip 7 cycles between attempts
        fail_count = monitor._consecutive_failures.get(s, 0)
        if fail_count >= 10 and monitor._cycle_n % 8 != 0:
            continue
        if fail_count >= 5 and monitor._cycle_n % 4 != 0:
            continue
        try:
            if use_fallback:
                await asyncio.wait_for(
                    monitor._snapshot_sport_fallback(s), timeout=SNAPSHOT_TIMEOUT_S)
            else:
                await asyncio.wait_for(
                    monitor._snapshot_sport(s), timeout=SNAPSHOT_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"Snapshot for {s} timed out after {SNAPSHOT_TIMEOUT_S}s — skipping")
            monitor._consecutive_failures[s] = (
                monitor._consecutive_failures.get(s, 0) + 1)

    # Prop snapshots — free cascade (DK + FD + BetMGM), no credits
    if not monitor._paused:
        try:
            await asyncio.wait_for(
                snapshot_props(monitor, monitored_sports), timeout=PROPS_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"Prop snapshot timed out after {PROPS_TIMEOUT_S}s — skipping")

    return interval


PROP_SPORTS = frozenset({
    "basketball_nba", "baseball_mlb", "icehockey_nhl", "americanfootball_nfl",
})


async def snapshot_props(monitor, monitored_sports: list[str]) -> None:
    """Scrape player props from all free sources for the supported sports.

    (DK + FanDuel + BetMGM) cascade for each sport that has
    prop markets, stores results in prop_snapshots table via
    store_prop_snapshot. Zero credit cost.
    """
    prop_sports = [s.strip() for s in monitored_sports if s.strip() in PROP_SPORTS]
    if not prop_sports:
        return

    total_stored = 0
    for sport in prop_sports:
        if monitor._paused:
            break
        try:
            result = await scrape_all_props(sport)
            if result.get("error") or not result.get("props"):
                continue
            if monitor._paused:
                break
            async with monitor._snapshot_lock:
                monitor._in_flight_db = True
                try:
                    stored = await store_prop_snapshot(
                        result["props"], sport, monitor.db_path)
                finally:
                    monitor._in_flight_db = False
            total_stored += stored
            logger.info(
                f"Props {sport}: {stored} lines stored "
                f"({result.get('multi_book_count', 0)} multi-book)"
            )
        except Exception as e:
            logger.warning(f"Prop snapshot failed for {sport}: {e}")

    if total_stored > 0:
        logger.info(f"Prop snapshot cycle complete: {total_stored} total lines stored")


async def snapshot_sport_fallback(monitor, sport: str, *,
                                  odds_api_io_get_odds, odds_api_io_usage) -> None:
    """Take an odds snapshot using all available sources.

    Called when Odds API credits are exhausted or unavailable.
    Delegates the scraper cascade to tools.lines.fallback_cascade
    (priority: odds-api.io Pro -> DK -> Action Network -> FD ->
    Fanatics). Merges all successful sources for maximum multi-book
    coverage. Tracks consecutive failures and escalates to Telegram
    after monitor._FAILURE_ALERT_THRESHOLD consecutive all-source
    failures for the sport.
    """
    from tools.lines.fallback_cascade import collect_free_sources, merge_free_sources

    scraped = await collect_free_sources(
        sport,
        odds_api_io_get_odds=odds_api_io_get_odds,
        odds_api_io_usage=odds_api_io_usage,
    )

    if not scraped:
        # Track consecutive failures for self-healing alerts
        monitor._consecutive_failures[sport] = (
            monitor._consecutive_failures.get(sport, 0) + 1)
        count = monitor._consecutive_failures[sport]
        logger.warning(
            f"All fallback sources failed for {sport} — skipping snapshot "
            f"(consecutive failures: {count})"
        )
        if count >= monitor._FAILURE_ALERT_THRESHOLD:
            try:
                from tools import telegram
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
    monitor._consecutive_failures[sport] = 0

    new_snapshot = merge_free_sources(scraped, sport)
    await monitor._process_snapshot(sport, new_snapshot)


async def record_significant_movements(monitor, sport: str,
                                       significant: list[dict],
                                       new_snapshot: dict) -> None:
    """Record, evaluate, and publish each significant line movement."""
    for mov in significant:
        await monitor._record_movement(sport, mov)
        await monitor._evaluate_movement(sport, mov, new_snapshot)
        # Publish line movement event
        try:
            from tools.event_bus import get_event_bus, EVENT_LINE_MOVED
            await get_event_bus().publish(EVENT_LINE_MOVED, {
                "sport": sport, **mov,
            })
        except Exception:
            pass


async def handle_sharp_signals(sink, sport: str, signals: list[dict]) -> None:
    """Append sharp-money signals to the alert sink and alert Telegram.

    `sink` is a list or collections.deque (both supported). Only
    high-confidence moves (3+ stale books and at least one mover)
    fire the alert_sharp_move notification. The sink is trimmed to the
    most recent 100 entries to prevent unbounded growth.
    """
    for sig in signals:
        sink.append({"sport": sport, "type": "sharp_money", **sig})
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
    if len(sink) > 100:
        if hasattr(sink, "popleft"):  # collections.deque
            while len(sink) > 100:
                sink.popleft()
        else:
            del sink[:-100]


async def fetch_recent_movements(db, sport: "str | None" = None, limit: int = 20) -> list[dict]:
    """Get recent line movements from the database."""
    if sport:
        cursor = await db.execute(
            "SELECT * FROM line_movements WHERE sport = ? ORDER BY detected_at DESC LIMIT ?",
            (sport, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM line_movements ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


async def fetch_ev_opportunities(db, status: str = "open", limit: int = 20) -> list[dict]:
    """Get current +EV opportunities."""
    cursor = await db.execute(
        "SELECT * FROM ev_opportunities WHERE status = ? ORDER BY detected_at DESC LIMIT ?",
        (status, limit),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


async def fetch_snapshot_history(db, sport: str, limit: int = 10) -> list[dict]:
    """Get snapshot history for a sport (metadata only, no full JSON)."""
    cursor = await db.execute(
        "SELECT id, sport, timestamp, game_count, credits_remaining "
        "FROM odds_snapshots WHERE sport = ? ORDER BY timestamp DESC LIMIT ?",
        (sport, limit),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


async def collect_status_counts(db) -> dict:
    """Aggregate DB-backed counters used by LineMonitor.get_status().

    Returns a dict with db_snapshots_total, latest_snapshot_at,
    db_movements_total, and db_closing_lines. Every query failure is
    swallowed (missing table / closed db yields zeros / Nones).
    """
    out = {
        "db_snapshots_total": 0,
        "latest_snapshot_at": None,
        "db_movements_total": 0,
        "db_closing_lines": 0,
    }
    try:
        row = await (await db.execute(
            "SELECT COUNT(*), MAX(timestamp) FROM odds_snapshots"
        )).fetchone()
        if row:
            out["db_snapshots_total"] = row[0] or 0
            out["latest_snapshot_at"] = row[1]
        row2 = await (await db.execute(
            "SELECT COUNT(*) FROM line_movements"
        )).fetchone()
        out["db_movements_total"] = row2[0] if row2 else 0
        try:
            row3 = await (await db.execute(
                "SELECT COUNT(*) FROM closing_lines"
            )).fetchone()
            out["db_closing_lines"] = row3[0] if row3 else 0
        except Exception:
            pass  # Table may not exist yet
    except Exception:
        pass
    return out
