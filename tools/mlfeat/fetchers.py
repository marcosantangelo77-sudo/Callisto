"""Read-only SQL fetchers shared by the mlfeat feature builders."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Union


def _fetch_player_history(
    conn: sqlite3.Connection,
    sport: str,
    player: str,
    stat_type: str,
    asof_date: date,
    limit: int = 40,
) -> list[sqlite3.Row]:
    """All prior ``player_stats`` for this player+stat STRICTLY before asof_date.

    Ordered most-recent-first; the caller picks a window length off the front.
    """
    cur = conn.execute(
        """
        SELECT ps.game_date, ps.event_id, ps.team, ps.stat_value
          FROM player_stats ps
         WHERE ps.sport = ?
           AND ps.player_name = ?
           AND ps.stat_type = ?
           AND ps.game_date < ?
         ORDER BY ps.game_date DESC
         LIMIT ?
        """,
        (sport, player, stat_type, asof_date.isoformat(), limit),
    )
    return cur.fetchall()


def _fetch_opp_allowed(
    conn: sqlite3.Connection,
    sport: str,
    stat_type: str,
    opp_team: Optional[str],
    asof_date: date,
    last_n: int = 10,
) -> tuple[float, int]:
    """Mean stat allowed by ``opp_team`` over its last-N games before asof.

    Implementation: we don't have a per-team allowed stat table, but we can
    approximate by summing the opponent's opponents' player stats for this
    stat_type on prior game_dates where ``opp_team`` played. We treat each
    per-player stat as a contribution and average.

    Returns ``(mean_allowed, n_contributions)``. NaN / 0 if no history.
    """
    if not opp_team:
        return float("nan"), 0
    # Step 1: find last-N game_dates on which opp_team appeared (either side)
    # using game_results for reliability.
    cur = conn.execute(
        """
        SELECT COALESCE(local_game_date, game_date) AS gd,
               home_team, away_team, home_score, away_score
          FROM game_results
         WHERE sport = ?
           AND (home_team = ? OR away_team = ?)
           AND COALESCE(local_game_date, game_date) < ?
         ORDER BY COALESCE(local_game_date, game_date) DESC
         LIMIT ?
        """,
        (sport, opp_team, opp_team, asof_date.isoformat(), last_n),
    )
    games = cur.fetchall()
    if not games:
        return float("nan"), 0

    # For a crude "allowed" proxy for player stats: sum stat_values in
    # player_stats for games on those dates, excluding players on opp_team.
    date_set = tuple({g["gd"] for g in games if g["gd"]})
    if not date_set:
        return float("nan"), 0
    placeholders = ",".join("?" for _ in date_set)
    q = f"""
        SELECT stat_value
          FROM player_stats
         WHERE sport = ?
           AND stat_type = ?
           AND game_date IN ({placeholders})
           AND team != ?
    """
    cur = conn.execute(q, (sport, stat_type, *date_set, opp_team))
    vals = [r[0] for r in cur.fetchall() if r[0] is not None]
    if not vals:
        return float("nan"), 0
    return float(sum(vals) / len(vals)), len(vals)


def _fetch_event_context(
    conn: sqlite3.Connection,
    sport: str,
    event_id: str,
) -> dict:
    """Best-effort resolution of event metadata: venue, teams, local-tz hour."""
    cur = conn.execute(
        """
        SELECT gc.home_team, gc.away_team,
               COALESCE(gc.local_game_date, gc.game_date) AS gd,
               gc.context_json
          FROM game_contexts gc
         WHERE gc.sport = ? AND gc.event_id = ?
         LIMIT 1
        """,
        (sport, event_id),
    )
    row = cur.fetchone()
    out: dict = {
        "home_team": None,
        "away_team": None,
        "game_date": None,
        "venue": None,
        "attendance": None,
    }
    if row:
        out["home_team"] = row["home_team"]
        out["away_team"] = row["away_team"]
        out["game_date"] = row["gd"]
        if row["context_json"]:
            try:
                j = json.loads(row["context_json"])
                out["venue"] = j.get("venue")
                out["attendance"] = j.get("attendance")
            except Exception:
                pass
    # Backfill from backtest_events.snapshot_time if home_team missing
    if not out["home_team"]:
        cur = conn.execute(
            """
            SELECT model_factors, snapshot_time, local_game_date, game_date
              FROM backtest_events
             WHERE sport = ? AND event_id = ?
             LIMIT 1
            """,
            (sport, event_id),
        )
        r2 = cur.fetchone()
        if r2:
            try:
                mf = json.loads(r2["model_factors"] or "{}")
                out["home_team"] = mf.get("home_team") or out["home_team"]
                out["away_team"] = mf.get("away_team") or out["away_team"]
            except Exception:
                pass
            out["game_date"] = (
                out["game_date"] or r2["local_game_date"] or r2["game_date"]
            )
            out["snapshot_time"] = r2["snapshot_time"]
    return out


def _fetch_player_clv_deviation(
    conn: sqlite3.Connection,
    sport: str,
    player: str,
    stat_type: str,
    asof_date: date,
    last_n: int = 10,
) -> tuple[float, int]:
    """Average (actual_stat - line) for this player's last-N resolved prop
    events strictly before asof_date.

    Positive => player has recently been beating posted lines for this stat;
    negative => consistently under. Returns (mean_dev, n).
    """
    cur = conn.execute(
        """
        SELECT (be.actual_stat - be.line) AS dev
          FROM backtest_events be
         WHERE be.sport = ?
           AND be.player = ?
           AND be.market = ?
           AND be.actual_stat IS NOT NULL
           AND be.line IS NOT NULL
           AND COALESCE(be.local_game_date, be.game_date) < ?
           AND be.actual_result IN ('won','lost','push')
         ORDER BY COALESCE(be.local_game_date, be.game_date) DESC
         LIMIT ?
        """,
        (sport, player, stat_type, asof_date.isoformat(), last_n),
    )
    devs = [r["dev"] for r in cur.fetchall() if r["dev"] is not None]
    if not devs:
        return float("nan"), 0
    return float(sum(devs) / len(devs)), len(devs)


def _pitcher_handedness(
    conn: sqlite3.Connection, event_id: str
) -> Optional[str]:
    """Starting pitcher throws hand for an MLB game, if we have statcast data."""
    cur = conn.execute(
        """
        SELECT pitcher_throws
          FROM statcast_pitches
         WHERE game_pk = ?
         ORDER BY at_bat_number ASC, pitch_number ASC
         LIMIT 1
        """,
        (event_id,),
    )
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return None


def _batter_stands(
    conn: sqlite3.Connection, player: str
) -> Optional[str]:
    cur = conn.execute(
        """
        SELECT batter_stands
          FROM statcast_pitches
         WHERE batter_name = ?
         ORDER BY game_date DESC
         LIMIT 1
        """,
        (player,),
    )
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return None


# ──────────────────────────────────────────────────────────────────────────
# Game-total oriented fetchers
# ──────────────────────────────────────────────────────────────────────────

def _team_recent_totals(
    conn: sqlite3.Connection,
    sport: str,
    team: str,
    asof_d: date,
    last_n: int = 10,
) -> list[int]:
    cur = conn.execute(
        """
        SELECT total_score
          FROM game_results
         WHERE sport = ?
           AND (home_team = ? OR away_team = ?)
           AND COALESCE(local_game_date, game_date) < ?
           AND total_score IS NOT NULL
         ORDER BY COALESCE(local_game_date, game_date) DESC
         LIMIT ?
        """,
        (sport, team, team, asof_d.isoformat(), last_n),
    )
    return [r[0] for r in cur.fetchall() if r[0] is not None]


def _team_games_in_window(
    conn: sqlite3.Connection,
    sport: str,
    team: str,
    asof_d: date,
    window_days: int,
) -> int:
    start = (asof_d - timedelta(days=window_days)).isoformat()
    cur = conn.execute(
        """
        SELECT COUNT(*) FROM game_results
         WHERE sport = ?
           AND (home_team = ? OR away_team = ?)
           AND COALESCE(local_game_date, game_date) >= ?
           AND COALESCE(local_game_date, game_date) < ?
        """,
        (sport, team, team, start, asof_d.isoformat()),
    )
    return int(cur.fetchone()[0] or 0)


def _team_last_game_date(
    conn: sqlite3.Connection, sport: str, team: str, asof_d: date
) -> Optional[date]:
    from datetime import date as _date

    cur = conn.execute(
        """
        SELECT COALESCE(local_game_date, game_date) AS gd
          FROM game_results
         WHERE sport = ?
           AND (home_team = ? OR away_team = ?)
           AND COALESCE(local_game_date, game_date) < ?
         ORDER BY gd DESC
         LIMIT 1
        """,
        (sport, team, team, asof_d.isoformat()),
    )
    row = cur.fetchone()
    if row and row["gd"]:
        try:
            return _date.fromisoformat(row["gd"][:10])
        except Exception:
            return None
    return None


def _lineup_recent_mean(
    conn: sqlite3.Connection,
    sport: str,
    team: str,
    asof_d: date,
    last_n_games: int = 10,
) -> float:
    """Mean per-game total of a primary scoring stat for recent roster."""
    primary_stat = {
        "basketball_nba": "points",
        "basketball_ncaab": "points",
        "basketball_ncaaw": "points",
        "basketball_wnba": "points",
        "icehockey_nhl": "points",
        "baseball_mlb": "hits",
        "americanfootball_nfl": "passing_yards",
    }.get(sport, "points")
    cur = conn.execute(
        """
        SELECT ps.event_id, SUM(ps.stat_value) AS team_total
          FROM player_stats ps
         WHERE ps.sport = ?
           AND ps.team = ?
           AND ps.stat_type = ?
           AND ps.game_date < ?
         GROUP BY ps.event_id
         ORDER BY MAX(ps.game_date) DESC
         LIMIT ?
        """,
        (sport, team, primary_stat, asof_d.isoformat(), last_n_games),
    )
    vals = [r["team_total"] for r in cur.fetchall() if r["team_total"] is not None]
    if not vals:
        return float("nan")
    return float(sum(vals) / len(vals))


def _line_movement_features(
    conn: sqlite3.Connection,
    sport: str,
    event_id: str,
    asof_ts,
) -> tuple[float, float, int]:
    """Aggregate total-market line movements for this event before asof."""
    # line_movements doesn't have event_id — use team + market + detected_at.
    # Safest: no-op unless we have teams; return (0,0,0).
    # We look up teams from event context.
    try:
        cur = conn.execute(
            """
            SELECT home_team, away_team FROM game_contexts
             WHERE sport=? AND event_id=? LIMIT 1
            """,
            (sport, event_id),
        )
        row = cur.fetchone()
        if not row:
            return 0.0, 0.0, 0
        team = row["home_team"]
        if isinstance(asof_ts, (datetime, date)):
            asof_iso = asof_ts.isoformat() if isinstance(asof_ts, datetime) else asof_ts.isoformat()
        else:
            asof_iso = str(asof_ts)
        cur = conn.execute(
            """
            SELECT point_movement FROM line_movements
             WHERE sport = ? AND team = ? AND market = 'totals'
               AND detected_at < ?
             ORDER BY detected_at DESC
             LIMIT 20
            """,
            (sport, team, asof_iso),
        )
        moves = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
        if not moves:
            return 0.0, 0.0, 0
        net = sum(moves)
        absmag = sum(abs(m) for m in moves)
        return float(net), float(absmag), len(moves)
    except sqlite3.Error:
        return 0.0, 0.0, 0
