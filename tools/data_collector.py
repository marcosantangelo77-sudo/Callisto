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


class DataCollector:
    """Collects game data from free sources for the embedding pipeline."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
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

            # Build context from available data
            context = {
                "home_score": home_score,
                "away_score": away_score,
                "total": home_score + away_score,
                "spread": home_score - away_score,
                "venue": comp.get("venue", {}).get("fullName", ""),
                "attendance": comp.get("attendance"),
            }

            # Extract headline/notes
            notes = comp.get("notes", [])
            if notes:
                context["notes"] = [n.get("headline", "") for n in notes[:3]]

            # Store game context
            try:
                await self._db.execute(
                    "INSERT OR IGNORE INTO game_contexts "
                    "(sport, event_id, game_date, home_team, away_team, "
                    "home_score, away_score, context_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sport, event_id, game_date_fmt,
                        home_team, away_team,
                        home_score, away_score,
                        json.dumps(context),
                    ),
                )
                games_stored += 1
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
                await self._db.execute(
                    "INSERT OR IGNORE INTO game_results "
                    "(sport, game_date, home_team, away_team, home_score, "
                    "away_score, total_score, spread_result, winner, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'espn')",
                    (
                        sport, game_date_fmt, home_team, away_team,
                        home_score, away_score, total_score, spread_result, winner,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to store game_result {event_id}: {e}")

        await self._db.commit()
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

        await self._db.commit()
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
                    except Exception:
                        pass

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
                except Exception:
                    pass

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
                    except Exception:
                        pass

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
            logger.debug(f"ESPN odds endpoint failed for {event_id}: {e}")

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
            logger.debug(f"ESPN probabilities endpoint failed for {event_id}: {e}")

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

            # Find matching stat
            stat_cursor = await self._db.execute(
                "SELECT stat_value FROM player_stats "
                "WHERE sport = ? AND game_date = ? "
                "AND player_name = ? AND stat_type = ?",
                (sport, game_date, player, stat_type),
            )
            stat_row = await stat_cursor.fetchone()

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

        await self._db.commit()
        logger.info(f"Resolved {resolved}/{len(trades)} paper trades for {sport} on {game_date}")

        return {
            "sport": sport,
            "game_date": game_date,
            "total_pending": len(trades),
            "resolved": resolved,
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
        await self._db.execute(
            "UPDATE game_contexts SET embedded = TRUE WHERE id = ?",
            (context_id,),
        )
        await self._db.commit()

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
