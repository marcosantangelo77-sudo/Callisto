"""Game-total feature builder (``build_game_total_features``)."""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import date, datetime
from typing import Optional, Union

import numpy as np

from tools.game_dates import (
    is_day_game,
    local_day_of_week,
    local_hour_of_day,
)

from tools.mlfeat.base import (
    FeatureVector,
    _altitude_factor,
    _asof_date,
    _is_dome,
    _mean,
    _open_ro,
    _park_factor,
    _safe_stdev,
)
from tools.mlfeat.fetchers import (
    _fetch_event_context,
    _fetch_opp_allowed,
    _line_movement_features,
    _lineup_recent_mean,
    _team_games_in_window,
    _team_last_game_date,
    _team_recent_totals,
)

logger = logging.getLogger("callisto.ml_features")


_GAME_FEATURE_NAMES: tuple[str, ...] = (
    # Lineup-strength proxies (mean of home/away player stats last N games)
    "home_lineup_recent_ppg",
    "away_lineup_recent_ppg",
    "home_lineup_recent_opp_allowed",
    "away_lineup_recent_opp_allowed",
    "lineup_delta",
    # Venue
    "park_factor",
    "is_dome",
    "venue_altitude_factor",
    # Pace / density
    "home_games_in_last_4",
    "away_games_in_last_4",
    "home_days_rest",
    "away_days_rest",
    "home_back_to_back",
    "away_back_to_back",
    # Local time / seasonality
    "local_hour",
    "is_day_game",
    *[f"dow_{i}" for i in range(7)],
    *[f"month_{i}" for i in range(1, 13)],
    "week_of_season",
    # Recent totals history for BOTH teams
    "home_recent_total_mean",
    "away_recent_total_mean",
    "home_recent_total_std",
    "away_recent_total_std",
    # Line movement features
    "total_line_movement",
    "total_line_movement_abs",
    "n_line_movements",
)


def feature_names_game_total() -> list[str]:
    return list(_GAME_FEATURE_NAMES)


def _season_week(
    asof_d: date, sport: str
) -> float:
    """Cheap week-of-season proxy — days from sport's canonical season start."""
    starts = {
        "baseball_mlb": date(asof_d.year, 3, 27),
        "basketball_nba": date(asof_d.year if asof_d.month >= 10 else asof_d.year - 1, 10, 20),
        "icehockey_nhl": date(asof_d.year if asof_d.month >= 10 else asof_d.year - 1, 10, 8),
        "americanfootball_nfl": date(asof_d.year if asof_d.month >= 9 else asof_d.year - 1, 9, 5),
    }
    start = starts.get(sport, date(asof_d.year, 1, 1))
    delta = (asof_d - start).days
    return max(0.0, float(delta // 7))


def build_game_total_features(
    event_id: str,
    asof_ts: Union[str, datetime, date],
    sport: str = "basketball_nba",
    conn: Optional[sqlite3.Connection] = None,
) -> FeatureVector:
    """Feature vector for a game-total prediction.

    Target (``target_stat_value``) is filled with the realised total score
    if the game is already resolved in ``game_results``.
    """
    close_after = conn is None
    if conn is None:
        conn = _open_ro()

    try:
        asof_d = _asof_date(asof_ts)
        ctx = _fetch_event_context(conn, sport, event_id)
        home = ctx.get("home_team")
        away = ctx.get("away_team")
        venue = ctx.get("venue")
        snapshot_time = ctx.get("snapshot_time")

        # Lineup strength proxies
        home_lineup = _lineup_recent_mean(conn, sport, home or "", asof_d)
        away_lineup = _lineup_recent_mean(conn, sport, away or "", asof_d)
        # "Allowed" proxy = the opponent-average for the team in their prior games.
        opp_mean_h, _ = _fetch_opp_allowed(
            conn, sport, "points" if sport != "baseball_mlb" else "hits",
            home, asof_d, last_n=10,
        )
        opp_mean_a, _ = _fetch_opp_allowed(
            conn, sport, "points" if sport != "baseball_mlb" else "hits",
            away, asof_d, last_n=10,
        )
        lineup_delta = (home_lineup - away_lineup) if (
            not math.isnan(home_lineup) and not math.isnan(away_lineup)
        ) else float("nan")

        # Venue
        pf = _park_factor(venue)
        dome = _is_dome(venue)
        alt_factor = _altitude_factor(venue)

        # Pace / schedule density
        home_g4 = _team_games_in_window(conn, sport, home or "", asof_d, 4)
        away_g4 = _team_games_in_window(conn, sport, away or "", asof_d, 4)
        home_last = _team_last_game_date(conn, sport, home or "", asof_d)
        away_last = _team_last_game_date(conn, sport, away or "", asof_d)
        home_days_rest = float((asof_d - home_last).days) if home_last else float("nan")
        away_days_rest = float((asof_d - away_last).days) if away_last else float("nan")
        home_b2b = 1.0 if home_days_rest == 1.0 else 0.0 if not math.isnan(home_days_rest) else 0.0
        away_b2b = 1.0 if away_days_rest == 1.0 else 0.0 if not math.isnan(away_days_rest) else 0.0

        # Local time / seasonality
        local_hr = float("nan")
        day_game_val = float("nan")
        dow: Optional[int] = None
        game_month: Optional[int] = None
        game_d_iso = ctx.get("game_date")
        if snapshot_time and home:
            try:
                h = local_hour_of_day(snapshot_time, sport, home)
                if h is not None:
                    local_hr = float(h)
                dow = local_day_of_week(snapshot_time, sport, home)
                dg = is_day_game(snapshot_time, sport, home)
                if dg is not None:
                    day_game_val = 1.0 if dg else 0.0
            except Exception:
                pass
        if dow is None and game_d_iso:
            try:
                dow = date.fromisoformat(game_d_iso[:10]).weekday()
            except Exception:
                pass
        if game_d_iso:
            try:
                game_month = date.fromisoformat(game_d_iso[:10]).month
            except Exception:
                pass

        dow_onehot = [0.0] * 7
        if dow is not None and 0 <= dow < 7:
            dow_onehot[dow] = 1.0
        month_onehot = [0.0] * 12
        if game_month is not None and 1 <= game_month <= 12:
            month_onehot[game_month - 1] = 1.0

        week_n = _season_week(asof_d, sport)

        # Recent-total histories
        h_totals = _team_recent_totals(conn, sport, home or "", asof_d)
        a_totals = _team_recent_totals(conn, sport, away or "", asof_d)
        h_mean = _mean(h_totals)
        a_mean = _mean(a_totals)
        h_std = _safe_stdev(h_totals)
        a_std = _safe_stdev(a_totals)

        # Line movements
        mv_net, mv_abs, mv_n = _line_movement_features(conn, sport, event_id, asof_ts)

        # Target — realised total
        cur = conn.execute(
            """
            SELECT total_score FROM game_results
             WHERE sport=? AND home_team=? AND away_team=?
               AND (game_date=? OR local_game_date=?)
             LIMIT 1
            """,
            (sport, home, away, game_d_iso, game_d_iso),
        )
        row = cur.fetchone()
        target = float(row[0]) if (row and row[0] is not None) else None

        values = np.array(
            [
                home_lineup,
                away_lineup,
                opp_mean_h,
                opp_mean_a,
                lineup_delta,
                pf,
                float(dome),
                alt_factor,
                float(home_g4),
                float(away_g4),
                home_days_rest,
                away_days_rest,
                home_b2b,
                away_b2b,
                local_hr,
                day_game_val,
                *dow_onehot,
                *month_onehot,
                week_n,
                h_mean,
                a_mean,
                h_std,
                a_std,
                mv_net,
                mv_abs,
                float(mv_n),
            ],
            dtype=float,
        )

        return FeatureVector(
            names=_GAME_FEATURE_NAMES,
            values=values,
            target_stat_value=target,
            meta={
                "event_id": event_id,
                "sport": sport,
                "asof": asof_d.isoformat(),
                "venue": venue,
                "home_team": home,
                "away_team": away,
            },
        )
    finally:
        if close_after:
            conn.close()
