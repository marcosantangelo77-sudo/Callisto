"""
ESPN site-API collectors: scoreboard scores, box scores, player stats.

Endpoints used (public, no auth):
  - scoreboard: live/recent scores
  - summary/boxscore: full player stats per game

"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client
from tools.collect.venues import _get_venue_metadata


logger = logging.getLogger("callisto.data_collector")

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



async def collect_scores(
    dc,
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
                prev_cursor = await dc._db.execute(
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

        # Canonical local-tz date (tools.game_dates). ESPN's per-event
        # ``date`` is the UTC commence time, so we can derive the venue-
        # local date without relying on the ET-oriented ``dates`` param.
        from tools.game_dates import local_game_date as _lgd
        event_utc = event.get("date", "")
        local_date = _lgd(event_utc, sport, home_team)
        local_date_str = local_date.isoformat() if local_date else None

        # Store game context — only overwrite if the new context is richer
        # (more keys) than the existing one. Prevents sparse re-collections
        # from regressing enriched data with officials/rest/broadcasts.
        try:
            from tools.db_utils import execute_with_retry
            context_json = json.dumps(context)
            await execute_with_retry(
                dc._db,
                "INSERT INTO game_contexts "
                "(sport, event_id, game_date, local_game_date, "
                "home_team, away_team, "
                "home_score, away_score, context_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sport, event_id) DO UPDATE SET "
                "context_json = CASE "
                "  WHEN length(excluded.context_json) >= length(context_json) "
                "    THEN excluded.context_json "
                "  ELSE context_json "
                "END, "
                "local_game_date = COALESCE(excluded.local_game_date, local_game_date), "
                "home_score = COALESCE(excluded.home_score, home_score), "
                "away_score = COALESCE(excluded.away_score, away_score)",
                (
                    sport, event_id, game_date_fmt, local_date_str,
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
                dc._db,
                "INSERT OR IGNORE INTO game_results "
                "(sport, game_date, local_game_date, home_team, away_team, "
                "home_score, away_score, total_score, spread_result, winner, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'espn')",
                (
                    sport, game_date_fmt, local_date_str,
                    home_team, away_team,
                    home_score, away_score, total_score, spread_result, winner,
                ),
                operation="data_collector store_game_result",
            )
        except Exception as e:
            logger.warning(f"Failed to store game_result {event_id}: {e}")

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_scores")
    logger.info(f"Collected {games_stored} games for {sport} on {date}")

    return {
        "sport": sport,
        "date": game_date_fmt,
        "total_events": len(events),
        "completed": games_stored,
    }


async def collect_box_scores(
    dc,
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
                    stored = await store_player_stats(
                        dc,
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
    await commit_with_retry(dc._db, operation="data_collector collect_box_scores")
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

async def store_player_stats(
    dc,
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
                    await dc._db.execute(
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
                    dc._player_stat_insert_failures += 1
                    logger.warning(
                        f"Player stat insert failed for "
                        f"{player_name}/{stat_type}: {e!r} "
                        f"(total insert failures: "
                        f"{dc._player_stat_insert_failures})"
                    )

        # Composite: PRA
        pts = float(stat_map.get("PTS", 0) or 0)
        reb = float(stat_map.get("REB", 0) or 0)
        ast = float(stat_map.get("AST", 0) or 0)
        pra = pts + reb + ast
        if pra > 0:
            try:
                await dc._db.execute(
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
                dc._player_stat_insert_failures += 1
                logger.warning(
                    f"PRA composite insert failed for {player_name}: "
                    f"{e!r} (total insert failures: "
                    f"{dc._player_stat_insert_failures})"
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
                    await dc._db.execute(
                        "INSERT OR IGNORE INTO player_stats "
                        "(sport, event_id, game_date, player_name, team, "
                        "stat_type, stat_value) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (sport, event_id, game_date, player_name, team, key, val),
                    )
                    count += 1
                except Exception as e:
                    dc._player_stat_insert_failures += 1
                    logger.warning(
                        f"Football stat insert failed for "
                        f"{player_name}/{key}: {e!r} "
                        f"(total insert failures: "
                        f"{dc._player_stat_insert_failures})"
                    )

    return count

async def get_today_event_ids(sport: str) -> list[str]:
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

async def collect_date_range(
    dc,
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

        scores = await collect_scores(sport, date_str)
        total_games += scores.get("completed", 0)

        box = await collect_box_scores(sport, date_str)
        total_players += box.get("player_stat_entries", 0)

        dates_processed += 1

    return {
        "sport": sport,
        "dates_processed": dates_processed,
        "total_games": total_games,
        "total_player_entries": total_players,
    }