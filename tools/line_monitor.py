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
from tools.edge_scanner import full_edge_scan, detect_sharp_money
from tools.parlay_scanner import find_correlated_parlay_edges, analyze_live_overreaction
from tools import telegram
from tools.dk_scraper import scrape_dk_odds
from tools.oddspapi import get_odds as oddspapi_get_odds, get_usage_status as oddspapi_usage

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
    "basketball_ncaab,basketball_nba,americanfootball_nfl,golf_pga",
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
        self._snapshots: dict[str, dict] = {}  # sport -> last snapshot
        self._alerts: list[dict] = []  # Recent movement alerts
        self._latest_edge_reports: dict[str, dict] = {}  # sport -> latest edge scan

    async def initialize(self) -> None:
        """Create tables for odds snapshots and alerts."""
        self._db = await aiosqlite.connect(self.db_path)
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
        logger.info("Line monitor initialized")

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
            try:
                credits = get_credit_status()
                use_fallback = False

                if not credits.get("api_key_set"):
                    # No Odds API key at all — go straight to fallbacks
                    logger.info("ODDS_API_KEY not set — using free fallback sources")
                    use_fallback = True

                remaining = credits.get("remaining")
                if remaining is not None and remaining < 10:
                    logger.info(f"Odds API credits low ({remaining}) — switching to free sources")
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

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                await asyncio.sleep(30)

    async def _snapshot_sport_fallback(self, sport: str) -> None:
        """Take an odds snapshot using free sources (DK scraper, then OddsPapi).

        Called when Odds API credits are exhausted or unavailable.
        Cascade: DraftKings (free, unlimited) -> OddsPapi (250/month free).
        """
        new_snapshot = None

        # 1. Try DraftKings scraper first — free and unlimited
        try:
            dk_data = await scrape_dk_odds(sport)
            if not dk_data.get("error") and dk_data.get("game_count", 0) > 0:
                new_snapshot = dk_data
                logger.info(f"Fallback snapshot {sport} via DraftKings: {dk_data['game_count']} games")
        except Exception as e:
            logger.warning(f"DK scraper failed for {sport}: {e}")

        # 2. If DK failed, try OddsPapi — 250/month free
        if new_snapshot is None:
            try:
                usage = oddspapi_usage()
                if usage.get("requests_remaining", 0) > 0 and usage.get("api_key_set"):
                    op_data = await oddspapi_get_odds(sport)
                    if not op_data.get("error") and op_data.get("game_count", 0) > 0:
                        new_snapshot = op_data
                        logger.info(
                            f"Fallback snapshot {sport} via OddsPapi: {op_data['game_count']} games "
                            f"({usage['requests_remaining'] - 1} OddsPapi requests left)"
                        )
                else:
                    logger.info(f"OddsPapi unavailable for {sport}: key_set={usage.get('api_key_set')}, remaining={usage.get('requests_remaining')}")
            except Exception as e:
                logger.warning(f"OddsPapi failed for {sport}: {e}")

        # 3. If both failed, log and skip
        if new_snapshot is None:
            logger.warning(f"All fallback sources failed for {sport} — skipping snapshot")
            return

        # Process the snapshot through the standard pipeline
        await self._process_snapshot(sport, new_snapshot)

    async def _snapshot_sport(self, sport: str) -> None:
        """Take an odds snapshot for a sport and compare with previous.

        Always enriches with DK scraper data (free) to ensure:
        1. DK lines are fresh from source (our primary target book)
        2. More bookmakers in the snapshot = better devig consensus
        3. If Odds API DK data is stale, scraper overwrites it
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

            # Enrich with fresh DK scraper data (free, always)
            new_snapshot = await self._enrich_with_dk(sport, new_snapshot)

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
            logger.debug(f"DK enrichment failed for {sport} (non-critical): {e}")

        return snapshot

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

            # Detect sharp money (one book moved, others didn't)
            # Data logged for analysis but NO Telegram alerts — too noisy
            sharp_signals = detect_sharp_money(old_snapshot, new_snapshot)
            if sharp_signals:
                logger.info(f"SHARP MONEY: {sport} — {len(sharp_signals)} signals (logged, no alert)")
                for sig in sharp_signals:
                    self._alerts.append({"sport": sport, "type": "sharp_money", **sig})

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

            # Consensus implied probability = average across all bookmakers
            implied_probs = [
                calculate_implied_probability(line["price"])
                for line in all_lines
            ]
            consensus_prob = sum(implied_probs) / len(implied_probs)

            # The moved line's implied probability
            moved_implied = calculate_implied_probability(new_price)

            # If the moved line implies LOWER probability than consensus,
            # there may be value (market overreacted against this team)
            edge = consensus_prob - moved_implied

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
                        f"Kelly={ev_result['kelly_fraction']:.1%})"
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
