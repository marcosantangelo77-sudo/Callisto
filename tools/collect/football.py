"""
NFL collectors: nflverse-data release CSVs — rosters, combine, play-by-play.
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

NFLFASTR_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

async def collect_nfl_players(dc, season: Optional[int] = None) -> dict:
    """Refresh nfl_players from nflverse seasonal roster CSV."""
    import csv
    import io as _io
    if season is None:
        season = datetime.now(timezone.utc).year
    url = f"{NFLFASTR_BASE}/rosters/roster_{season}.csv"
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_nfl_players")
    logger.info(f"NFL roster {season}: {stored} players upserted")
    return {"season": season, "players_upserted": stored}

async def collect_nfl_combine(dc, start_year: int = 2000) -> dict:
    """Refresh nfl_combine_results from nflverse combine CSV."""
    import csv
    import io as _io
    url = f"{NFLFASTR_BASE}/combine/combine.csv"
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_nfl_combine")
    logger.info(f"NFL combine: {stored} rows upserted (since {start_year})")
    return {"rows_upserted": stored}

async def collect_nfl_plays(dc, season: Optional[int] = None) -> dict:
    """Stream-ingest nflfastR per-season play_by_play CSV into nfl_play_events."""
    import csv
    import io as _io
    if season is None:
        season = datetime.now(timezone.utc).year
    url = f"{NFLFASTR_BASE}/pbp/play_by_play_{season}.csv"
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
                await dc._db.executemany(INSERT_SQL, rows[start:start + CHUNK])
                stored += len(rows[start:start + CHUNK])
            except Exception as e:
                logger.warning(f"nfl_play_events batch insert failed: {e}")

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_nfl_plays")
    logger.info(f"NFL PBP {season}: {stored} plays stored")
    return {"season": season, "plays_stored": stored}