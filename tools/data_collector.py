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


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
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

        client = _get_client()
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

            # Store game context — use INSERT OR REPLACE to ensure enriched
            # data overwrites older sparse entries. The UNIQUE(sport, event_id)
            # constraint means re-collecting a game updates its context with
            # officials, rest_days, broadcasts, etc. that may have been missing
            # from earlier collections (due to DB locks or pre-enrichment code).
            try:
                from tools.db_utils import execute_with_retry
                await execute_with_retry(
                    self._db,
                    "INSERT INTO game_contexts "
                    "(sport, event_id, game_date, home_team, away_team, "
                    "home_score, away_score, context_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(sport, event_id) DO UPDATE SET "
                    "context_json = excluded.context_json, "
                    "home_score = excluded.home_score, "
                    "away_score = excluded.away_score",
                    (
                        sport, event_id, game_date_fmt,
                        home_team, away_team,
                        home_score, away_score,
                        json.dumps(context),
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
        client = _get_client()
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

            for team_data in players_data:
                team_name = team_data.get("team", {}).get("displayName", "")
                statistics = team_data.get("statistics", [])

                for stat_group in statistics:
                    stat_labels = stat_group.get("labels", [])
                    athletes = stat_group.get("athletes", [])

                    for athlete in athletes:
                        player_name = athlete.get("athlete", {}).get("displayName", "")
                        stats = athlete.get("stats", [])

                        if not player_name or not stats:
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
                        logger.info(f"Player stat insert failed for {player_name}/{stat_type}: {e}")

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
                    logger.info(f"PRA composite insert failed for {player_name}: {e}")

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
                        logger.info(f"Football stat insert failed for {player_name}/{key}: {e}")

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
        client = _get_client()

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
        client = _get_client()
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
        logger.info(f"Resolved {resolved}/{len(trades)} paper trades for {sport} on {game_date}")

        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": len(trades),
            "resolved": resolved,
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

        logger.info(
            f"Resolved {resolved}/{len(trades)} game-level paper trades "
            f"for {sport} on {game_date} ({unmatched} unmatched)"
        )

        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": len(trades),
            "resolved": resolved,
            "unmatched": unmatched,
        }

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

        client = _get_client()
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

            # Store enrichment data
            pbp_summary = {
                "total_plays": total_plays,
                "scoring_plays": len(scoring_plays),
                "periods": periods,
                "momentum_swings": sorted(
                    momentum_swings, key=lambda x: x["swing"], reverse=True
                )[:10],  # Top 10 biggest swings
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

            # Update existing game_context with PBP data
            cursor = await self._db.execute(
                "SELECT id, context_json FROM game_contexts "
                "WHERE sport = ? AND event_id = ?",
                (sport, event_id),
            )
            row = await cursor.fetchone()
            if row:
                ctx_id, ctx_json = row
                ctx = json.loads(ctx_json) if ctx_json else {}
                ctx["play_by_play"] = pbp_summary
                await self._db.execute(
                    "UPDATE game_contexts SET context_json = ? WHERE id = ?",
                    (json.dumps(ctx), ctx_id),
                )
                games_enriched += 1
                logger.debug(
                    f"PBP enrichment: {event_id} — {total_plays} plays, "
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
        Collect pitch-level Statcast data from Baseball Savant (free).

        118 columns per pitch: velocity, spin rate, exit velocity, launch angle,
        expected batting average, zone, pitch type, etc.

        Stores aggregated pitcher/batter stats in player_stats table.

        Args:
            start_date: YYYY-MM-DD format
            end_date: YYYY-MM-DD format (defaults to start_date)
            player_type: 'pitcher' or 'batter'
        """
        if end_date is None:
            end_date = start_date

        client = _get_client()
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
            resp = await client.get(url, params=params, timeout=60.0, follow_redirects=True)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Statcast fetch error: {e}")
            return {"error": str(e), "pitches": 0}

        content = resp.text
        if not content or len(content) < 100:
            return {"error": "Empty response from Baseball Savant", "pitches": 0}

        # Use csv module for proper quoted-field handling
        import csv
        import io

        reader = csv.DictReader(io.StringIO(content))
        # Clean BOM from header field names
        if reader.fieldnames:
            reader.fieldnames = [
                f.strip().strip('"').strip('\ufeff"').strip('\ufeff')
                for f in reader.fieldnames
            ]

        rows_list = list(reader)
        total_pitches = len(rows_list)
        if total_pitches == 0:
            return {"error": "No data rows", "pitches": 0}

        # Aggregate by pitcher for storage
        pitcher_stats = {}
        for row in rows_list:

            pitcher = (row.get("player_name") or "").strip()
            game_date = (row.get("game_date") or start_date).strip()
            if not pitcher:
                continue

            key = (pitcher, game_date)
            if key not in pitcher_stats:
                pitcher_stats[key] = {
                    "pitches": 0,
                    "strikeouts": 0,
                    "velocities": [],
                    "exit_velocities": [],
                    "events": [],
                    "pitcher_team": (
                        (row.get("home_team") or "").strip()
                        if (row.get("inning_topbot") or "") == "Bot"
                        else (row.get("away_team") or "").strip()
                    ),
                }

            stats = pitcher_stats[key]
            stats["pitches"] += 1

            # Aggregate key metrics
            try:
                vel = float(row.get("release_speed") or "0")
                if 50 < vel < 110:  # Sane velocity range (mph)
                    stats["velocities"].append(vel)
            except (ValueError, TypeError):
                pass

            try:
                ev = float(row.get("launch_speed") or "0")
                if 10 < ev < 130:  # Sane exit velocity range (mph)
                    stats["exit_velocities"].append(ev)
            except (ValueError, TypeError):
                pass

            event = (row.get("events") or "").strip()
            if event:
                stats["events"].append(event)
                if event in ("strikeout", "strikeout_double_play"):
                    stats["strikeouts"] += 1

        # Store aggregated stats — batched to reduce DB lock contention
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

        stored = 0
        if batch_rows:
            try:
                await self._db.executemany(
                    "INSERT OR IGNORE INTO player_stats "
                    "(sport, event_id, game_date, player_name, team, stat_type, stat_value) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    batch_rows,
                )
                stored = len(batch_rows)
            except Exception as e:
                logger.warning(f"Statcast batch insert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, operation="data_collector collect_statcast")
        logger.info(
            f"Statcast: {total_pitches} pitches from {len(pitcher_stats)} pitchers, "
            f"stored {stored} stat entries ({start_date} to {end_date})"
        )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "player_type": player_type,
            "total_pitches": total_pitches,
            "pitchers": len(pitcher_stats),
            "stats_stored": stored,
        }

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

        return stats
