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
from tools.odds_api_io import (
    get_odds as odds_api_io_get_odds,
    get_usage_status as odds_api_io_usage,
    get_value_bets as odds_api_io_value_bets,
)
from tools.oddspapi import get_odds as oddspapi_get_odds, get_usage_status as oddspapi_usage
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
    "basketball_ncaab,basketball_nba,americanfootball_nfl,golf_pga,baseball_mlb",
).split(",")

# Movement thresholds — what counts as "significant"
PRICE_MOVEMENT_THRESHOLD = 10    # American odds points (e.g., -110 → -120)
POINT_MOVEMENT_THRESHOLD = 1.0   # Spread/total points (e.g., -3.5 → -4.5)

# Minimum edge for alert
MIN_EDGE_ALERT = 0.03  # 3% edge minimum to flag as interesting


class LineMonitor:
    """Autonomous line movement detection engine."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False  # Set True to pause snapshot writes (during backtests)
        self._snapshots: dict[str, dict] = {}  # sport -> last snapshot (only latest per sport)
        self._alerts: list[dict] = []  # Recent movement alerts (capped at 100)
        self._latest_edge_reports: dict[str, dict] = {}  # sport -> latest edge scan (only latest per sport)
        self._kl_cache: dict[str, dict] = {}  # "sport:event_id:market" -> KL metrics
        # Self-healing: track consecutive all-source failures per sport.
        # Alert via Telegram only after 3+ consecutive failures.
        self._consecutive_failures: dict[str, int] = {}  # sport -> count
        self._FAILURE_ALERT_THRESHOLD = 3

    async def initialize(self) -> None:
        """Create tables for odds snapshots and alerts."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                game_count INTEGER DEFAULT 0,
                credits_remaining INTEGER
            );

            CREATE TABLE IF NOT EXISTS line_movements (
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
            );

            CREATE TABLE IF NOT EXISTS ev_opportunities (
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
                status TEXT DEFAULT 'open'
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_sport_ts ON odds_snapshots(sport, timestamp);
            CREATE INDEX IF NOT EXISTS idx_movements_sport ON line_movements(sport, detected_at);
            CREATE INDEX IF NOT EXISTS idx_ev_status ON ev_opportunities(status, detected_at);
        """)
        await self._db.commit()
        # Ensure prop_snapshots table exists
        await ensure_prop_schema(self.db_path)
        logger.info("Line monitor initialized (with prop snapshots)")

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            f"Line monitor started — {len(MONITORED_SPORTS)} sports, "
            f"{SNAPSHOT_INTERVAL}s interval"
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
        if self._db:
            await self._db.close()
        logger.info("Line monitor stopped")

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
                await asyncio.sleep(5)
                continue
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

                for sport in MONITORED_SPORTS:
                    if use_fallback:
                        await self._snapshot_sport_fallback(sport.strip())
                    else:
                        await self._snapshot_sport(sport.strip())

                # Prop snapshots — free cascade (DK + FD + BetMGM), no credits
                await self._snapshot_props()

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
            try:
                result = await scrape_all_props(sport)
                if result.get("error") or not result.get("props"):
                    continue
                stored = await store_prop_snapshot(result["props"], sport, self.db_path)
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

        # 5. BetMGM — DISABLED: redundant with odds-api.io Pro (includes BetMGM).
        # Scraped endpoint returns 400/403 consistently, generating log noise.
        # Re-enable only if odds-api.io loses BetMGM coverage.

        # 6. OddsPapi — 250/month free (last resort)
        if not scraped:
            try:
                usage = oddspapi_usage()
                if usage.get("requests_remaining", 0) > 0 and usage.get("api_key_set"):
                    op_data = await oddspapi_get_odds(sport)
                    if not op_data.get("error") and op_data.get("game_count", 0) > 0:
                        scraped["oddspapi"] = op_data
                        logger.info(
                            f"OddsPapi {sport}: {op_data['game_count']} games "
                            f"({usage['requests_remaining'] - 1} left)"
                        )
            except Exception as e:
                logger.warning(f"OddsPapi failed for {sport}: {e}")

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
            # Use h2h,spreads,totals = 3 credits per sport
            new_snapshot = await get_odds(
                sport=sport,
                regions="us",
                markets="h2h,spreads,totals",
                odds_format="american",
            )

            if new_snapshot.get("error"):
                logger.warning(f"Snapshot error for {sport}: {new_snapshot['error']} — trying fallbacks")
                await self._snapshot_sport_fallback(sport)
                return

            # Enrich with fresh scraper data from all free sources (always)
            new_snapshot = await self._enrich_with_dk(sport, new_snapshot)
            new_snapshot = await self._enrich_with_fd(sport, new_snapshot)
            # BetMGM enrichment disabled — redundant with odds-api.io Pro
            # new_snapshot = await self._enrich_with_mgm(sport, new_snapshot)

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

        Shared pipeline used by both primary (Odds API) and fallback
        (DraftKings scraper, OddsPapi) snapshot paths.
        """
        now = datetime.now(timezone.utc).isoformat()
        game_count = new_snapshot.get("game_count", 0)
        credits_remaining = new_snapshot.get("credits", {}).get("remaining")
        source = new_snapshot.get("source", "odds_api")

        # Store snapshot
        await self._db.execute(
            "INSERT INTO odds_snapshots (sport, timestamp, snapshot_json, game_count, credits_remaining) "
            "VALUES (?, ?, ?, ?, ?)",
            (sport, now, json.dumps(new_snapshot), game_count, credits_remaining),
        )
        await self._db.commit()

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

        # Also cache in historical_odds_cache format for backtesting.
        # Every live snapshot with multiple books becomes backtest-grade data.
        # This is how the system accumulates real multi-book odds over time.
        book_count = 0
        for g in new_snapshot.get("games", []):
            book_count = max(book_count, len(g.get("bookmakers", [])))
        if book_count >= 2 and game_count > 0:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                await self._db.execute(
                    "INSERT OR REPLACE INTO historical_odds_cache "
                    "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, fetched_at) "
                    "VALUES (?, ?, NULL, 'h2h,spreads,totals', ?, 0, ?)",
                    (sport, today, json.dumps(new_snapshot), now),
                )
                await self._db.commit()
                logger.info(f"Cached multi-book snapshot for backtest: {sport} {today} ({book_count} books)")
            except Exception as e:
                logger.warning(f"Failed to cache snapshot for backtest: {e}")

        # Run edge scanner on every snapshot
        edge_report = full_edge_scan(new_snapshot)
        self._latest_edge_reports[sport] = edge_report
        total_edges = edge_report.get("total_edges", 0)
        if total_edges > 0:
            logger.info(f"Edge scan {sport}: {total_edges} edges found")

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
            # Data logged for analysis but NO Telegram alerts — too noisy
            sharp_signals = detect_sharp_money(old_snapshot, new_snapshot)
            if sharp_signals:
                logger.info(f"SHARP MONEY: {sport} — {len(sharp_signals)} signals (logged, no alert)")
                for sig in sharp_signals:
                    self._alerts.append({"sport": sport, "type": "sharp_money", **sig})
                # Cap alerts to prevent unbounded growth
                if len(self._alerts) > 100:
                    self._alerts = self._alerts[-100:]

            # Compute KL divergence between previous and current snapshot
            # Measures information flow — how much the market "learned" between snapshots.
            await self._compute_and_store_kl(sport, old_snapshot, new_snapshot)

        self._snapshots[sport] = new_snapshot

    async def _record_movement(self, sport: str, movement: dict) -> None:
        """Record a line movement to the database."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
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
        )
        await self._db.commit()

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

                    # Cache in memory for edge_confidence lookups
                    cache_key = f"{sport}:{event_id}:{market_type}"
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

            # Implied range sanity on devigged probs
            fair_range = max(devigged_fair_probs) - min(devigged_fair_probs)
            if fair_range > 0.25:
                logger.warning(
                    f"Edge eval: implausible devigged range {fair_range:.1%} "
                    f"for {target_team} {market}, skipping"
                )
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
                    now = datetime.now(timezone.utc).isoformat()
                    await self._db.execute(
                        "INSERT INTO ev_opportunities "
                        "(detected_at, sport, game_id, team, market, bookmaker, "
                        "american_odds, implied_probability, estimated_true_prob, "
                        "edge, expected_value, kelly_fraction) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            now, sport, game.get("id", ""), target_team, market,
                            movement["bookmaker"], new_price,
                            round(moved_implied, 4), round(consensus_prob, 4),
                            round(edge, 4), ev_result["expected_value"],
                            ev_result["kelly_fraction"],
                        ),
                    )
                    await self._db.commit()

                    logger.info(
                        f"+EV OPPORTUNITY: {target_team} {market} @ {new_price} "
                        f"(edge={edge:.1%}, EV=${ev_result['expected_value']}, "
                        f"Kelly={ev_result['kelly_fraction']:.1%}, "
                        f"devig_books={len(devigged_fair_probs)})"
                    )
                    # Autonomous loop will pick this up and analyze via AGP
            break

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

    def get_status(self) -> dict:
        """Return monitor status."""
        return {
            "running": self._running,
            "monitored_sports": MONITORED_SPORTS,
            "snapshot_interval_seconds": SNAPSHOT_INTERVAL,
            "cached_snapshots": list(self._snapshots.keys()),
            "recent_alerts": len(self._alerts),
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
