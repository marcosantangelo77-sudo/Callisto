"""
Basketball collectors: stats.nba.com rosters/shots + NCAA hoop box scores.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client
from tools.collect.espn import ESPN_BASE


logger = logging.getLogger("callisto.data_collector")

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

NCAA_BBALL_LEAGUES = {
    "basketball_ncaab": ("basketball", "mens-college-basketball"),
    "basketball_ncaaw": ("basketball", "womens-college-basketball"),
}

async def collect_nba_players(dc, season: Optional[str] = None) -> dict:
    """Refresh nba_players from stats.nba.com commonallplayers."""
    import httpx as _httpx
    client = _httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, max_redirects=5,
        headers=NBA_HEADERS,
    )
    try:
        if season is None:
            now = datetime.now(timezone.utc)
            y0 = now.year if now.month >= 10 else now.year - 1
            season = f"{y0}-{str(y0 + 1)[-2:]}"
        params = {"IsOnlyCurrentSeason": "1", "LeagueID": "00", "Season": season}
        try:
            r = await client.get(
                f"{NBA_STATS_BASE}/commonallplayers",
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
                await dc._db.executemany(
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
        await commit_with_retry(dc._db, operation="data_collector collect_nba_players")
        logger.info(f"NBA players {season}: {stored} upserted")
        return {"season": season, "players_upserted": stored}
    finally:
        await client.aclose()

async def collect_nba_shots(dc, date: Optional[str] = None) -> dict:
    """Per-shot events from stats.nba.com shotchartdetail for games on `date`."""
    import httpx as _httpx
    from asyncio import sleep as _sleep
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    client = _httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, max_redirects=5,
        headers=NBA_HEADERS,
    )
    try:
        try:
            r = await client.get(
                f"{NBA_STATS_BASE}/scoreboardv3",
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
                    f"{NBA_STATS_BASE}/shotchartdetail", params=params,
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
                    await dc._db.executemany(INSERT_SQL, all_rows[start:start + CHUNK])
                    stored += len(all_rows[start:start + CHUNK])
                except Exception as e:
                    logger.warning(f"nba_shot_events batch insert failed: {e}")

        from tools.db_utils import commit_with_retry
        await commit_with_retry(dc._db, operation="data_collector collect_nba_shots")
        logger.info(f"NBA shots {date}: {len(game_ids)} games → {stored} shots")
        return {"date": date, "games": len(game_ids), "shots_stored": stored}
    finally:
        await client.aclose()

async def collect_ncaa_basketball_players(dc, sport: str) -> dict:
    """Refresh ncaa_basketball_players for a given sport."""
    if sport not in NCAA_BBALL_LEAGUES:
        return {"error": f"unsupported sport: {sport}", "players": 0}
    category, league = NCAA_BBALL_LEAGUES[sport]
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_ncaa_basketball_players")
    logger.info(f"{sport}: {stored} players across {len(teams)} teams")
    return {"sport": sport, "teams": len(teams), "players_upserted": stored}

async def collect_ncaa_basketball_game_stats(dc, sport: str, date: Optional[str] = None) -> dict:
    """Fetch ESPN boxscores for completed NCAA games on `date` and
    upsert per-player per-game stats."""
    if sport not in NCAA_BBALL_LEAGUES:
        return {"error": f"unsupported sport: {sport}", "rows": 0}
    category, league = NCAA_BBALL_LEAGUES[sport]
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_ncaa_basketball_game_stats")
    logger.info(
        f"{sport} boxscores {date}: {len(completed)} games → {stored} rows"
    )
    return {"sport": sport, "games": len(completed), "rows_stored": stored}