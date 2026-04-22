"""
Organic data collector — free APIs for game stats, player data, and context.

Feeds the embedding pipeline and prop resolution engine with real data.
All sources are free (no API key required):
  - ESPN API: scores, player stats, injuries, schedules
  - The Odds API scores endpoint (free, no credit cost)

Data flow:
  1. After games complete → collect final scores + player box scores
  2. Store in game_contexts and player_stats tables
  3. Mark as ready for embedding
  4. Resolve outstanding paper trade props with actual stats

ESPN API is undocumented but stable. Endpoints used:
  - scoreboard: live/recent scores
  - boxscore: full player stats per game
  - injuries: team injury reports
"""

import asyncio
import difflib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.data_collector")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ESPN API base URLs (public, no auth required)
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_SPORTS = {
    "basketball_nba": ("basketball", "nba"),
    "basketball_ncaab": ("basketball", "mens-college-basketball"),
    "basketball_ncaaw": ("basketball", "womens-college-basketball"),
    "americanfootball_nfl": ("football", "nfl"),
    "icehockey_nhl": ("hockey", "nhl"),
    "baseball_mlb": ("baseball", "mlb"),
    "golf_pga": ("golf", "pga"),
}

_client: Optional[httpx.AsyncClient] = None
_client_lock: Optional[asyncio.Lock] = None


def _get_client_lock() -> asyncio.Lock:
    """Lazy-init the asyncio.Lock so we don't bind to a non-existent loop at import."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client() -> httpx.AsyncClient:
    """Get-or-create the shared httpx client.

    SECURITY (audit H-10): the previous synchronous double-check could race when
    two concurrent collect_* calls hit the singleton at the same time, leaking
    a client and (under sustained load) exhausting local sockets. The init is
    now serialized by an asyncio.Lock and the function is async; callers must
    `await` it. The lock-acquisition cost is one TLS-cheap op compared to the
    network call that follows, so contention is irrelevant.
    """
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    lock = _get_client_lock()
    async with lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, max_redirects=5)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ── Static venue metadata ──
# Dimensions that ESPN doesn't provide but are critical for hypothesis testing.
# Altitude in feet, timezone offset from ET.
VENUE_METADATA = {
    # NBA
    "Ball Arena": {"dome": True, "altitude_ft": 5280, "tz_offset": -2, "city": "Denver"},
    "Vivint Arena": {"dome": True, "altitude_ft": 4226, "tz_offset": -2, "city": "Salt Lake City"},
    "Footprint Center": {"dome": True, "altitude_ft": 1086, "tz_offset": -2, "city": "Phoenix"},
    "Chase Center": {"dome": True, "altitude_ft": 10, "tz_offset": -3, "city": "San Francisco"},
    "Crypto.com Arena": {"dome": True, "altitude_ft": 300, "tz_offset": -3, "city": "Los Angeles"},
    "Intuit Dome": {"dome": True, "altitude_ft": 100, "tz_offset": -3, "city": "Inglewood"},
    "Moda Center": {"dome": True, "altitude_ft": 50, "tz_offset": -3, "city": "Portland"},
    "Climate Pledge Arena": {"dome": True, "altitude_ft": 20, "tz_offset": -3, "city": "Seattle"},
    "Target Center": {"dome": True, "altitude_ft": 830, "tz_offset": -1, "city": "Minneapolis"},
    "United Center": {"dome": True, "altitude_ft": 594, "tz_offset": -1, "city": "Chicago"},
    "Madison Square Garden": {"dome": True, "altitude_ft": 33, "tz_offset": 0, "city": "New York"},
    "TD Garden": {"dome": True, "altitude_ft": 20, "tz_offset": 0, "city": "Boston"},
    # NFL outdoor stadiums
    "Empower Field at Mile High": {"dome": False, "altitude_ft": 5280, "tz_offset": -2, "city": "Denver"},
    "Highmark Stadium": {"dome": False, "altitude_ft": 600, "tz_offset": 0, "city": "Buffalo"},
    "Lambeau Field": {"dome": False, "altitude_ft": 640, "tz_offset": -1, "city": "Green Bay"},
    "Soldier Field": {"dome": False, "altitude_ft": 594, "tz_offset": -1, "city": "Chicago"},
    "Arrowhead Stadium": {"dome": False, "altitude_ft": 800, "tz_offset": -1, "city": "Kansas City"},
    "MetLife Stadium": {"dome": False, "altitude_ft": 10, "tz_offset": 0, "city": "East Rutherford"},
    "SoFi Stadium": {"dome": True, "altitude_ft": 100, "tz_offset": -3, "city": "Inglewood"},
    "Allegiant Stadium": {"dome": True, "altitude_ft": 2001, "tz_offset": -3, "city": "Las Vegas"},
    "Mercedes-Benz Stadium": {"dome": True, "altitude_ft": 1050, "tz_offset": 0, "city": "Atlanta"},
    "AT&T Stadium": {"dome": True, "altitude_ft": 600, "tz_offset": -1, "city": "Arlington"},
    "Caesars Superdome": {"dome": True, "altitude_ft": 3, "tz_offset": -1, "city": "New Orleans"},
    "Lucas Oil Stadium": {"dome": True, "altitude_ft": 720, "tz_offset": 0, "city": "Indianapolis"},
    "U.S. Bank Stadium": {"dome": True, "altitude_ft": 830, "tz_offset": -1, "city": "Minneapolis"},
    "State Farm Stadium": {"dome": True, "altitude_ft": 1100, "tz_offset": -2, "city": "Glendale"},
    "NRG Stadium": {"dome": True, "altitude_ft": 43, "tz_offset": -1, "city": "Houston"},
    # MLB outdoor
    "Coors Field": {"dome": False, "altitude_ft": 5200, "tz_offset": -2, "city": "Denver", "park_factor": 1.35},
    "Fenway Park": {"dome": False, "altitude_ft": 20, "tz_offset": 0, "city": "Boston", "park_factor": 1.07},
    "Oracle Park": {"dome": False, "altitude_ft": 0, "tz_offset": -3, "city": "San Francisco", "park_factor": 0.83},
    "Petco Park": {"dome": False, "altitude_ft": 15, "tz_offset": -3, "city": "San Diego", "park_factor": 0.90},
    "Yankee Stadium": {"dome": False, "altitude_ft": 10, "tz_offset": 0, "city": "New York", "park_factor": 1.11},
    "Wrigley Field": {"dome": False, "altitude_ft": 600, "tz_offset": -1, "city": "Chicago", "park_factor": 1.05},
    "Great American Ball Park": {"dome": False, "altitude_ft": 480, "tz_offset": 0, "city": "Cincinnati", "park_factor": 1.13},
    "Dodger Stadium": {"dome": False, "altitude_ft": 510, "tz_offset": -3, "city": "Los Angeles", "park_factor": 0.96},
    "T-Mobile Park": {"dome": True, "altitude_ft": 2, "tz_offset": -3, "city": "Seattle", "park_factor": 0.93},
    "Tropicana Field": {"dome": True, "altitude_ft": 10, "tz_offset": 0, "city": "St. Petersburg", "park_factor": 0.90},
    "Minute Maid Park": {"dome": True, "altitude_ft": 43, "tz_offset": -1, "city": "Houston", "park_factor": 1.04},
    "Globe Life Field": {"dome": True, "altitude_ft": 540, "tz_offset": -1, "city": "Arlington", "park_factor": 0.98},
    "Chase Field": {"dome": True, "altitude_ft": 1082, "tz_offset": -2, "city": "Phoenix", "park_factor": 1.04},
    "Rogers Centre": {"dome": True, "altitude_ft": 250, "tz_offset": 0, "city": "Toronto", "park_factor": 1.00},
    "loanDepot park": {"dome": True, "altitude_ft": 5, "tz_offset": 0, "city": "Miami", "park_factor": 0.88},
    "American Family Field": {"dome": True, "altitude_ft": 635, "tz_offset": -1, "city": "Milwaukee", "park_factor": 1.05},
    # NHL arenas — all indoor (dome=True)
    # Can be extended as needed
}

# Fuzzy match threshold for venue name lookups
_VENUE_MATCH_THRESHOLD = 0.6


def _get_venue_metadata(venue_name: str, sport: str = "") -> dict:
    """Look up static venue metadata by name with fuzzy matching."""
    if not venue_name:
        return {}

    # Direct match first
    if venue_name in VENUE_METADATA:
        return {f"venue_{k}": v for k, v in VENUE_METADATA[venue_name].items()}

    # Fuzzy match
    matches = difflib.get_close_matches(
        venue_name, VENUE_METADATA.keys(), n=1, cutoff=_VENUE_MATCH_THRESHOLD
    )
    if matches:
        meta = VENUE_METADATA[matches[0]]
        return {f"venue_{k}": v for k, v in meta.items()}

    return {}


class DataCollector:
    """Collects game data from free sources for the embedding pipeline."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        # Counter for silent player-stat insert failures. Pre-fix these
        # logged at INFO (invisible in production) and the drift was
        # undetectable. Now bumped at WARNING + exposed via
        # ``get_collection_stats()`` so drift shows up in /health.
        self._player_stat_insert_failures: int = 0

    async def initialize(self) -> None:
        from tools.schema import open_db
        self._db = await open_db(self.db_path)
        logger.info("Data collector initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── ESPN SCOREBOARD ──

    async def collect_scores(
        self,
        sport: str,
        date: Optional[str] = None,
    ) -> dict:
        """
        Collect final scores from ESPN for a given date.
        Stores completed games in game_contexts table.

        Args:
            sport: Odds API sport key (e.g., 'basketball_nba')
            date: YYYYMMDD format. Defaults to today.

        Returns:
            Summary of games collected.
        """
        espn_sport = ESPN_SPORTS.get(sport)
        if not espn_sport:
            return {"error": f"Unsupported sport: {sport}", "games": 0}

        category, league = espn_sport
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y%m%d")

        client = await _get_client()
        url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
        params = {"dates": date}

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"ESPN scoreboard error: {e}")
            return {"error": str(e), "games": 0}

        events = data.get("events", [])
        games_stored = 0
        game_date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

        for event in events:
            if event.get("status", {}).get("type", {}).get("completed") is not True:
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue

            comp = competitions[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            home = away = None
            for team in competitors:
                if team.get("homeAway") == "home":
                    home = team
                elif team.get("homeAway") == "away":
                    away = team

            if not home or not away:
                continue

            home_team = home.get("team", {}).get("displayName", "")
            away_team = away.get("team", {}).get("displayName", "")
            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))
            event_id = event.get("id", "")

            # Build rich context from all available ESPN data
            venue_obj = comp.get("venue", {})
            context = {
                "home_score": home_score,
                "away_score": away_score,
                "total": home_score + away_score,
                "spread": home_score - away_score,
                "venue": venue_obj.get("fullName", ""),
                "venue_city": venue_obj.get("address", {}).get("city", ""),
                "venue_state": venue_obj.get("address", {}).get("state", ""),
                "venue_indoor": venue_obj.get("indoor", None),
                "attendance": comp.get("attendance"),
            }

            # Extract headline/notes
            notes = comp.get("notes", [])
            if notes:
                context["notes"] = [n.get("headline", "") for n in notes[:3]]

            # Extract officials/referees
            officials = comp.get("officials", [])
            if officials:
                context["officials"] = [
                    {
                        "name": off.get("displayName", off.get("fullName", "")),
                        "position": off.get("position", {}).get("displayName", ""),
                        "order": off.get("order", 0),
                    }
                    for off in officials
                ]

            # Extract broadcast info (national TV indicator)
            broadcasts = comp.get("broadcasts", [])
            if broadcasts:
                broadcast_names = []
                for bc in broadcasts:
                    for name_obj in bc.get("names", []):
                        if isinstance(name_obj, str):
                            broadcast_names.append(name_obj)
                        elif isinstance(name_obj, dict):
                            broadcast_names.append(name_obj.get("shortName", ""))
                    # Some formats have the name directly
                    if bc.get("name"):
                        broadcast_names.append(bc["name"])
                context["broadcasts"] = broadcast_names
                national_nets = {"ESPN", "ABC", "TNT", "NBC", "CBS", "FOX", "ESPN2",
                                 "FS1", "TBS", "NBCSN", "ESPNU", "ESPN+"}
                context["national_tv"] = any(
                    n.upper() in national_nets for n in broadcast_names
                )

            # Extract team records if available
            for side, team_data in [("home", home), ("away", away)]:
                records = team_data.get("records", [])
                for rec in records:
                    if rec.get("name") == "overall" or rec.get("type") == "total":
                        context[f"{side}_record"] = rec.get("summary", "")
                        break

            # Compute rest days from game_results table
            try:
                for side, team_name in [("home", home_team), ("away", away_team)]:
                    prev_cursor = await self._db.execute(
                        "SELECT game_date FROM game_results "
                        "WHERE (home_team = ? OR away_team = ?) "
                        "AND game_date < ? AND sport = ? "
                        "ORDER BY game_date DESC LIMIT 1",
                        (team_name, team_name, game_date_fmt, sport),
                    )
                    prev_row = await prev_cursor.fetchone()
                    if prev_row and prev_row[0]:
                        try:
                            prev_date = datetime.strptime(prev_row[0][:10], "%Y-%m-%d")
                            game_dt = datetime.strptime(game_date_fmt, "%Y-%m-%d")
                            rest_days = (game_dt - prev_date).days
                            context[f"{side}_rest_days"] = rest_days
                            context[f"{side}_b2b"] = rest_days <= 1
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.debug(f"Rest day computation failed: {e}")

            # Add venue metadata from static lookup
            venue_name = context.get("venue", "")
            venue_meta = _get_venue_metadata(venue_name, sport)
            if venue_meta:
                context.update(venue_meta)

            # Store game context — only overwrite if the new context is richer
            # (more keys) than the existing one. Prevents sparse re-collections
            # from regressing enriched data with officials/rest/broadcasts.
            try:
                from tools.db_utils import execute_with_retry
                context_json = json.dumps(context)
                await execute_with_retry(
                    self._db,
                    "INSERT INTO game_contexts "
                    "(sport, event_id, game_date, home_team, away_team, "
                    "home_score, away_score, context_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(sport, event_id) DO UPDATE SET "
                    "context_json = CASE "
                    "  WHEN length(excluded.context_json) >= length(context_json) "
                    "    THEN excluded.context_json "
                    "  ELSE context_json "
                    "END, "
                    "home_score = COALESCE(excluded.home_score, home_score), "
                    "away_score = COALESCE(excluded.away_score, away_score)",
                    (
                        sport, event_id, game_date_fmt,
                        home_team, away_team,
                        home_score, away_score,
                        context_json,
                    ),
                    operation="data_collector store_game",
                )
                games_stored += 1
                # Publish game completed event
                try:
                    from tools.event_bus import get_event_bus, EVENT_GAME_COMPLETED
                    await get_event_bus().publish(EVENT_GAME_COMPLETED, {
                        "sport": sport, "event_id": event_id,
                        "game_date": game_date_fmt,
                        "home_team": home_team, "away_team": away_team,
                        "home_score": home_score, "away_score": away_score,
                    })
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"Failed to store game {event_id}: {e}")

            # Also store in game_results for backtest resolution
            try:
                total_score = home_score + away_score
                spread_result = float(away_score - home_score)
                winner = (
                    home_team if home_score > away_score
                    else away_team if away_score > home_score
                    else "push"
                )
                from tools.db_utils import execute_with_retry
                await execute_with_retry(
                    self._db,
                    "INSERT OR IGNORE INTO game_results "
                    "(sport, game_date, home_team, away_team, home_score, "
                    "away_score, total_score, spread_result, winner, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'espn')",
                    (
                        sport, game_date_fmt, home_team, away_team,
                        home_score, away_score, total_score, spread_result, winner,
                    ),
                    operation="data_collector store_game_result",
                )
            except Exception as e:
                logger.warning(f"Failed to store game_result {event_id}: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_scores")
        logger.info(f"Collected {games_stored} games for {sport} on {date}")

        return {
            "sport": sport,
            "date": game_date_fmt,
            "total_events": len(events),
            "completed": games_stored,
        }

    # ── ESPN BOX SCORES ──

    async def collect_box_scores(
        self,
        sport: str,
        date: Optional[str] = None,
    ) -> dict:
        """
        Collect player box scores from ESPN for completed games.
        Stores individual player stats in player_stats table.

        This is critical for:
          1. Resolving paper trade prop outcomes
          2. Building the embedding corpus for pattern discovery
        """
        espn_sport = ESPN_SPORTS.get(sport)
        if not espn_sport:
            return {"error": f"Unsupported sport: {sport}", "players": 0}

        category, league = espn_sport
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y%m%d")

        # First get event IDs from scoreboard
        client = await _get_client()
        url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
        params = {"dates": date}

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            scoreboard = resp.json()
        except Exception as e:
            logger.warning(f"ESPN scoreboard fetch failed for {category}/{league} date={date}: {e}")
            return {"error": str(e), "players": 0}

        game_date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        total_players = 0
        events = scoreboard.get("events", [])

        for event in events:
            if event.get("status", {}).get("type", {}).get("completed") is not True:
                continue

            event_id = event.get("id", "")
            # Fetch box score
            box_url = (
                f"https://site.api.espn.com/apis/site/v2/sports/"
                f"{category}/{league}/summary"
            )

            try:
                box_resp = await client.get(box_url, params={"event": event_id})
                box_resp.raise_for_status()
                box_data = box_resp.json()
            except Exception as e:
                logger.warning(f"Box score fetch failed for {event_id}: {e}")
                continue

            # Extract player stats from boxscore
            boxscore = box_data.get("boxscore", {})
            players_data = boxscore.get("players", [])

            # Track data quality — too many dropped athletes signals partial response
            dropped_athletes = 0
            seen_athletes = 0

            for team_data in players_data:
                team_name = team_data.get("team", {}).get("displayName", "")
                statistics = team_data.get("statistics", [])

                for stat_group in statistics:
                    stat_labels = stat_group.get("labels", [])
                    athletes = stat_group.get("athletes", [])

                    for athlete in athletes:
                        seen_athletes += 1
                        player_name = athlete.get("athlete", {}).get("displayName", "")
                        stats = athlete.get("stats", [])

                        if not player_name or not stats:
                            dropped_athletes += 1
                            continue

                        # Map label→value
                        stat_map = dict(zip(stat_labels, stats))
                        stored = await self._store_player_stats(
                            sport=sport,
                            event_id=event_id,
                            game_date=game_date_fmt,
                            player_name=player_name,
                            team=team_name,
                            stat_map=stat_map,
                            category=category,
                        )
                        total_players += stored

            # Warn if box score response was incomplete
            if seen_athletes > 0 and dropped_athletes / seen_athletes > 0.2:
                logger.warning(
                    f"Box score {sport} {event_id}: dropped {dropped_athletes}/{seen_athletes} "
                    f"athletes (>20%) — ESPN response may be incomplete"
                )

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_box_scores")
        logger.info(
            f"Collected stats for {total_players} player-stat entries "
            f"for {sport} on {date}"
        )

        return {
            "sport": sport,
            "date": game_date_fmt,
            "games_processed": len([
                e for e in events
                if e.get("status", {}).get("type", {}).get("completed")
            ]),
            "player_stat_entries": total_players,
        }

    async def _store_player_stats(
        self,
        sport: str,
        event_id: str,
        game_date: str,
        player_name: str,
        team: str,
        stat_map: dict,
        category: str,
    ) -> int:
        """Store individual player stats. Returns count of entries stored."""
        count = 0

        # Basketball stat mapping
        if category == "basketball":
            mappings = {
                "PTS": "points",
                "REB": "rebounds",
                "AST": "assists",
                "3PM": "threes",
                "STL": "steals",
                "BLK": "blocks",
                "TO": "turnovers",
                "MIN": "minutes",
            }
            minutes = None
            min_str = stat_map.get("MIN", "0")
            if ":" in str(min_str):
                parts = str(min_str).split(":")
                try:
                    minutes = int(parts[0]) + int(parts[1]) / 60
                except (ValueError, IndexError):
                    pass
            else:
                try:
                    minutes = float(min_str)
                except (ValueError, TypeError):
                    pass

            for espn_key, stat_type in mappings.items():
                if espn_key in stat_map and stat_type != "minutes":
                    try:
                        val = float(stat_map[espn_key])
                    except (ValueError, TypeError):
                        continue

                    try:
                        await self._db.execute(
                            "INSERT OR IGNORE INTO player_stats "
                            "(sport, event_id, game_date, player_name, team, "
                            "stat_type, stat_value, minutes_played) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                sport, event_id, game_date,
                                player_name, team, stat_type,
                                val, minutes,
                            ),
                        )
                        count += 1
                    except Exception as e:
                        # WARNING (not INFO) + counter so drift is visible.
                        self._player_stat_insert_failures += 1
                        logger.warning(
                            f"Player stat insert failed for "
                            f"{player_name}/{stat_type}: {e!r} "
                            f"(total insert failures: "
                            f"{self._player_stat_insert_failures})"
                        )

            # Composite: PRA
            pts = float(stat_map.get("PTS", 0) or 0)
            reb = float(stat_map.get("REB", 0) or 0)
            ast = float(stat_map.get("AST", 0) or 0)
            pra = pts + reb + ast
            if pra > 0:
                try:
                    await self._db.execute(
                        "INSERT OR IGNORE INTO player_stats "
                        "(sport, event_id, game_date, player_name, team, "
                        "stat_type, stat_value, minutes_played) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sport, event_id, game_date,
                            player_name, team,
                            "points_rebounds_assists", pra, minutes,
                        ),
                    )
                    count += 1
                except Exception as e:
                    self._player_stat_insert_failures += 1
                    logger.warning(
                        f"PRA composite insert failed for {player_name}: "
                        f"{e!r} (total insert failures: "
                        f"{self._player_stat_insert_failures})"
                    )

        # Football stat mapping
        elif category == "football":
            # Football has nested stat categories — handle common ones
            for key in ["passingYards", "rushingYards", "receivingYards",
                        "passingTouchdowns", "rushingTouchdowns", "receptions"]:
                if key in stat_map:
                    try:
                        val = float(stat_map[key])
                    except (ValueError, TypeError):
                        continue
                    try:
                        await self._db.execute(
                            "INSERT OR IGNORE INTO player_stats "
                            "(sport, event_id, game_date, player_name, team, "
                            "stat_type, stat_value) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (sport, event_id, game_date, player_name, team, key, val),
                        )
                        count += 1
                    except Exception as e:
                        self._player_stat_insert_failures += 1
                        logger.warning(
                            f"Football stat insert failed for "
                            f"{player_name}/{key}: {e!r} "
                            f"(total insert failures: "
                            f"{self._player_stat_insert_failures})"
                        )

        return count

    # ── ESPN ODDS (hidden core API, free, no auth) ──

    ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports"
    # Core API uses slightly different league keys than the site API
    ESPN_CORE_LEAGUES = {
        "basketball_nba": ("basketball", "nba"),
        "basketball_ncaab": ("basketball", "mens-college-basketball"),
        "americanfootball_nfl": ("football", "nfl"),
        "icehockey_nhl": ("hockey", "nhl"),
        "baseball_mlb": ("baseball", "mlb"),
        "golf_pga": ("golf", "pga"),
    }

    async def collect_espn_odds(
        self,
        sport: str,
        event_ids: list[str] = None,
    ) -> list[dict]:
        """
        Fetch odds data from ESPN's hidden core API.

        Supplementary free data — errors log and return empty, never crash.

        Args:
            sport: Odds API sport key (e.g., 'basketball_nba')
            event_ids: Specific ESPN event IDs. If None, pulls today's scoreboard.

        Returns:
            List of dicts with event_id, teams, odds lines, and win probabilities.
        """
        core_sport = self.ESPN_CORE_LEAGUES.get(sport)
        if not core_sport:
            logger.warning(f"collect_espn_odds: unsupported sport {sport}")
            return []

        core_category, core_league = core_sport
        client = await _get_client()

        # If no event IDs supplied, get them from today's scoreboard
        if not event_ids:
            event_ids = await self._get_today_event_ids(sport)
            if not event_ids:
                return []

        results = []
        for eid in event_ids:
            try:
                entry = await self._fetch_event_odds(
                    client, core_category, core_league, eid,
                )
                if entry:
                    results.append(entry)
            except Exception as e:
                logger.warning(f"ESPN odds fetch failed for event {eid}: {e}")
                continue

        logger.info(
            f"Collected ESPN odds for {len(results)}/{len(event_ids)} "
            f"events ({sport})"
        )
        return results

    async def _get_today_event_ids(self, sport: str) -> list[str]:
        """Pull today's event IDs from the ESPN site scoreboard."""
        espn_sport = ESPN_SPORTS.get(sport)
        if not espn_sport:
            return []
        category, league = espn_sport
        client = await _get_client()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
        try:
            resp = await client.get(url, params={"dates": today})
            resp.raise_for_status()
            events = resp.json().get("events", [])
            return [e["id"] for e in events if "id" in e]
        except Exception as e:
            logger.warning(f"ESPN scoreboard fetch for event IDs failed: {e}")
            return []

    async def _fetch_event_odds(
        self,
        client: httpx.AsyncClient,
        core_category: str,
        core_league: str,
        event_id: str,
    ) -> Optional[dict]:
        """Fetch odds and win probabilities for a single ESPN event."""
        base = (
            f"{self.ESPN_CORE_BASE}/{core_category}/leagues/{core_league}"
            f"/events/{event_id}/competitions/{event_id}"
        )

        # ── Odds (spreads, totals, moneylines from multiple books) ──
        odds_data = []
        try:
            resp = await client.get(f"{base}/odds", params={"limit": 50})
            resp.raise_for_status()
            raw_odds = resp.json()
            for item in raw_odds.get("items", []):
                provider = item.get("provider", {}).get("name", "unknown")
                odds_data.append({
                    "provider": provider,
                    "provider_id": item.get("provider", {}).get("id"),
                    "spread": item.get("spread"),
                    "over_under": item.get("overUnder"),
                    "home_ml": item.get("homeTeamOdds", {}).get("moneyLine"),
                    "away_ml": item.get("awayTeamOdds", {}).get("moneyLine"),
                    "home_spread_odds": item.get("homeTeamOdds", {}).get("spreadOdds"),
                    "away_spread_odds": item.get("awayTeamOdds", {}).get("spreadOdds"),
                    "over_odds": item.get("overOdds"),
                    "under_odds": item.get("underOdds"),
                    "details": item.get("details"),
                })
        except Exception as e:
            logger.warning(f"ESPN odds endpoint failed for {event_id}: {e}")

        # ── Win probabilities (live or pregame) ──
        probabilities = []
        try:
            resp = await client.get(
                f"{base}/probabilities", params={"limit": 200},
            )
            resp.raise_for_status()
            raw_probs = resp.json()
            for item in raw_probs.get("items", []):
                probabilities.append({
                    "home_win_pct": item.get("homeWinPercentage"),
                    "away_win_pct": item.get("awayWinPercentage"),
                    "tie_pct": item.get("tiePercentage"),
                    "sequence": item.get("sequenceNumber"),
                })
        except Exception as e:
            logger.warning(f"ESPN probabilities endpoint failed for {event_id}: {e}")

        if not odds_data and not probabilities:
            return None

        return {
            "event_id": event_id,
            "odds": odds_data,
            "probabilities": probabilities,
        }

    # ── BATCH COLLECTION ──

    async def collect_date_range(
        self,
        sport: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """
        Collect scores and box scores for a date range.
        Format: YYYY-MM-DD for both dates.
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        total_games = 0
        total_players = 0
        dates_processed = 0

        current = start
        while current <= end:
            date_str = current.strftime("%Y%m%d")
            current += timedelta(days=1)

            scores = await self.collect_scores(sport, date_str)
            total_games += scores.get("completed", 0)

            box = await self.collect_box_scores(sport, date_str)
            total_players += box.get("player_stat_entries", 0)

            dates_processed += 1

        return {
            "sport": sport,
            "dates_processed": dates_processed,
            "total_games": total_games,
            "total_player_entries": total_players,
        }

    # ── PROP RESOLUTION ──

    async def resolve_prop_outcomes(
        self,
        sport: str,
        game_date: str,
    ) -> dict:
        """
        Resolve paper trades using collected player stats.

        Matches paper_trades entries with player_stats to determine
        if props hit (Over/Under).
        """
        # Get unresolved paper trades for this date
        cursor = await self._db.execute(
            "SELECT trade_id, player, market, line, side "
            "FROM paper_trades "
            "WHERE sport = ? AND game_date = ? AND actual_result IS NULL",
            (sport, game_date),
        )
        trades = await cursor.fetchall()

        resolved = 0
        for trade_id, player, market, line, side in trades:
            # Map market to stat_type
            stat_type = market.replace("player_", "")

            # Find matching stat — try exact match first
            stat_cursor = await self._db.execute(
                "SELECT stat_value FROM player_stats "
                "WHERE sport = ? AND game_date = ? "
                "AND player_name = ? AND stat_type = ?",
                (sport, game_date, player, stat_type),
            )
            stat_row = await stat_cursor.fetchone()

            # Fuzzy player name matching if exact match fails
            if not stat_row and player:
                fuzzy_cursor = await self._db.execute(
                    "SELECT DISTINCT player_name FROM player_stats "
                    "WHERE sport = ? AND game_date = ? AND stat_type = ?",
                    (sport, game_date, stat_type),
                )
                candidates = [r[0] for r in await fuzzy_cursor.fetchall()]
                best_match = None
                best_ratio = 0.0
                for candidate in candidates:
                    # Case-insensitive exact match
                    if candidate.lower() == player.lower():
                        best_match = candidate
                        best_ratio = 1.0
                        break
                    ratio = difflib.SequenceMatcher(
                        None, player.lower(), candidate.lower()
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = candidate
                if best_match and best_ratio >= 0.85:
                    stat_cursor = await self._db.execute(
                        "SELECT stat_value FROM player_stats "
                        "WHERE sport = ? AND game_date = ? "
                        "AND player_name = ? AND stat_type = ?",
                        (sport, game_date, best_match, stat_type),
                    )
                    stat_row = await stat_cursor.fetchone()
                    if stat_row:
                        logger.info(
                            f"Fuzzy matched player '{player}' -> '{best_match}' "
                            f"(ratio={best_ratio:.2f})"
                        )

            if not stat_row or line is None:
                continue

            actual_stat = stat_row[0]

            # Determine result
            if side == "Over":
                result = "won" if actual_stat > line else "lost" if actual_stat < line else "push"
            elif side == "Under":
                result = "won" if actual_stat < line else "lost" if actual_stat > line else "push"
            else:
                continue

            await self._db.execute(
                "UPDATE paper_trades SET actual_result = ?, actual_stat = ? "
                "WHERE trade_id = ?",
                (result, actual_stat, trade_id),
            )
            resolved += 1

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector resolve_paper_trades")

        clv_written = 0
        try:
            from tools.clv_tracker import CLVTracker
            _clv = CLVTracker(self.db_path)
            _clv._db = self._db
            clv_written = await _clv.sync_paper_trades_to_clv_log()
        except Exception as e:
            logger.warning(f"clv_log player-prop sync failed ({sport} {game_date}): {e}")

        logger.info(
            f"Resolved {resolved}/{len(trades)} paper trades for {sport} "
            f"on {game_date} (clv_log +{clv_written})"
        )

        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": len(trades),
            "resolved": resolved,
            "clv_log_written": clv_written,
        }

    # ── GAME-LEVEL RESOLUTION ──

    GAME_LEVEL_MARKETS = (
        'spreads', 'totals', 'h2h', 'totals_f5', 'totals_first_5',
        'first_five_totals', 'total', 'total_first5', 'spread', 'moneyline',
    )

    @staticmethod
    def _fuzzy_team_match(
        name: str, candidates: list[str], threshold: float = 0.8,
    ) -> Optional[str]:
        """
        Match a team name against candidates using progressively looser
        strategies: exact -> case-insensitive -> fuzzy (SequenceMatcher).
        Returns the best match or None if nothing exceeds *threshold*.
        """
        if not name:
            return None
        for c in candidates:
            if c == name:
                return c
        name_lower = name.lower()
        for c in candidates:
            if c.lower() == name_lower:
                return c
        best_match = None
        best_ratio = 0.0
        for c in candidates:
            ratio = difflib.SequenceMatcher(
                None, name_lower, c.lower(),
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = c
        if best_match and best_ratio >= threshold:
            return best_match
        return None

    async def resolve_game_level_outcomes(
        self,
        sport: str,
        game_date: str,
    ) -> dict:
        """
        Resolve paper trades for game-level markets (spreads, totals, h2h/moneyline).

        Uses the game_results table to determine outcomes for paper trades
        that are NOT player props.

        Args:
            sport: Odds API sport key (e.g. 'basketball_nba')
            game_date: YYYY-MM-DD format

        Returns:
            Summary dict with counts of pending, resolved, and unmatched trades.
        """
        # Build a comma-separated placeholder list for the IN clause
        placeholders = ",".join("?" for _ in self.GAME_LEVEL_MARKETS)

        # Fetch unresolved game-level paper trades (include home_team/away_team for matching)
        cursor = await self._db.execute(
            f"SELECT trade_id, event_id, market, line, side, home_team, away_team "
            f"FROM paper_trades "
            f"WHERE sport = ? AND game_date = ? AND actual_result IS NULL "
            f"AND market IN ({placeholders})",
            (sport, game_date, *self.GAME_LEVEL_MARKETS),
        )
        trades = await cursor.fetchall()

        if not trades:
            return {
                "sport": sport,
                "game_date": game_date,
                "total_pending": 0,
                "resolved": 0,
                "unmatched": 0,
            }

        # Fetch all game results for this sport + date
        gr_cursor = await self._db.execute(
            "SELECT home_team, away_team, home_score, away_score, "
            "total_score, spread_result, winner "
            "FROM game_results WHERE sport = ? AND game_date = ?",
            (sport, game_date),
        )
        game_rows = await gr_cursor.fetchall()

        # Build lookup structures
        games = []
        all_team_names = []
        for home, away, h_score, a_score, total, spread_res, winner in game_rows:
            games.append({
                "home_team": home,
                "away_team": away,
                "home_score": h_score,
                "away_score": a_score,
                "total_score": total,
                "spread_result": spread_res,
                "winner": winner,
            })
            all_team_names.extend([home, away])

        resolved = 0
        unmatched = 0

        for trade_row in trades:
            trade_id, event_id, market, line, side = trade_row[:5]
            pt_home = trade_row[5] if len(trade_row) > 5 else None
            pt_away = trade_row[6] if len(trade_row) > 6 else None
            game = None

            # Strategy 1: match by event_id if paper trade has one
            if event_id:
                eid_cursor = await self._db.execute(
                    "SELECT gr.home_team, gr.away_team, gr.home_score, gr.away_score, "
                    "gr.total_score, gr.spread_result, gr.winner "
                    "FROM game_results gr "
                    "JOIN game_contexts gc ON gr.sport = gc.sport "
                    "  AND gr.game_date = gc.game_date "
                    "  AND gr.home_team = gc.home_team "
                    "  AND gr.away_team = gc.away_team "
                    "WHERE gc.event_id = ? AND gr.sport = ? AND gr.game_date = ?",
                    (event_id, sport, game_date),
                )
                eid_row = await eid_cursor.fetchone()
                if eid_row:
                    game = {
                        "home_team": eid_row[0],
                        "away_team": eid_row[1],
                        "home_score": eid_row[2],
                        "away_score": eid_row[3],
                        "total_score": eid_row[4],
                        "spread_result": eid_row[5],
                        "winner": eid_row[6],
                    }

            # Strategy 2: match by team name from the side field
            if not game and side and games:
                matched_team = self._fuzzy_team_match(side, all_team_names)
                if matched_team:
                    for g in games:
                        if matched_team in (g["home_team"], g["away_team"]):
                            game = g
                            break

            # Strategy 3: match by stored home_team/away_team (critical for totals
            # where side="Over"/"Under" and can't team-match via Strategy 2)
            if not game and games and (pt_home or pt_away):
                match_name = pt_home or pt_away
                matched_team = self._fuzzy_team_match(match_name, all_team_names)
                if matched_team:
                    for g in games:
                        if matched_team in (g["home_team"], g["away_team"]):
                            game = g
                            break

            if not game:
                unmatched += 1
                continue

            result = None

            # ── h2h / moneyline ──
            if market in ('h2h', 'moneyline'):
                winner = game["winner"]
                if winner == "push":
                    result = "push"
                else:
                    matched = self._fuzzy_team_match(side, [game["home_team"], game["away_team"]])
                    if matched:
                        winner_matched = self._fuzzy_team_match(
                            winner, [game["home_team"], game["away_team"]],
                        )
                        result = "won" if matched == winner_matched else "lost"

            # ── spreads ──
            elif market in ('spreads', 'spread'):
                if line is not None:
                    # Determine if the side is the home or away team
                    matched = self._fuzzy_team_match(side, [game["home_team"], game["away_team"]])
                    if matched:
                        if matched == game["home_team"]:
                            # Home team: margin = home_score - away_score
                            margin = game["home_score"] - game["away_score"]
                        else:
                            # Away team: margin = away_score - home_score
                            margin = game["away_score"] - game["home_score"]
                        # The team covers if margin + line > 0
                        adjusted = margin + line
                        if adjusted > 0:
                            result = "won"
                        elif adjusted < 0:
                            result = "lost"
                        else:
                            result = "push"

            # ── totals ──
            elif market in ('totals', 'total', 'totals_f5', 'totals_first_5',
                            'first_five_totals', 'total_first5'):
                if line is not None and game["total_score"] is not None:
                    total = game["total_score"]
                    side_lower = (side or "").lower().strip()
                    if side_lower == "over":
                        if total > line:
                            result = "won"
                        elif total < line:
                            result = "lost"
                        else:
                            result = "push"
                    elif side_lower == "under":
                        if total < line:
                            result = "won"
                        elif total > line:
                            result = "lost"
                        else:
                            result = "push"

            if result is None:
                unmatched += 1
                continue

            await self._db.execute(
                "UPDATE paper_trades SET actual_result = ? "
                "WHERE trade_id = ?",
                (result, trade_id),
            )
            resolved += 1

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector resolve_game_paper_trades")

        # Backfill closing odds from closing_lines table for trades missing them
        cl_backfilled = 0
        backfill_cursor = await self._db.execute(
            "SELECT trade_id, event_id, market, side, signal_implied_prob "
            "FROM paper_trades "
            "WHERE sport = ? AND game_date = ? AND closing_odds IS NULL",
            (sport, game_date),
        )
        backfill_trades = await backfill_cursor.fetchall()

        for bt in backfill_trades:
            bt_id, bt_event, bt_market, bt_side, bt_signal_imp = bt
            cl_cursor = await self._db.execute(
                "SELECT closing_odds, closing_implied FROM closing_lines "
                "WHERE event_id = ? AND market = ? AND team = ? "
                "ORDER BY CASE WHEN source = 'Pinnacle' THEN 0 "
                "WHEN source = 'LowVig.ag' THEN 1 ELSE 2 END, "
                "captured_at DESC LIMIT 1",
                (bt_event, bt_market, bt_side),
            )
            cl_row = await cl_cursor.fetchone()

            if not cl_row:
                cl_row = await self._closing_from_snapshot(
                    sport, game_date, bt_event, bt_market, bt_side
                )

            if cl_row:
                cl_odds, cl_implied = cl_row
                clv = None
                if bt_signal_imp is not None and cl_implied is not None:
                    clv = round(cl_implied - bt_signal_imp, 4)
                await self._db.execute(
                    "UPDATE paper_trades SET closing_odds = ?, "
                    "closing_implied = ?, clv_implied = ? "
                    "WHERE trade_id = ?",
                    (cl_odds, cl_implied, clv, bt_id),
                )
                cl_backfilled += 1

        if cl_backfilled > 0:
            await commit_with_retry(
                self._db,
                operation="data_collector backfill_closing_odds",
            )
            logger.info(
                f"Backfilled closing odds for {cl_backfilled} paper trades "
                f"({sport} {game_date})"
            )

        # Promote every freshly-resolved paper trade into clv_log — this is
        # the permanent signal-quality ledger. Without this call, paper_trade
        # wins/losses never reach the CLV analysis surface. Idempotent: the
        # sync method only touches rows missing a matching clv_log entry.
        clv_written = 0
        try:
            from tools.clv_tracker import CLVTracker
            _clv = CLVTracker(self.db_path)
            _clv._db = self._db  # reuse the caller's connection for the same tx
            clv_written = await _clv.sync_paper_trades_to_clv_log()
        except Exception as e:
            logger.warning(f"clv_log paper-trade sync failed ({sport} {game_date}): {e}")

        logger.info(
            f"Resolved {resolved}/{len(trades)} game-level paper trades "
            f"for {sport} on {game_date} ({unmatched} unmatched, "
            f"clv_log +{clv_written})"
        )

        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": len(trades),
            "resolved": resolved,
            "unmatched": unmatched,
            "clv_log_written": clv_written,
        }

    async def _closing_from_snapshot(
        self, sport: str, game_date: str, event_id: str, market: str, side: str
    ):
        """Extract closing odds from the last odds snapshot containing this game."""
        import json

        try:
            cursor = await self._db.execute(
                "SELECT snapshot_json FROM odds_snapshots "
                "WHERE sport = ? AND timestamp LIKE ? "
                "ORDER BY timestamp DESC LIMIT 10",
                (sport, f"{game_date}%"),
            )
            rows = await cursor.fetchall()

            # Canonicalize the sharp-books allowlist so "Betfair Exchange"
            # (odds-api.com title casing with space) and "betfair_exchange"
            # (odds-api.io key) both resolve. Before this fix, the literal
            # space-form vs underscore-form meant odds-api.io snapshots
            # never matched and the function silently returned soft-book
            # closes — making close_reliable=False for most paper trades.
            from tools.book_keys import canonicalize_book, canonicalize_book_set
            sharp_books = canonicalize_book_set(
                {"pinnacle", "lowvig.ag", "betfair_exchange", "circa", "sharp"}
            )
            for row in rows:
                try:
                    data = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    continue

                for game in data.get("games", []):
                    if game.get("id") != event_id:
                        continue

                    best_odds = None
                    best_implied = None
                    is_sharp = False

                    for bm in game.get("bookmakers", []):
                        book = canonicalize_book(bm.get("title") or bm.get("key") or "")
                        for mkt in bm.get("markets", []):
                            if mkt.get("key") != market:
                                continue
                            for outcome in mkt.get("outcomes", []):
                                if outcome.get("name") != side:
                                    continue
                                price = outcome.get("price")
                                if price is None:
                                    continue
                                price = int(price)
                                imp = 1 / (1 + 100 / abs(price)) if price > 0 else abs(price) / (abs(price) + 100)
                                if book in sharp_books:
                                    return (price, round(imp, 4))
                                if best_odds is None or not is_sharp:
                                    best_odds = price
                                    best_implied = round(imp, 4)

                    if best_odds is not None:
                        return (best_odds, best_implied)

        except Exception as e:
            logger.debug(f"Snapshot closing line lookup failed: {e}")

        return None

    # ── ESPN PLAY-BY-PLAY ──

    async def collect_play_by_play(
        self,
        sport: str,
        date: Optional[str] = None,
    ) -> dict:
        """
        Collect play-by-play and win probability data from ESPN summary endpoint.

        Dense data: ~400-500 plays per NBA game with coordinates, scoring runs,
        momentum shifts, pace metrics, and real-time win probabilities.

        Stores in game_contexts.context_json under 'play_by_play' and 'win_probability' keys.
        """
        espn_sport = ESPN_SPORTS.get(sport)
        if not espn_sport:
            return {"error": f"Unsupported sport: {sport}", "games": 0}

        category, league = espn_sport
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y%m%d")

        client = await _get_client()
        url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
        params = {"dates": date}

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            scoreboard = resp.json()
        except Exception as e:
            logger.error(f"ESPN scoreboard error for PBP: {e}")
            return {"error": str(e), "games": 0}

        game_date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        games_enriched = 0
        events = scoreboard.get("events", [])

        for event in events:
            if event.get("status", {}).get("type", {}).get("completed") is not True:
                continue

            event_id = event.get("id", "")
            summary_url = (
                f"https://site.api.espn.com/apis/site/v2/sports/"
                f"{category}/{league}/summary"
            )

            try:
                summary_resp = await client.get(summary_url, params={"event": event_id})
                summary_resp.raise_for_status()
                summary = summary_resp.json()
            except Exception as e:
                logger.warning(f"PBP fetch failed for {event_id}: {e}")
                continue

            plays = summary.get("plays", [])
            win_probs = summary.get("winprobability", [])

            if not plays:
                continue

            # Extract key PBP metrics
            scoring_plays = [p for p in plays if p.get("scoringPlay")]
            total_plays = len(plays)

            # Compute pace metrics per period
            periods = {}
            for play in plays:
                period_num = play.get("period", {}).get("number", 0)
                if period_num not in periods:
                    periods[period_num] = {"plays": 0, "scoring_plays": 0}
                periods[period_num]["plays"] += 1
                if play.get("scoringPlay"):
                    periods[period_num]["scoring_plays"] += 1

            # Win probability momentum: biggest swings
            momentum_swings = []
            if len(win_probs) >= 2:
                for i in range(1, len(win_probs)):
                    prev_wp = win_probs[i - 1].get("homeWinPercentage", 0.5)
                    curr_wp = win_probs[i].get("homeWinPercentage", 0.5)
                    swing = abs(curr_wp - prev_wp)
                    if swing >= 0.05:  # 5%+ swing = significant momentum shift
                        momentum_swings.append({
                            "play_id": win_probs[i].get("playId"),
                            "swing": round(swing, 3),
                            "direction": "home" if curr_wp > prev_wp else "away",
                            "wp_after": round(curr_wp, 3),
                        })

            # 2026-04-18: Prior version stored ONLY a 7-stat summary
            # (total_plays, scoring_plays, periods, momentum_swings, and
            # final/max/min win-prob). The raw `plays` and `winprobability`
            # arrays returned by ESPN were parsed and then thrown away,
            # meaning Callisto was not actually storing play-by-play — it
            # was storing a histogram. Downstream pace / scoring-run /
            # player-impact / in-game modelling had no timeline to read.
            #
            # This version stores a compact form of every play plus the
            # full win-prob series, while keeping the prior summary fields
            # for backward compatibility with any consumer reading them.
            def _parse_clock(play_obj):
                c = play_obj.get("clock", {}) or {}
                val = c.get("displayValue") or c.get("value")
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, str) and ":" in val:
                    try:
                        mm, ss = val.split(":")
                        return int(mm) * 60 + int(ss)
                    except ValueError:
                        return 0
                return 0

            wp_by_play_id = {
                wp.get("playId"): wp.get("homeWinPercentage")
                for wp in win_probs
                if wp.get("playId")
            }

            compact_plays = []
            for p in plays:
                st = p.get("scoringPlay") is True
                coord = p.get("coordinate") or {}
                pid = p.get("id")
                compact_plays.append({
                    "p": p.get("period", {}).get("number", 0),
                    "c": _parse_clock(p),
                    "t": (p.get("type", {}) or {}).get("text") or p.get("shortText", ""),
                    "hs": p.get("homeScore"),
                    "as": p.get("awayScore"),
                    "sc": p.get("homeAway") if st else None,
                    "wp": (
                        round(float(wp_by_play_id[pid]), 4)
                        if pid in wp_by_play_id and wp_by_play_id[pid] is not None
                        else None
                    ),
                    "x": coord.get("x") if coord else None,
                    "y": coord.get("y") if coord else None,
                    "tx": (p.get("text") or "")[:200],  # cap description length
                })

            pbp_payload = {
                "plays": compact_plays,
                "wp_series": [
                    round(float(wp.get("homeWinPercentage")), 4)
                    for wp in win_probs
                    if wp.get("homeWinPercentage") is not None
                ],
                "total_plays": total_plays,
                "scoring_plays": len(scoring_plays),
                "periods": periods,
                "momentum_swings": sorted(
                    momentum_swings, key=lambda x: x["swing"], reverse=True
                )[:10],
                "final_home_wp": round(
                    win_probs[-1].get("homeWinPercentage", 0.5), 3
                ) if win_probs else None,
                "max_home_wp": round(
                    max(wp.get("homeWinPercentage", 0.5) for wp in win_probs), 3
                ) if win_probs else None,
                "min_home_wp": round(
                    min(wp.get("homeWinPercentage", 0.5) for wp in win_probs), 3
                ) if win_probs else None,
            }

            # Update existing game_context with PBP data. Also mirror the final
            # home win-probability into a top-level `win_probability` key so
            # queries that filter on it (e.g. "rows with win_probability set")
            # find these games — the prior version only nested it inside
            # play_by_play, which left the top-level filter returning 0 matches.
            cursor = await self._db.execute(
                "SELECT id, context_json FROM game_contexts "
                "WHERE sport = ? AND event_id = ?",
                (sport, event_id),
            )
            row = await cursor.fetchone()
            if row:
                ctx_id, ctx_json = row
                ctx = json.loads(ctx_json) if ctx_json else {}
                ctx["play_by_play"] = pbp_payload
                if pbp_payload["final_home_wp"] is not None:
                    ctx["win_probability"] = pbp_payload["final_home_wp"]
                await self._db.execute(
                    "UPDATE game_contexts SET context_json = ? WHERE id = ?",
                    (json.dumps(ctx), ctx_id),
                )
                games_enriched += 1
                logger.debug(
                    f"PBP enrichment: {event_id} — {total_plays} plays stored, "
                    f"{len(compact_plays)} timeline entries, "
                    f"{len(momentum_swings)} momentum swings"
                )

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_play_by_play")
        logger.info(
            f"Play-by-play: enriched {games_enriched} {sport} games on {game_date_fmt}"
        )
        return {
            "sport": sport,
            "date": game_date_fmt,
            "games_enriched": games_enriched,
        }

    # ── BASEBALL SAVANT / STATCAST ──

    async def collect_statcast(
        self,
        start_date: str,
        end_date: Optional[str] = None,
        player_type: str = "pitcher",
    ) -> dict:
        """
        Collect pitch-level Statcast data from Baseball Savant (free, no key).

        Stores ONE ROW PER PITCH in the `statcast_pitches` table, keeping the
        40 highest-signal fields from the 118-column savant CSV:
          - physics: pitch_type, release_speed, release_spin_rate,
            release_extension, release_pos_{x,y,z}, spin_axis, pfx_{x,z}
          - location: plate_{x,z}, zone, sz_top, sz_bot
          - batted ball: launch_speed, launch_angle, hit_distance_sc, bb_type, hc_{x,y}
          - outcome: type, description, events, balls, strikes
          - game state: on_{1b,2b,3b}, outs_when_up, post_{home,away}_score
          - expected: estimated_ba_using_speedangle, estimated_woba_using_speedangle,
            woba_value, woba_denom
          - participants: pitcher_id, pitcher_name, pitcher_throws,
            batter_id, batter_name, batter_stands

        Also updates a rolling per-pitcher-game aggregate in `player_stats`
        (avg_velocity, avg_exit_velocity, pitches, strikeouts) so legacy
        consumers keep working.

        Args:
            start_date: YYYY-MM-DD
            end_date:   YYYY-MM-DD (defaults to start_date)
            player_type: 'pitcher' or 'batter' — scopes Baseball Savant's
                         search, but the pitch list returned is the same.
        """
        if end_date is None:
            end_date = start_date

        client = await _get_client()
        url = "https://baseballsavant.mlb.com/statcast_search/csv"
        params = {
            "all": "true",
            "type": "details",
            "game_date_gt": start_date,
            "game_date_lt": end_date,
            "player_type": player_type,
            "min_pitches": "1",
        }

        try:
            resp = await client.get(url, params=params, timeout=120.0, follow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Statcast fetch error: {e}")
            return {"error": str(e), "pitches": 0}

        content = resp.text
        if not content or len(content) < 100:
            return {"error": "Empty response from Baseball Savant", "pitches": 0}

        import csv
        import io

        reader = csv.DictReader(io.StringIO(content))
        if reader.fieldnames:
            reader.fieldnames = [
                f.strip().strip('"').strip('\ufeff"').strip('\ufeff')
                for f in reader.fieldnames
            ]

        rows_list = list(reader)
        total_pitches = len(rows_list)
        if total_pitches == 0:
            return {"error": "No data rows", "pitches": 0}

        # Typed coercion with sanity clamps so sensor noise doesn't corrupt models.
        def _f(val, lo=None, hi=None):
            if val is None or val == "":
                return None
            try:
                x = float(val)
            except (TypeError, ValueError):
                return None
            if lo is not None and x < lo:
                return None
            if hi is not None and x > hi:
                return None
            return x

        def _i(val):
            if val is None or val == "":
                return None
            try:
                return int(float(val))
            except (TypeError, ValueError):
                return None

        def _s(val):
            if val is None:
                return None
            s = str(val).strip()
            return s if s else None

        pitch_rows: list[tuple] = []
        pitcher_stats: dict[tuple[str, str], dict] = {}

        for row in rows_list:
            game_pk = _i(row.get("game_pk"))
            at_bat_number = _i(row.get("at_bat_number"))
            pitch_number = _i(row.get("pitch_number"))
            if game_pk is None or at_bat_number is None or pitch_number is None:
                continue

            # Savant CSV puts the pitcher's name in `player_name` when the
            # search was scoped to pitchers; for batter-scoped searches the
            # name is the batter's. Use both columns defensively.
            pitcher_name = _s(row.get("player_name")) if player_type == "pitcher" else _s(row.get("pitcher_name"))
            batter_name  = _s(row.get("player_name")) if player_type == "batter"  else _s(row.get("batter_name"))

            pitch_rows.append((
                game_pk, at_bat_number, pitch_number,
                _s(row.get("game_date")),
                _s(row.get("home_team")), _s(row.get("away_team")),
                _i(row.get("inning")), _s(row.get("inning_topbot")),
                _i(row.get("pitcher")), pitcher_name, _s(row.get("p_throws")),
                _i(row.get("batter")),  batter_name,  _s(row.get("stand")),
                _s(row.get("pitch_type")), _s(row.get("pitch_name")),
                _f(row.get("release_speed"), 40, 115),
                _f(row.get("release_spin_rate"), 0, 4500),
                _f(row.get("release_extension"), 0, 10),
                _f(row.get("release_pos_x")), _f(row.get("release_pos_y")), _f(row.get("release_pos_z")),
                _f(row.get("spin_axis")),
                _f(row.get("pfx_x")), _f(row.get("pfx_z")),
                _f(row.get("plate_x")), _f(row.get("plate_z")),
                _i(row.get("zone")),
                _f(row.get("sz_top")), _f(row.get("sz_bot")),
                _f(row.get("launch_speed"), 5, 130),
                _f(row.get("launch_angle"), -90, 90),
                _f(row.get("hit_distance_sc"), 0, 500),
                _s(row.get("bb_type")),
                _f(row.get("hc_x")), _f(row.get("hc_y")),
                _s(row.get("type")), _s(row.get("description")), _s(row.get("events")),
                _i(row.get("balls")), _i(row.get("strikes")), _i(row.get("outs_when_up")),
                _i(row.get("on_1b")), _i(row.get("on_2b")), _i(row.get("on_3b")),
                _f(row.get("estimated_ba_using_speedangle"), 0, 1),
                _f(row.get("estimated_woba_using_speedangle"), 0, 3),
                _f(row.get("woba_value")), _f(row.get("woba_denom")),
                _i(row.get("post_home_score")), _i(row.get("post_away_score")),
            ))

            # Roll up per-pitcher-game aggregates for the legacy player_stats table.
            pitcher = (row.get("player_name") or "").strip()
            g_date = (row.get("game_date") or start_date).strip()
            if not pitcher:
                continue
            key = (pitcher, g_date)
            if key not in pitcher_stats:
                pitcher_stats[key] = {
                    "pitches": 0, "strikeouts": 0,
                    "velocities": [], "exit_velocities": [],
                    "pitcher_team": (
                        (row.get("home_team") or "").strip()
                        if (row.get("inning_topbot") or "") == "Bot"
                        else (row.get("away_team") or "").strip()
                    ),
                }
            s = pitcher_stats[key]
            s["pitches"] += 1
            vel = _f(row.get("release_speed"), 50, 110)
            if vel is not None:
                s["velocities"].append(vel)
            ev = _f(row.get("launch_speed"), 10, 130)
            if ev is not None:
                s["exit_velocities"].append(ev)
            event = (row.get("events") or "").strip()
            if event in ("strikeout", "strikeout_double_play"):
                s["strikeouts"] += 1

        # Chunked insert so the WriteCoordinator queue drains between batches.
        stored_pitches = 0
        if pitch_rows:
            INSERT_SQL = (
                "INSERT OR IGNORE INTO statcast_pitches ("
                "game_pk, at_bat_number, pitch_number, game_date, "
                "home_team, away_team, inning, inning_topbot, "
                "pitcher_id, pitcher_name, pitcher_throws, "
                "batter_id, batter_name, batter_stands, "
                "pitch_type, pitch_name, release_speed, release_spin_rate, "
                "release_extension, release_pos_x, release_pos_y, release_pos_z, "
                "spin_axis, pfx_x, pfx_z, plate_x, plate_z, zone, sz_top, sz_bot, "
                "launch_speed, launch_angle, hit_distance_sc, bb_type, hc_x, hc_y, "
                "type, description, events, balls, strikes, outs_when_up, "
                "on_1b, on_2b, on_3b, "
                "estimated_ba_using_speedangle, estimated_woba_using_speedangle, "
                "woba_value, woba_denom, post_home_score, post_away_score"
                ") VALUES (" + ",".join(["?"] * 51) + ")"
            )
            CHUNK = 2000
            for start in range(0, len(pitch_rows), CHUNK):
                try:
                    await self._db.executemany(INSERT_SQL, pitch_rows[start:start + CHUNK])
                    stored_pitches += len(pitch_rows[start:start + CHUNK])
                except Exception as e:
                    logger.warning(f"Statcast pitch batch insert failed (chunk {start}): {e}")

        # Legacy per-pitcher-game aggregate keeps player_stats consumers alive.
        batch_rows = []
        for (pitcher, game_date), stats in pitcher_stats.items():
            avg_velo = round(sum(stats["velocities"]) / len(stats["velocities"]), 1) if stats["velocities"] else None
            avg_ev = round(sum(stats["exit_velocities"]) / len(stats["exit_velocities"]), 1) if stats["exit_velocities"] else None
            stat_entries = {
                "pitches": stats["pitches"],
                "strikeouts": stats["strikeouts"],
                "avg_velocity": avg_velo,
                "avg_exit_velocity": avg_ev,
            }
            for stat_type, stat_value in stat_entries.items():
                if stat_value is None:
                    continue
                batch_rows.append((
                    "baseball_mlb",
                    f"statcast_{game_date}",
                    game_date,
                    pitcher,
                    stats.get("pitcher_team", ""),
                    f"statcast_{stat_type}",
                    str(stat_value),
                ))

        stored_aggregates = 0
        if batch_rows:
            try:
                await self._db.executemany(
                    "INSERT OR IGNORE INTO player_stats "
                    "(sport, event_id, game_date, player_name, team, stat_type, stat_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                stored_aggregates = len(batch_rows)
            except Exception as e:
                logger.warning(f"Statcast aggregate batch insert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_statcast")
        logger.info(
            f"Statcast {start_date}..{end_date}: {total_pitches} pitches → "
            f"stored {stored_pitches} pitch rows + {stored_aggregates} aggregate stats "
            f"({len(pitcher_stats)} pitchers)"
        )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "player_type": player_type,
            "total_pitches": total_pitches,
            "pitchers": len(pitcher_stats),
            "pitch_rows_stored": stored_pitches,
            "aggregate_rows_stored": stored_aggregates,
        }

    # ── MLB PLAYER METADATA ──

    async def collect_mlb_players(self) -> dict:
        """
        Refresh the mlb_players table from the free MLB Stats API.

        Pulls every active player on every 40-man roster across all 30 teams.
        Anchors every pitcher-vs-batter prop model: height, weight, bats,
        throws, primary position, MLB debut date, current team.

        Idempotent — uses INSERT OR REPLACE keyed on player_id. Safe to run
        nightly. Takes ~30 HTTP calls (teams + per-team rosters) and returns
        ~1,300 players.

        Uses only `https://statsapi.mlb.com/api/v1/…` — no API key required.
        """
        client = await _get_client()

        # 1. teams list
        try:
            resp = await client.get(
                "https://statsapi.mlb.com/api/v1/teams",
                params={"sportId": 1, "activeStatus": "Y"},
                timeout=30.0, follow_redirects=True,
            )
            resp.raise_for_status()
            teams = resp.json().get("teams", [])
        except Exception as e:
            logger.error(f"MLB Stats API teams fetch failed: {e}")
            return {"error": str(e), "players": 0}

        # 2. for each team, pull the 40-man + active roster (union)
        roster_entries: list[dict] = []
        for t in teams:
            team_id = t.get("id")
            team_abbr = t.get("abbreviation") or t.get("teamCode")
            if not team_id:
                continue
            for roster_type in ("40Man", "active"):
                try:
                    r = await client.get(
                        f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                        params={"rosterType": roster_type},
                        timeout=20.0, follow_redirects=True,
                    )
                    r.raise_for_status()
                    for entry in r.json().get("roster", []):
                        person = entry.get("person", {})
                        pid = person.get("id")
                        if pid:
                            roster_entries.append({
                                "player_id": pid,
                                "current_team_id": team_id,
                                "current_team_abbr": team_abbr,
                            })
                except Exception as e:
                    logger.debug(f"Roster {team_id} ({roster_type}) failed: {e}")

        # Dedup (a player can appear in both rosters for the same team)
        seen_ids = set()
        unique_entries: list[dict] = []
        for e in roster_entries:
            if e["player_id"] in seen_ids:
                continue
            seen_ids.add(e["player_id"])
            unique_entries.append(e)

        # 3. batched detail fetch — MLB supports personIds up to ~100 per call
        def _height_to_inches(h: str) -> Optional[int]:
            """Convert '6' 2\"' or \"6'2\" to inches, returning None on failure."""
            if not h or not isinstance(h, str):
                return None
            import re as _re
            m = _re.match(r"\s*(\d+)\s*['′]\s*(\d+)", h)
            if m:
                return int(m.group(1)) * 12 + int(m.group(2))
            return None

        def _weight_to_lb(w) -> Optional[int]:
            try:
                return int(str(w).replace("lbs", "").replace("lb", "").strip())
            except (TypeError, ValueError, AttributeError):
                return None

        batch_size = 80
        player_rows: list[tuple] = []
        by_id = {e["player_id"]: e for e in unique_entries}
        ids = list(by_id.keys())

        for start in range(0, len(ids), batch_size):
            chunk = ids[start:start + batch_size]
            try:
                r = await client.get(
                    "https://statsapi.mlb.com/api/v1/people",
                    params={"personIds": ",".join(str(i) for i in chunk)},
                    timeout=30.0, follow_redirects=True,
                )
                r.raise_for_status()
                people = r.json().get("people", [])
            except Exception as e:
                logger.warning(f"MLB people detail batch failed ({start}): {e}")
                continue

            for p in people:
                pid = p.get("id")
                if not pid:
                    continue
                anchor = by_id.get(pid, {})
                pos = p.get("primaryPosition", {}) or {}
                bats = (p.get("batSide", {}) or {}).get("code")
                throws = (p.get("pitchHand", {}) or {}).get("code")
                player_rows.append((
                    pid,
                    p.get("fullName") or "",
                    p.get("firstName"),
                    p.get("lastName"),
                    pos.get("abbreviation"),
                    pos.get("type"),
                    bats,
                    throws,
                    _height_to_inches(p.get("height")),
                    _weight_to_lb(p.get("weight")),
                    p.get("birthDate"),
                    p.get("mlbDebutDate"),
                    anchor.get("current_team_id"),
                    anchor.get("current_team_abbr"),
                    1 if p.get("active") else 0,
                ))

        stored = 0
        if player_rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO mlb_players ("
                    "player_id, full_name, first_name, last_name, "
                    "primary_position, position_type, bats, throws, "
                    "height_in, weight_lb, birth_date, mlb_debut_date, "
                    "current_team_id, current_team_abbr, active, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    player_rows,
                )
                stored = len(player_rows)
            except Exception as e:
                logger.warning(f"mlb_players upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_mlb_players")
        logger.info(
            f"MLB players: refreshed {stored} records across {len(teams)} teams"
        )
        return {
            "teams": len(teams),
            "roster_entries": len(unique_entries),
            "players_upserted": stored,
        }

    # ──────────────────────────────────────────
    # NHL: player metadata + per-shot play-by-play
    # Source: api.nhle.com (free, no key required).
    # ──────────────────────────────────────────

    NHL_API = "https://api-web.nhle.com/v1"

    async def collect_nhl_players(self) -> dict:
        """Refresh the nhl_players table from api.nhle.com.

        For each of the 32 teams we pull /roster/{abbr}/current (active
        players) and then /player/{id}/landing for bio fields (height,
        weight, shoots, birth, draft). INSERT OR REPLACE keyed on player_id.
        """
        client = await _get_client()
        try:
            r = await client.get(
                f"{self.NHL_API}/standings/now",
                timeout=20.0, follow_redirects=True,
            )
            r.raise_for_status()
            standings = r.json().get("standings", [])
        except Exception as e:
            logger.error(f"NHL standings fetch failed: {e}")
            return {"error": str(e), "players": 0}

        teams = []
        for t in standings:
            abbr = (t.get("teamAbbrev") or {}).get("default")
            tid = t.get("teamId") or t.get("teamAbbrevId")
            if abbr:
                teams.append({"abbr": abbr, "team_id": tid})

        player_ids: set[int] = set()
        team_by_player: dict[int, tuple] = {}
        for team in teams:
            try:
                r = await client.get(
                    f"{self.NHL_API}/roster/{team['abbr']}/current",
                    timeout=20.0, follow_redirects=True,
                )
                r.raise_for_status()
                roster = r.json() or {}
                for group in ("forwards", "defensemen", "goalies"):
                    for p in roster.get(group, []) or []:
                        pid = p.get("id")
                        if pid:
                            player_ids.add(pid)
                            team_by_player[pid] = (team.get("team_id"), team["abbr"])
            except Exception as e:
                logger.debug(f"NHL roster {team['abbr']} failed: {e}")

        player_rows: list[tuple] = []
        from asyncio import sleep as _sleep
        for pid in player_ids:
            try:
                r = await client.get(
                    f"{self.NHL_API}/player/{pid}/landing",
                    timeout=15.0, follow_redirects=True,
                )
                r.raise_for_status()
                p = r.json() or {}
            except Exception as e:
                logger.debug(f"NHL player {pid} landing failed: {e}")
                continue
            team_id, team_abbr = team_by_player.get(pid, (None, None))
            draft = p.get("draftDetails", {}) or {}
            fname = (p.get("firstName") or {}).get("default", "")
            lname = (p.get("lastName") or {}).get("default", "")
            bcity = p.get("birthCity")
            if isinstance(bcity, dict):
                bcity = bcity.get("default")
            player_rows.append((
                pid,
                f"{fname} {lname}".strip(),
                fname or None,
                lname or None,
                p.get("position"),
                p.get("shootsCatches"),
                p.get("sweaterNumber"),
                p.get("heightInInches"),
                p.get("weightInPounds"),
                p.get("birthDate"),
                p.get("birthCountry"),
                bcity,
                draft.get("year"),
                draft.get("round"),
                draft.get("pickInRound"),
                draft.get("teamAbbrev"),
                team_id,
                team_abbr,
                1,
            ))
            await _sleep(0.05)

        stored = 0
        if player_rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO nhl_players ("
                    "player_id, full_name, first_name, last_name, position, "
                    "shoots_catches, sweater_number, height_in, weight_lb, "
                    "birth_date, birth_country, birth_city, "
                    "draft_year, draft_round, draft_pick, draft_team, "
                    "current_team_id, current_team_abbr, active, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    player_rows,
                )
                stored = len(player_rows)
            except Exception as e:
                logger.warning(f"nhl_players upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_nhl_players")
        logger.info(f"NHL players: refreshed {stored} across {len(teams)} teams")
        return {"teams": len(teams), "players_upserted": stored}

    async def collect_nhl_shots(self, date: Optional[str] = None) -> dict:
        """Per-shot event ingestion from api-web.nhle.com for games on `date`.

        Walks /schedule/{date}, then /gamecenter/{game_id}/play-by-play for
        each completed game, extracts shot-on-goal / missed-shot /
        blocked-shot / goal events, INSERT OR IGNORE on (game_id, event_id).
        """
        client = await _get_client()
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            r = await client.get(
                f"{self.NHL_API}/schedule/{date}",
                timeout=20.0, follow_redirects=True,
            )
            r.raise_for_status()
            sched = r.json() or {}
        except Exception as e:
            logger.error(f"NHL schedule {date} fetch failed: {e}")
            return {"error": str(e), "games": 0, "shots": 0}

        game_ids: list[int] = []
        for week_day in sched.get("gameWeek", []):
            if week_day.get("date") != date:
                continue
            for g in week_day.get("games", []):
                state = g.get("gameState") or g.get("gameScheduleState")
                if state in ("OFF", "FINAL"):
                    gid = g.get("id")
                    if gid:
                        game_ids.append(gid)

        all_shot_rows: list[tuple] = []
        SHOT_EVENTS = {"shot-on-goal", "missed-shot", "blocked-shot", "goal"}
        for gid in game_ids:
            try:
                r = await client.get(
                    f"{self.NHL_API}/gamecenter/{gid}/play-by-play",
                    timeout=30.0, follow_redirects=True,
                )
                r.raise_for_status()
                pbp = r.json() or {}
            except Exception as e:
                logger.debug(f"NHL play-by-play {gid} failed: {e}")
                continue
            home_team = pbp.get("homeTeam") or {}
            away_team = pbp.get("awayTeam") or {}
            game_date = pbp.get("gameDate") or date
            for play in pbp.get("plays", []) or []:
                etype = play.get("typeDescKey") or ""
                if etype not in SHOT_EVENTS:
                    continue
                pdesc = play.get("periodDescriptor") or {}
                d = play.get("details") or {}
                shooter_team_id = d.get("eventOwnerTeamId")
                shooter_team_abbr = (
                    home_team.get("abbrev") if shooter_team_id == home_team.get("id")
                    else away_team.get("abbrev") if shooter_team_id == away_team.get("id")
                    else None
                )
                all_shot_rows.append((
                    gid,
                    play.get("eventId"),
                    game_date,
                    pdesc.get("number"),
                    pdesc.get("periodType"),
                    play.get("timeInPeriod"),
                    play.get("timeRemaining"),
                    etype,
                    d.get("shotType"),
                    play.get("situationCode"),
                    d.get("xCoord"),
                    d.get("yCoord"),
                    d.get("zoneCode"),
                    shooter_team_id,
                    shooter_team_abbr,
                    d.get("shootingPlayerId") or d.get("scoringPlayerId"),
                    d.get("goalieInNetId"),
                    d.get("assist1PlayerId"),
                    d.get("assist2PlayerId"),
                    1 if etype == "goal" else 0,
                    d.get("homeScore"),
                    d.get("awayScore"),
                ))

        stored = 0
        if all_shot_rows:
            INSERT_SQL = (
                "INSERT OR IGNORE INTO nhl_shot_events ("
                "game_id, event_id, game_date, period, period_type, "
                "time_in_period, time_remaining, event_type, shot_type, "
                "situation_code, x_coord, y_coord, zone_code, "
                "shooting_team_id, shooting_team_abbr, "
                "shooter_id, goalie_id, assist1_id, assist2_id, "
                "is_goal, home_score, away_score"
                ") VALUES (" + ",".join(["?"] * 22) + ")"
            )
            CHUNK = 2000
            for start in range(0, len(all_shot_rows), CHUNK):
                try:
                    await self._db.executemany(INSERT_SQL, all_shot_rows[start:start + CHUNK])
                    stored += len(all_shot_rows[start:start + CHUNK])
                except Exception as e:
                    logger.warning(f"nhl_shot_events batch insert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_nhl_shots")
        logger.info(f"NHL shots {date}: {len(game_ids)} games → {stored} shot rows")
        return {"date": date, "games": len(game_ids), "shots_stored": stored}

    # ──────────────────────────────────────────
    # NFL: roster, combine, play-by-play
    # Source: nflverse-data release CSVs on GitHub (free, public).
    # ──────────────────────────────────────────

    NFLFASTR_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

    async def collect_nfl_players(self, season: Optional[int] = None) -> dict:
        """Refresh nfl_players from nflverse seasonal roster CSV."""
        import csv
        import io as _io
        if season is None:
            season = datetime.now(timezone.utc).year
        url = f"{self.NFLFASTR_BASE}/rosters/roster_{season}.csv"
        client = await _get_client()
        try:
            r = await client.get(url, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"NFL roster {season} fetch failed: {e}")
            return {"error": str(e), "players": 0}

        reader = csv.DictReader(_io.StringIO(r.text))
        rows: list[tuple] = []
        for row in reader:
            pid = row.get("gsis_id") or row.get("player_id") or ""
            if not pid:
                continue
            height_in = None
            h = (row.get("height") or "").strip()
            if h:
                try:
                    if "-" in h:
                        ft, inc = h.split("-")
                        height_in = int(ft) * 12 + int(inc)
                    else:
                        height_in = int(float(h))
                except ValueError:
                    height_in = None
            try:
                weight_lb = int(float(row.get("weight") or 0)) or None
            except ValueError:
                weight_lb = None
            try:
                jersey = int(float(row.get("jersey_number") or 0)) or None
            except ValueError:
                jersey = None
            def _ival(k):
                try:
                    return int(float(row.get(k) or 0)) or None
                except ValueError:
                    return None
            rows.append((
                pid,
                (row.get("full_name") or f"{row.get('first_name','')} {row.get('last_name','')}").strip(),
                row.get("first_name") or None,
                row.get("last_name") or None,
                row.get("position") or None,
                row.get("position_group") or None,
                jersey,
                height_in,
                weight_lb,
                row.get("birth_date") or None,
                row.get("college") or None,
                _ival("entry_year"),
                _ival("draft_round"),
                _ival("draft_number"),
                _ival("years_exp"),
                row.get("team") or None,
                row.get("status") or None,
                row.get("headshot_url") or None,
            ))

        stored = 0
        if rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO nfl_players ("
                    "player_id, full_name, first_name, last_name, position, "
                    "position_group, jersey_number, height_in, weight_lb, "
                    "birth_date, college, draft_year, draft_round, draft_pick, "
                    "years_exp, current_team, status, headshot_url, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    rows,
                )
                stored = len(rows)
            except Exception as e:
                logger.warning(f"nfl_players upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_nfl_players")
        logger.info(f"NFL roster {season}: {stored} players upserted")
        return {"season": season, "players_upserted": stored}

    async def collect_nfl_combine(self, start_year: int = 2000) -> dict:
        """Refresh nfl_combine_results from nflverse combine CSV."""
        import csv
        import io as _io
        url = f"{self.NFLFASTR_BASE}/combine/combine.csv"
        client = await _get_client()
        try:
            r = await client.get(url, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"NFL combine fetch failed: {e}")
            return {"error": str(e), "rows": 0}

        reader = csv.DictReader(_io.StringIO(r.text))

        def _num(v, cast=float):
            if v is None or v == "" or v == "NA":
                return None
            try:
                return cast(float(v))
            except (TypeError, ValueError):
                return None

        rows: list[tuple] = []
        for row in reader:
            try:
                year = int(float(row.get("season") or row.get("year") or 0))
            except ValueError:
                continue
            if year < start_year:
                continue
            full_name = (row.get("player_name") or row.get("full_name") or "").strip()
            if not full_name:
                continue
            rows.append((
                row.get("pfr_id") or row.get("gsis_id") or None,
                year,
                full_name,
                row.get("pos") or row.get("position") or None,
                row.get("school") or row.get("college") or None,
                _num(row.get("ht") or row.get("height")),
                _num(row.get("wt") or row.get("weight"), int),
                _num(row.get("arm")),
                _num(row.get("hand")),
                _num(row.get("forty") or row.get("forty_yard")),
                _num(row.get("bench") or row.get("bench_press"), int),
                _num(row.get("vertical")),
                _num(row.get("broad_jump"), int),
                _num(row.get("cone") or row.get("three_cone")),
                _num(row.get("shuttle")),
                _num(row.get("draft_year"), int),
                _num(row.get("draft_round"), int),
                _num(row.get("draft_pick"), int),
                row.get("draft_team") or None,
            ))

        stored = 0
        if rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO nfl_combine_results ("
                    "player_id, combine_year, full_name, position, college, "
                    "height_in, weight_lb, arm_length_in, hand_size_in, "
                    "forty_yard, bench_press_reps, vertical_in, broad_jump_in, "
                    "three_cone, shuttle_20y, draft_year, draft_round, draft_pick, draft_team"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                stored = len(rows)
            except Exception as e:
                logger.warning(f"nfl_combine_results upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_nfl_combine")
        logger.info(f"NFL combine: {stored} rows upserted (since {start_year})")
        return {"rows_upserted": stored}

    async def collect_nfl_plays(self, season: Optional[int] = None) -> dict:
        """Stream-ingest nflfastR per-season play_by_play CSV into nfl_play_events."""
        import csv
        import io as _io
        if season is None:
            season = datetime.now(timezone.utc).year
        url = f"{self.NFLFASTR_BASE}/pbp/play_by_play_{season}.csv"
        client = await _get_client()
        try:
            r = await client.get(url, timeout=300.0, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"NFL PBP {season} fetch failed: {e}")
            return {"error": str(e), "plays": 0}

        reader = csv.DictReader(_io.StringIO(r.text))

        def _i(v):
            if v is None or v == "" or v == "NA":
                return None
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        def _f(v):
            if v is None or v == "" or v == "NA":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _s(v):
            if v is None:
                return None
            s = str(v).strip()
            return s if s and s != "NA" else None

        rows: list[tuple] = []
        for row in reader:
            pid = _i(row.get("play_id"))
            gid = _s(row.get("game_id"))
            if pid is None or gid is None:
                continue
            rows.append((
                pid, gid,
                _s(row.get("game_date")),
                _s(row.get("home_team")), _s(row.get("away_team")),
                _s(row.get("posteam")), _s(row.get("defteam")),
                _i(row.get("season")), _i(row.get("week")), _i(row.get("qtr")),
                _s(row.get("time")), _i(row.get("down")), _i(row.get("ydstogo")),
                _s(row.get("yrdln")), _i(row.get("yardline_100")),
                _s(row.get("play_type")), _i(row.get("yards_gained")),
                _f(row.get("epa")), _f(row.get("wpa")), _i(row.get("success")),
                _s(row.get("passer_id") or row.get("passer_player_id")),
                _s(row.get("passer") or row.get("passer_player_name")),
                _s(row.get("receiver_id") or row.get("receiver_player_id")),
                _s(row.get("receiver") or row.get("receiver_player_name")),
                _f(row.get("air_yards")), _f(row.get("yards_after_catch")),
                _s(row.get("pass_length")), _s(row.get("pass_location")),
                _i(row.get("complete_pass")), _i(row.get("incomplete_pass")),
                _i(row.get("interception")),
                _s(row.get("rusher_id") or row.get("rusher_player_id")),
                _s(row.get("rusher") or row.get("rusher_player_name")),
                _s(row.get("run_location")), _s(row.get("run_gap")),
                _i(row.get("sack")), _i(row.get("qb_hit")),
                _i(row.get("tackle_with_assist")),
                _s(row.get("sack_player_id")),
                _i(row.get("touchdown")), _s(row.get("td_player_id")),
                _i(row.get("field_goal_attempt")),
                _s(row.get("field_goal_result")),
                _i(row.get("kick_distance")),
                _i(row.get("score_differential")),
            ))

        stored = 0
        if rows:
            INSERT_SQL = (
                "INSERT OR IGNORE INTO nfl_play_events ("
                "play_id, game_id, game_date, home_team, away_team, posteam, "
                "defteam, season, week, qtr, time, down, ydstogo, yrdln, "
                "yardline_100, play_type, yards_gained, epa, wpa, success, "
                "passer_id, passer_name, receiver_id, receiver_name, "
                "air_yards, yards_after_catch, pass_length, pass_location, "
                "complete_pass, incomplete_pass, interception, rusher_id, "
                "rusher_name, run_location, run_gap, sack, qb_hit, "
                "tackle_with_assist, sack_player_id, touchdown, td_player_id, "
                "field_goal_attempt, field_goal_result, kick_distance, "
                "score_differential"
                ") VALUES (" + ",".join(["?"] * 45) + ")"
            )
            CHUNK = 2000
            for start in range(0, len(rows), CHUNK):
                try:
                    await self._db.executemany(INSERT_SQL, rows[start:start + CHUNK])
                    stored += len(rows[start:start + CHUNK])
                except Exception as e:
                    logger.warning(f"nfl_play_events batch insert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_nfl_plays")
        logger.info(f"NFL PBP {season}: {stored} plays stored")
        return {"season": season, "plays_stored": stored}

    # ──────────────────────────────────────────
    # NBA: shot chart detail + roster
    # Source: stats.nba.com (free, requires UA + x-nba-stats-origin headers).
    # ──────────────────────────────────────────

    NBA_STATS_BASE = "https://stats.nba.com/stats"
    NBA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Callisto/1.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
        "Connection": "keep-alive",
    }

    async def collect_nba_players(self, season: Optional[str] = None) -> dict:
        """Refresh nba_players from stats.nba.com commonallplayers."""
        import httpx as _httpx
        client = _httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, max_redirects=5,
            headers=self.NBA_HEADERS,
        )
        try:
            if season is None:
                now = datetime.now(timezone.utc)
                y0 = now.year if now.month >= 10 else now.year - 1
                season = f"{y0}-{str(y0 + 1)[-2:]}"
            params = {"IsOnlyCurrentSeason": "1", "LeagueID": "00", "Season": season}
            try:
                r = await client.get(
                    f"{self.NBA_STATS_BASE}/commonallplayers",
                    params=params,
                )
                r.raise_for_status()
                js = r.json()
            except Exception as e:
                logger.error(f"NBA commonallplayers fetch failed: {e}")
                return {"error": str(e), "players": 0}
            rs = (js.get("resultSets") or [{}])[0]
            headers = rs.get("headers") or []
            idx = {h: i for i, h in enumerate(headers)}
            rows = rs.get("rowSet") or []

            def _get(row, key):
                i = idx.get(key)
                return row[i] if i is not None and i < len(row) else None

            player_rows: list[tuple] = []
            for row in rows:
                pid = _get(row, "PERSON_ID")
                if pid is None:
                    continue
                full_name = (_get(row, "DISPLAY_FIRST_LAST") or "").strip()
                player_rows.append((
                    int(pid),
                    full_name,
                    None, None,
                    None,
                    None, None, None, None,
                    None, None, None, None,
                    None, None, None, None,
                    _get(row, "TEAM_ID"),
                    _get(row, "TEAM_ABBREVIATION"),
                    1 if _get(row, "ROSTERSTATUS") == 1 else 0,
                ))

            stored = 0
            if player_rows:
                try:
                    await self._db.executemany(
                        "INSERT OR REPLACE INTO nba_players ("
                        "player_id, full_name, first_name, last_name, position, "
                        "height_in, weight_lb, wingspan_in, standing_reach_in, "
                        "jersey_number, birth_date, country, college, "
                        "draft_year, draft_round, draft_pick, years_pro, "
                        "current_team_id, current_team_abbr, active, updated_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                        player_rows,
                    )
                    stored = len(player_rows)
                except Exception as e:
                    logger.warning(f"nba_players upsert failed: {e}")

            from tools.db_utils import commit_with_retry
            await commit_with_retry(self._db, operation="data_collector collect_nba_players")
            logger.info(f"NBA players {season}: {stored} upserted")
            return {"season": season, "players_upserted": stored}
        finally:
            await client.aclose()

    async def collect_nba_shots(self, date: Optional[str] = None) -> dict:
        """Per-shot events from stats.nba.com shotchartdetail for games on `date`."""
        import httpx as _httpx
        from asyncio import sleep as _sleep
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        client = _httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, max_redirects=5,
            headers=self.NBA_HEADERS,
        )
        try:
            try:
                r = await client.get(
                    f"{self.NBA_STATS_BASE}/scoreboardv3",
                    params={"GameDate": date, "LeagueID": "00"},
                )
                r.raise_for_status()
                sb = r.json()
            except Exception as e:
                logger.debug(f"NBA scoreboardv3 {date} failed: {e}")
                return {"error": str(e), "games": 0, "shots": 0}

            games = (sb.get("scoreboard") or {}).get("games") or []
            game_ids = [
                g.get("gameId") for g in games
                if (g.get("gameStatus") == 3 and g.get("gameId"))
            ]
            season = (sb.get("scoreboard") or {}).get("seasonYear") or ""

            all_rows: list[tuple] = []
            for gid in game_ids:
                await _sleep(0.6)
                try:
                    params = {
                        "ContextMeasure": "FGA", "GameID": gid, "TeamID": "0",
                        "PlayerID": "0", "Season": season,
                        "SeasonType": "Regular Season", "LeagueID": "00",
                    }
                    r = await client.get(
                        f"{self.NBA_STATS_BASE}/shotchartdetail", params=params,
                    )
                    r.raise_for_status()
                    js = r.json()
                except Exception as e:
                    logger.debug(f"NBA shotchartdetail {gid} failed: {e}")
                    continue

                rs = (js.get("resultSets") or [{}])[0]
                headers = rs.get("headers") or []
                idx = {h: i for i, h in enumerate(headers)}
                rows = rs.get("rowSet") or []

                def _g(row, key):
                    i = idx.get(key)
                    return row[i] if i is not None and i < len(row) else None

                for row in rows:
                    all_rows.append((
                        _g(row, "GAME_ID"),
                        _g(row, "GAME_EVENT_ID"),
                        date,
                        _g(row, "PLAYER_ID"),
                        _g(row, "PLAYER_NAME"),
                        _g(row, "TEAM_ID"),
                        _g(row, "TEAM_NAME"),
                        _g(row, "PERIOD"),
                        _g(row, "MINUTES_REMAINING"),
                        _g(row, "SECONDS_REMAINING"),
                        _g(row, "SHOT_TYPE"),
                        _g(row, "ACTION_TYPE"),
                        _g(row, "SHOT_ZONE_BASIC"),
                        _g(row, "SHOT_ZONE_AREA"),
                        _g(row, "SHOT_ZONE_RANGE"),
                        _g(row, "SHOT_DISTANCE"),
                        _g(row, "LOC_X"),
                        _g(row, "LOC_Y"),
                        _g(row, "SHOT_MADE_FLAG"),
                        _g(row, "HTM"),
                        _g(row, "VTM"),
                    ))

            stored = 0
            if all_rows:
                INSERT_SQL = (
                    "INSERT OR IGNORE INTO nba_shot_events ("
                    "game_id, event_num, game_date, player_id, player_name, "
                    "team_id, team_abbr, period, minutes_remaining, "
                    "seconds_remaining, shot_type, action_type, "
                    "shot_zone_basic, shot_zone_area, shot_zone_range, "
                    "shot_distance, loc_x, loc_y, made_flag, htm, vtm"
                    ") VALUES (" + ",".join(["?"] * 21) + ")"
                )
                CHUNK = 2000
                for start in range(0, len(all_rows), CHUNK):
                    try:
                        await self._db.executemany(INSERT_SQL, all_rows[start:start + CHUNK])
                        stored += len(all_rows[start:start + CHUNK])
                    except Exception as e:
                        logger.warning(f"nba_shot_events batch insert failed: {e}")

            from tools.db_utils import commit_with_retry
            await commit_with_retry(self._db, operation="data_collector collect_nba_shots")
            logger.info(f"NBA shots {date}: {len(game_ids)} games → {stored} shots")
            return {"date": date, "games": len(game_ids), "shots_stored": stored}
        finally:
            await client.aclose()

    # ──────────────────────────────────────────
    # NCAA BASKETBALL (M + W): rosters + per-game box score stats
    # Source: ESPN college endpoints.
    # ──────────────────────────────────────────

    NCAA_BBALL_LEAGUES = {
        "basketball_ncaab": ("basketball", "mens-college-basketball"),
        "basketball_ncaaw": ("basketball", "womens-college-basketball"),
    }

    async def collect_ncaa_basketball_players(self, sport: str) -> dict:
        """Refresh ncaa_basketball_players for a given sport."""
        if sport not in self.NCAA_BBALL_LEAGUES:
            return {"error": f"unsupported sport: {sport}", "players": 0}
        category, league = self.NCAA_BBALL_LEAGUES[sport]
        client = await _get_client()

        teams: list[dict] = []
        for page in range(1, 25):
            try:
                r = await client.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/teams",
                    params={"limit": 400, "page": page},
                    timeout=30.0, follow_redirects=True,
                )
                r.raise_for_status()
                sports_obj = ((r.json().get("sports") or [{}])[0])
                leagues_obj = (sports_obj.get("leagues") or [{}])[0]
                page_teams = leagues_obj.get("teams") or []
                if not page_teams:
                    break
                for t in page_teams:
                    team = (t.get("team") or {})
                    if team.get("id"):
                        teams.append({
                            "id": team.get("id"),
                            "abbr": team.get("abbreviation"),
                            "name": team.get("displayName"),
                        })
                if len(page_teams) < 400:
                    break
            except Exception as e:
                logger.debug(f"NCAA {sport} team list page {page} failed: {e}")
                break

        def _height_to_inches(h) -> Optional[int]:
            if not h:
                return None
            import re as _re
            m = _re.match(r"\s*(\d+)\s*['′]\s*(\d+)", str(h))
            if m:
                return int(m.group(1)) * 12 + int(m.group(2))
            return None

        player_rows: list[tuple] = []
        for team in teams:
            try:
                r = await client.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/teams/{team['id']}/roster",
                    timeout=20.0, follow_redirects=True,
                )
                r.raise_for_status()
                roster = r.json().get("athletes") or []
            except Exception as e:
                logger.debug(f"NCAA {sport} roster {team['id']} failed: {e}")
                continue
            for a in roster:
                pid = a.get("id")
                if not pid:
                    continue
                pos = (a.get("position") or {}).get("abbreviation")
                exp_abbr = (a.get("experience") or {}).get("abbreviation")
                birthplace = a.get("birthPlace") or {}
                player_rows.append((
                    sport,
                    str(pid),
                    a.get("fullName") or a.get("displayName") or "",
                    a.get("firstName") or None,
                    a.get("lastName") or None,
                    str(team["id"]),
                    team.get("abbr"),
                    team.get("name"),
                    a.get("jersey") or None,
                    pos,
                    exp_abbr,
                    _height_to_inches(a.get("displayHeight")),
                    a.get("weight") or None,
                    birthplace.get("displayText") if isinstance(birthplace, dict) else None,
                    None,
                    1,
                ))

        stored = 0
        if player_rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO ncaa_basketball_players ("
                    "sport, player_id, full_name, first_name, last_name, "
                    "team_id, team_abbr, team_name, jersey_number, position, "
                    "class, height_in, weight_lb, home_town, hand, active, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    player_rows,
                )
                stored = len(player_rows)
            except Exception as e:
                logger.warning(f"ncaa_basketball_players upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_ncaa_basketball_players")
        logger.info(f"{sport}: {stored} players across {len(teams)} teams")
        return {"sport": sport, "teams": len(teams), "players_upserted": stored}

    async def collect_ncaa_basketball_game_stats(self, sport: str, date: Optional[str] = None) -> dict:
        """Fetch ESPN boxscores for completed NCAA games on `date` and
        upsert per-player per-game stats."""
        if sport not in self.NCAA_BBALL_LEAGUES:
            return {"error": f"unsupported sport: {sport}", "rows": 0}
        category, league = self.NCAA_BBALL_LEAGUES[sport]
        client = await _get_client()

        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y%m%d")
        date_compact = date.replace("-", "")
        game_date_fmt = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"

        try:
            r = await client.get(
                f"{ESPN_BASE}/{category}/{league}/scoreboard",
                params={"dates": date_compact, "limit": 500},
                timeout=30.0, follow_redirects=True,
            )
            r.raise_for_status()
            events = r.json().get("events", [])
        except Exception as e:
            logger.error(f"NCAA {sport} scoreboard {date} failed: {e}")
            return {"error": str(e), "games": 0, "rows": 0}

        completed = [
            e for e in events
            if (e.get("status", {}) or {}).get("type", {}).get("completed") is True
        ]

        def _int(v):
            try:
                return int(str(v).replace("-", "0"))
            except (TypeError, ValueError):
                return None

        def _frac(v):
            try:
                m, a = str(v).split("-")
                return int(m), int(a)
            except (TypeError, ValueError):
                return (None, None)

        stat_rows: list[tuple] = []
        for event in completed:
            event_id = event.get("id")
            if not event_id:
                continue
            try:
                r = await client.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/summary",
                    params={"event": event_id},
                    timeout=30.0, follow_redirects=True,
                )
                r.raise_for_status()
                sm = r.json() or {}
            except Exception as e:
                logger.debug(f"NCAA {sport} summary {event_id} failed: {e}")
                continue

            boxscore = sm.get("boxscore") or {}
            teams = boxscore.get("players") or []
            home_team_id, away_team_id = None, None
            home_abbr, away_abbr = None, None
            for competition in sm.get("header", {}).get("competitions", []):
                for c in competition.get("competitors", []):
                    tid = (c.get("team") or {}).get("id")
                    tabbr = (c.get("team") or {}).get("abbreviation")
                    if c.get("homeAway") == "home":
                        home_team_id, home_abbr = tid, tabbr
                    elif c.get("homeAway") == "away":
                        away_team_id, away_abbr = tid, tabbr

            for team_block in teams:
                team = (team_block.get("team") or {})
                team_id = team.get("id")
                team_abbr = team.get("abbreviation")
                is_home = 1 if str(team_id) == str(home_team_id) else 0
                opp_id = away_team_id if is_home else home_team_id
                opp_abbr = away_abbr if is_home else home_abbr

                for group in team_block.get("statistics") or []:
                    keys = group.get("keys") or []
                    key_idx = {k: i for i, k in enumerate(keys)}

                    def _val(stat_list, key):
                        i = key_idx.get(key)
                        return stat_list[i] if i is not None and i < len(stat_list) else None

                    for athlete in group.get("athletes") or []:
                        player = athlete.get("athlete") or {}
                        pid = player.get("id")
                        if not pid:
                            continue
                        stats = athlete.get("stats") or []
                        fgm, fga = _frac(_val(stats, "fg"))
                        fg3m, fg3a = _frac(_val(stats, "3pt") or _val(stats, "threePt"))
                        ftm, fta = _frac(_val(stats, "ft"))
                        mins = _val(stats, "min")
                        try:
                            mins_f = float(mins) if mins else None
                        except ValueError:
                            mins_f = None
                        pts = _int(_val(stats, "pts"))
                        reb = _int(_val(stats, "reb"))
                        orb = _int(_val(stats, "oreb") or _val(stats, "offReb"))
                        drb = _int(_val(stats, "dreb") or _val(stats, "defReb"))
                        ast = _int(_val(stats, "ast"))
                        stl = _int(_val(stats, "stl"))
                        blk = _int(_val(stats, "blk"))
                        tov = _int(_val(stats, "to") or _val(stats, "turnovers"))
                        pf = _int(_val(stats, "pf") or _val(stats, "fouls"))
                        plus_minus = _int(_val(stats, "+/-") or _val(stats, "plus_minus"))
                        ts_pct = None
                        efg_pct = None
                        if fga and pts is not None and fta is not None:
                            denom = 2.0 * (fga + 0.44 * fta)
                            ts_pct = round(pts / denom, 4) if denom else None
                        if fga and fgm is not None and fg3m is not None:
                            efg_pct = round((fgm + 0.5 * fg3m) / fga, 4) if fga else None

                        stat_rows.append((
                            sport,
                            str(event_id),
                            game_date_fmt,
                            str(pid),
                            player.get("displayName"),
                            str(team_id) if team_id else None,
                            team_abbr,
                            str(opp_id) if opp_id else None,
                            opp_abbr,
                            is_home,
                            1 if (athlete.get("starter") is True) else 0,
                            mins_f,
                            pts, reb, orb, drb, ast, stl, blk, tov, pf,
                            fgm, fga, fg3m, fg3a, ftm, fta,
                            plus_minus, ts_pct, efg_pct,
                        ))

        stored = 0
        if stat_rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO ncaa_basketball_game_stats ("
                    "sport, game_id, game_date, player_id, player_name, "
                    "team_id, team_abbr, opponent_team_id, opponent_abbr, "
                    "is_home, started, minutes, points, rebounds, off_reb, "
                    "def_reb, assists, steals, blocks, turnovers, "
                    "personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta, "
                    "plus_minus, true_shooting_pct, efg_pct"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    stat_rows,
                )
                stored = len(stat_rows)
            except Exception as e:
                logger.warning(f"ncaa_basketball_game_stats upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_ncaa_basketball_game_stats")
        logger.info(
            f"{sport} boxscores {date}: {len(completed)} games → {stored} rows"
        )
        return {"sport": sport, "games": len(completed), "rows_stored": stored}

    # ──────────────────────────────────────────
    # GOLF: round-level strokes-gained + core stats
    # ──────────────────────────────────────────

    async def collect_golf_player_rounds(self, season: Optional[int] = None) -> dict:
        """Ingest per-round SG data for the PGA Tour via DataGolf public JSON.

        Graceful on 403/404 — if the public feed is unreachable, log and
        return 0 rows rather than raising; callers can retry later.
        """
        import httpx as _httpx
        if season is None:
            season = datetime.now(timezone.utc).year

        rows: list[tuple] = []
        async with _httpx.AsyncClient(
            timeout=60.0, follow_redirects=True, max_redirects=5,
            headers={"User-Agent": "Mozilla/5.0 (Callisto)"},
        ) as c:
            try:
                r = await c.get(
                    "https://feeds.datagolf.com/preds/archive",
                    params={"tour": "pga", "year": season, "file_format": "json"},
                )
                r.raise_for_status()
                archive = r.json()
            except Exception as e:
                logger.info(f"DataGolf archive {season} unreachable ({e})")
                archive = []

            for ev in archive or []:
                event_id = str(ev.get("event_id") or ev.get("eventId") or "")
                if not event_id:
                    continue
                event_name = ev.get("event_name") or ev.get("eventName")
                course = ev.get("course") or ev.get("courseName")
                for round_entry in ev.get("rounds", []) or []:
                    round_num = round_entry.get("round_num") or round_entry.get("round")
                    round_date = round_entry.get("round_date") or round_entry.get("date")
                    for p in round_entry.get("players", []) or []:
                        pid = str(p.get("dg_id") or p.get("player_id") or "")
                        if not pid:
                            continue
                        rows.append((
                            pid,
                            p.get("player_name") or p.get("name") or "",
                            event_id,
                            event_name,
                            course,
                            season,
                            round_num,
                            round_date,
                            p.get("tee_time"),
                            p.get("score") or p.get("round_score"),
                            p.get("score_to_par") or p.get("round_to_par"),
                            p.get("thru"),
                            p.get("sg_total"),
                            p.get("sg_ott"),
                            p.get("sg_app"),
                            p.get("sg_arg"),
                            p.get("sg_putt") or p.get("sg_putting"),
                            p.get("sg_t2g"),
                            p.get("driving_distance") or p.get("dd"),
                            p.get("driving_accuracy") or p.get("da"),
                            p.get("gir_pct") or p.get("gir"),
                            p.get("scrambling_pct") or p.get("scrambling"),
                            p.get("putts_per_round") or p.get("putts"),
                            1 if p.get("made_cut") else 0,
                        ))

        stored = 0
        if rows:
            try:
                await self._db.executemany(
                    "INSERT OR REPLACE INTO golf_player_rounds ("
                    "player_id, player_name, event_id, event_name, course, "
                    "season, round_num, round_date, tee_time, score, "
                    "score_to_par, thru, sg_total, sg_ott, sg_app, sg_arg, "
                    "sg_putt, sg_t2g, driving_distance, driving_accuracy, "
                    "gir_pct, scrambling_pct, putts_per_round, made_cut"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                stored = len(rows)
            except Exception as e:
                logger.warning(f"golf_player_rounds upsert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_golf_player_rounds")
        logger.info(f"Golf rounds {season}: {stored} rows upserted")
        return {"season": season, "rows_upserted": stored}

    # ── EMBEDDING PIPELINE ──

    async def get_unembedded_contexts(
        self, sport: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Get game contexts that haven't been embedded yet."""
        if sport:
            cursor = await self._db.execute(
                "SELECT id, sport, event_id, game_date, home_team, away_team, "
                "home_score, away_score, context_json "
                "FROM game_contexts WHERE embedded = FALSE AND sport = ? "
                "ORDER BY game_date DESC LIMIT ?",
                (sport, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, sport, event_id, game_date, home_team, away_team, "
                "home_score, away_score, context_json "
                "FROM game_contexts WHERE embedded = FALSE "
                "ORDER BY game_date DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        contexts = []
        for row in rows:
            ctx = dict(zip(cols, row))
            ctx["context"] = json.loads(ctx.pop("context_json"))
            contexts.append(ctx)
        return contexts

    async def mark_embedded(self, context_id: int) -> None:
        """Mark a game context as embedded."""
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "UPDATE game_contexts SET embedded = TRUE WHERE id = ?",
            (context_id,),
            operation="data_collector mark_embedded",
        )
        await commit_with_retry(self._db, operation="data_collector mark_embedded")

    async def get_collection_stats(self) -> dict:
        """Return data collection statistics."""
        stats = {}
        for table in ["game_contexts", "player_stats"]:
            cursor = await self._db.execute(
                f"SELECT sport, COUNT(*) as count, "
                f"MIN(game_date) as earliest, MAX(game_date) as latest "
                f"FROM {table} GROUP BY sport"
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            stats[table] = [dict(zip(cols, r)) for r in rows]

        # Embedding pipeline status
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM game_contexts WHERE embedded = FALSE"
        )
        stats["unembedded_contexts"] = (await cursor.fetchone())[0]

        # Silent-drift visibility: player stat insert failures should be 0
        # in steady state. Non-zero means WARNING-log spam worth chasing —
        # usually a schema mismatch (see audit finding on player_stats
        # INFO-level logs that hid drift for weeks).
        stats["player_stat_insert_failures"] = self._player_stat_insert_failures

        return stats
