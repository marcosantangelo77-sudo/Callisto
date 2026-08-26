"""
Baseball collectors: Baseball Savant Statcast pitches + MLB Stats API rosters.
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

async def collect_statcast(
    dc,
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
                await dc._db.executemany(INSERT_SQL, pitch_rows[start:start + CHUNK])
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
            await dc._db.executemany(
                "INSERT OR IGNORE INTO player_stats "
                "(sport, event_id, game_date, player_name, team, stat_type, stat_value) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch_rows,
            )
            stored_aggregates = len(batch_rows)
        except Exception as e:
            logger.warning(f"Statcast aggregate batch insert failed: {e}")

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_statcast")
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

async def collect_mlb_players(dc) -> dict:
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
            await dc._db.executemany(
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
    await commit_with_retry(dc._db, operation="data_collector collect_mlb_players")
    logger.info(
        f"MLB players: refreshed {stored} records across {len(teams)} teams"
    )
    return {
        "teams": len(teams),
        "roster_entries": len(unique_entries),
        "players_upserted": stored,
    }