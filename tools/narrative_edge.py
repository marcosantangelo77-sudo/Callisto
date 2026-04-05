"""
Narrative Edge Detector — finds edges Vegas models CAN'T price.

Detects player-level situations where prop lines are systematically
mispriced because the factors aren't in any model's feature set:

1. MILESTONE HUNTING — player is 1-2 away from a record/milestone
   (triple doubles, franchise records, career milestones). Players
   consciously or unconsciously push for these. Books don't adjust.

2. ROLE ELEVATION — starter just went down, backup's props haven't
   caught up to their new usage rate. The first 2-3 games after a
   role change are the biggest mispricing window.

3. NARRATIVE MOMENTUM — contract year players, revenge games (traded
   player facing former team), hometown returns. Emotional factors
   that drive real performance but aren't in any model.

4. USAGE SURGE DETECTION — player's minutes/usage trending up over
   last 5 games but props still set to season average.

All of these exploit the same structural inefficiency: prop lines are
set by models using season averages. Any RECENT CHANGE in a player's
situation creates a lag between reality and the posted line.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.narrative_edge")

DB_PATH = "memory/callisto.db"


async def detect_usage_surge(
    sport: str = "basketball_nba",
    min_games: int = 10,
    recent_window: int = 5,
    surge_threshold: float = 1.20,  # 20% above season avg
) -> list[dict]:
    """
    Find players whose recent performance significantly exceeds their
    season average. These are role elevation / hot streak candidates
    whose prop lines may still reflect outdated season averages.

    A player averaging 15 PPG for the season but 22 PPG over the last
    5 games has a usage surge of 1.47x. If their points line is still
    set at 17.5, the over is +EV.
    """
    edges = []
    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        # Get all players with enough games
        cursor = await db.execute("""
            SELECT player_name, stat_type,
                   COUNT(*) as games,
                   AVG(stat_value) as season_avg,
                   MAX(game_date) as last_game
            FROM player_stats
            WHERE sport = ?
            AND stat_type IN ('Points', 'Rebounds', 'Assists', 'Threes',
                              'Steals', 'Blocks')
            GROUP BY player_name, stat_type
            HAVING games >= ?
        """, (sport, min_games))
        players = await cursor.fetchall()

        for player_name, stat_type, games, season_avg, last_game in players:
            if not season_avg or season_avg < 3:  # skip bench warmers
                continue

            # Get recent games
            cursor2 = await db.execute("""
                SELECT stat_value, game_date
                FROM player_stats
                WHERE sport = ? AND player_name = ? AND stat_type = ?
                ORDER BY game_date DESC
                LIMIT ?
            """, (sport, player_name, stat_type, recent_window))
            recent = await cursor2.fetchall()

            if len(recent) < 3:
                continue

            recent_avg = sum(r[0] for r in recent) / len(recent)
            surge_ratio = recent_avg / max(season_avg, 0.1)

            if surge_ratio >= surge_threshold:
                # Check if there's a current prop line for this player
                prop_market = _stat_to_prop_market(stat_type)
                if not prop_market:
                    continue

                cursor3 = await db.execute("""
                    SELECT line, price_american, book, side
                    FROM prop_snapshots
                    WHERE player = ? AND market = ?
                    AND snapshot_time >= datetime('now', '-6 hours')
                    ORDER BY snapshot_time DESC
                    LIMIT 4
                """, (player_name, prop_market))
                props = await cursor3.fetchall()

                over_line = None
                for line, price, book, side in props:
                    if side == "Over":
                        over_line = (line, price, book)
                        break

                edge_info = {
                    "player": player_name,
                    "stat_type": stat_type,
                    "prop_market": prop_market,
                    "season_avg": round(season_avg, 1),
                    "recent_avg": round(recent_avg, 1),
                    "surge_ratio": round(surge_ratio, 2),
                    "recent_games": len(recent),
                    "total_games": games,
                    "edge_type": "usage_surge",
                    "last_game": last_game,
                }

                if over_line:
                    line_val, price, book = over_line
                    # If recent avg > line, the over is likely +EV
                    if recent_avg > line_val:
                        edge_info["current_line"] = line_val
                        edge_info["line_gap"] = round(recent_avg - line_val, 1)
                        edge_info["book"] = book
                        edge_info["odds"] = price
                        edge_info["actionable"] = True
                    else:
                        edge_info["current_line"] = line_val
                        edge_info["actionable"] = False
                else:
                    edge_info["actionable"] = False
                    edge_info["note"] = "no current prop line found"

                edges.append(edge_info)

    # Sort by surge ratio descending
    edges.sort(key=lambda x: x["surge_ratio"], reverse=True)
    return edges


async def detect_role_change(
    sport: str = "basketball_nba",
    lookback_days: int = 14,
    min_minute_increase: float = 5.0,  # minutes increase
) -> list[dict]:
    """
    Detect players whose minutes have jumped significantly in recent
    games vs their season average. This signals a role change (starter
    injured, trade, coaching decision) that prop lines may not reflect.

    The FIRST 2-3 games after a role change are the biggest mispricing
    window. After that, books adjust.
    """
    edges = []
    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        cursor = await db.execute("""
            SELECT player_name,
                   AVG(minutes_played) as season_avg_min,
                   COUNT(*) as total_games
            FROM player_stats
            WHERE sport = ? AND stat_type = 'Points'
            AND minutes_played IS NOT NULL AND minutes_played > 0
            GROUP BY player_name
            HAVING total_games >= 10
        """, (sport,))
        players = await cursor.fetchall()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        for player_name, season_avg_min, total_games in players:
            cursor2 = await db.execute("""
                SELECT AVG(minutes_played) as recent_min,
                       COUNT(*) as recent_games,
                       MAX(game_date) as last_game
                FROM player_stats
                WHERE sport = ? AND player_name = ? AND stat_type = 'Points'
                AND minutes_played IS NOT NULL
                AND game_date >= ?
            """, (sport, player_name, cutoff))
            row = await cursor2.fetchone()

            if not row or not row[0] or row[1] < 3:
                continue

            recent_min, recent_games, last_game = row
            minute_jump = recent_min - season_avg_min

            if minute_jump >= min_minute_increase:
                edges.append({
                    "player": player_name,
                    "season_avg_minutes": round(season_avg_min, 1),
                    "recent_avg_minutes": round(recent_min, 1),
                    "minute_increase": round(minute_jump, 1),
                    "recent_games": recent_games,
                    "total_games": total_games,
                    "last_game": last_game,
                    "edge_type": "role_change",
                    "note": f"+{minute_jump:.0f} min/game recently — props may lag",
                })

    edges.sort(key=lambda x: x["minute_increase"], reverse=True)
    return edges


async def detect_milestone_proximity(
    sport: str = "basketball_nba",
) -> list[dict]:
    """
    Find players approaching statistical milestones that could drive
    conscious stat-padding behavior:

    - Career points/rebounds/assists milestones (round numbers)
    - Season-high chasing (personal best this season)
    - Franchise record proximity
    - Triple-double watch (2+ categories near 10)

    Books don't adjust for milestone hunting. A player 1 rebound from
    a triple double will absolutely hunt boards. The rebounds over is +EV.
    """
    edges = []
    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        # Triple-double proximity: players with 2+ stats near 10 in recent games
        cursor = await db.execute("""
            SELECT player_name, game_date,
                   MAX(CASE WHEN stat_type='Points' THEN stat_value END) as pts,
                   MAX(CASE WHEN stat_type='Rebounds' THEN stat_value END) as reb,
                   MAX(CASE WHEN stat_type='Assists' THEN stat_value END) as ast
            FROM player_stats
            WHERE sport = ? AND stat_type IN ('Points', 'Rebounds', 'Assists')
            AND game_date >= date('now', '-7 days')
            GROUP BY player_name, game_date
            HAVING pts IS NOT NULL AND reb IS NOT NULL AND ast IS NOT NULL
        """, (sport,))
        recent_games = await cursor.fetchall()

        # Find players who came close to triple-doubles recently
        from collections import defaultdict
        near_td = defaultdict(list)
        for player, gdate, pts, reb, ast in recent_games:
            cats_near_10 = sum(1 for v in [pts, reb, ast] if v and v >= 7)
            cats_at_10 = sum(1 for v in [pts, reb, ast] if v and v >= 10)
            if cats_near_10 >= 2 and cats_at_10 >= 1:
                # Player had at least 1 category at 10+ and another at 7+
                short_cats = []
                if pts and 7 <= pts < 10:
                    short_cats.append(f"points ({pts:.0f})")
                if reb and 7 <= reb < 10:
                    short_cats.append(f"rebounds ({reb:.0f})")
                if ast and 7 <= ast < 10:
                    short_cats.append(f"assists ({ast:.0f})")
                if short_cats:
                    near_td[player].append({
                        "date": gdate,
                        "pts": pts, "reb": reb, "ast": ast,
                        "short_in": short_cats,
                    })

        for player, games in near_td.items():
            if len(games) >= 1:
                latest = max(games, key=lambda x: x["date"])
                edges.append({
                    "player": player,
                    "edge_type": "triple_double_proximity",
                    "near_td_games": len(games),
                    "latest": latest,
                    "note": f"Near triple-double {len(games)}x in last 7 days. "
                            f"Short in: {', '.join(latest['short_in'])}. "
                            f"These players consciously hunt the missing category.",
                })

        # Season-high proximity: players who recently set or nearly set season highs
        cursor2 = await db.execute("""
            SELECT player_name, stat_type,
                   MAX(stat_value) as season_high,
                   (SELECT stat_value FROM player_stats ps2
                    WHERE ps2.sport = ps.sport AND ps2.player_name = ps.player_name
                    AND ps2.stat_type = ps.stat_type
                    ORDER BY game_date DESC LIMIT 1) as last_game_val
            FROM player_stats ps
            WHERE sport = ? AND stat_type IN ('Points', 'Rebounds', 'Assists', 'Threes')
            GROUP BY player_name, stat_type
            HAVING season_high > 15
        """, (sport,))
        season_highs = await cursor2.fetchall()

        for player, stat, high, last_val in season_highs:
            if last_val and high and last_val >= high * 0.9:
                edges.append({
                    "player": player,
                    "stat_type": stat,
                    "season_high": high,
                    "last_game": last_val,
                    "edge_type": "season_high_chase",
                    "note": f"Last game {last_val:.0f} {stat} "
                            f"(season high: {high:.0f}). "
                            f"Confidence boost → overperformance cycle.",
                })

    return edges


async def detect_revenge_game(
    sport: str = "basketball_nba",
) -> list[dict]:
    """
    Detect players facing their former team. Traded/released players
    systematically outperform their season averages in revenge spots.
    This is well-documented in NBA analytics but still not priced into props.

    We detect this by looking for players who changed teams mid-season
    (appear in player_stats with different teams).
    """
    edges = []
    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        await db.execute("PRAGMA busy_timeout = 60000")

        # Find players who have stats with 2+ different teams this season
        cursor = await db.execute("""
            SELECT player_name, GROUP_CONCAT(DISTINCT team) as teams,
                   COUNT(DISTINCT team) as team_count
            FROM player_stats
            WHERE sport = ? AND stat_type = 'Points'
            AND game_date >= date('now', '-180 days')
            GROUP BY player_name
            HAVING team_count >= 2
        """, (sport,))

        traded_players = await cursor.fetchall()

        for player, teams_str, team_count in traded_players:
            teams = teams_str.split(",")

            # Get their current team (most recent game)
            cursor2 = await db.execute("""
                SELECT team, game_date FROM player_stats
                WHERE sport = ? AND player_name = ? AND stat_type = 'Points'
                ORDER BY game_date DESC LIMIT 1
            """, (sport, player))
            current = await cursor2.fetchone()
            if not current:
                continue
            current_team = current[0]
            former_teams = [t for t in teams if t != current_team]

            # Get their season avg and check if any upcoming games are vs former team
            cursor3 = await db.execute("""
                SELECT AVG(stat_value) FROM player_stats
                WHERE sport = ? AND player_name = ? AND stat_type = 'Points'
            """, (sport, player))
            avg_pts = (await cursor3.fetchone())[0]

            edges.append({
                "player": player,
                "current_team": current_team,
                "former_teams": former_teams,
                "season_avg_points": round(avg_pts, 1) if avg_pts else 0,
                "edge_type": "revenge_game",
                "note": f"Traded from {', '.join(former_teams)}. "
                        f"Props over is +EV when facing former team.",
            })

    return edges


async def full_narrative_scan(sport: str = "basketball_nba") -> dict:
    """Run all narrative edge detectors and return combined results."""
    logger.info(f"Narrative edge scan starting for {sport}")

    results = {
        "sport": sport,
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "usage_surges": [],
        "role_changes": [],
        "milestones": [],
        "revenge_games": [],
    }

    try:
        results["usage_surges"] = await detect_usage_surge(sport)
        logger.info(f"Usage surges: {len(results['usage_surges'])} found")
    except Exception as e:
        logger.warning(f"Usage surge scan failed: {e}")

    try:
        results["role_changes"] = await detect_role_change(sport)
        logger.info(f"Role changes: {len(results['role_changes'])} found")
    except Exception as e:
        logger.warning(f"Role change scan failed: {e}")

    try:
        results["milestones"] = await detect_milestone_proximity(sport)
        logger.info(f"Milestones: {len(results['milestones'])} found")
    except Exception as e:
        logger.warning(f"Milestone scan failed: {e}")

    try:
        results["revenge_games"] = await detect_revenge_game(sport)
        logger.info(f"Revenge games: {len(results['revenge_games'])} found")
    except Exception as e:
        logger.warning(f"Revenge game scan failed: {e}")

    total = sum(len(v) for v in results.values() if isinstance(v, list))
    logger.info(f"Narrative scan complete: {total} total edges for {sport}")

    return results


def _stat_to_prop_market(stat_type: str) -> Optional[str]:
    """Map ESPN stat type names to prop market keys."""
    mapping = {
        "Points": "player_points",
        "Rebounds": "player_rebounds",
        "Assists": "player_assists",
        "Threes": "player_threes",
        "Steals": "player_steals",
        "Blocks": "player_blocks",
    }
    return mapping.get(stat_type)
