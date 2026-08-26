"""
NHL collectors: api-web.nhle.com rosters + per-shot play-by-play events.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client


logger = logging.getLogger("callisto.data_collector")

NHL_API = "https://api-web.nhle.com/v1"

async def collect_nhl_players(dc) -> dict:
    """Refresh the nhl_players table from api.nhle.com.

    For each of the 32 teams we pull /roster/{abbr}/current (active
    players) and then /player/{id}/landing for bio fields (height,
    weight, shoots, birth, draft). INSERT OR REPLACE keyed on player_id.
    """
    client = await _get_client()
    try:
        r = await client.get(
            f"{NHL_API}/standings/now",
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
                f"{NHL_API}/roster/{team['abbr']}/current",
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
                f"{NHL_API}/player/{pid}/landing",
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_nhl_players")
    logger.info(f"NHL players: refreshed {stored} across {len(teams)} teams")
    return {"teams": len(teams), "players_upserted": stored}

async def collect_nhl_shots(dc, date: Optional[str] = None) -> dict:
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
            f"{NHL_API}/schedule/{date}",
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
                f"{NHL_API}/gamecenter/{gid}/play-by-play",
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
                await dc._db.executemany(INSERT_SQL, all_shot_rows[start:start + CHUNK])
                stored += len(all_shot_rows[start:start + CHUNK])
            except Exception as e:
                logger.warning(f"nhl_shot_events batch insert failed: {e}")

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_nhl_shots")
    logger.info(f"NHL shots {date}: {len(game_ids)} games → {stored} shot rows")
    return {"date": date, "games": len(game_ids), "shots_stored": stored}