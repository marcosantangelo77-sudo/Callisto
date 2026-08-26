"""Player-prop feature builder (``build_player_prop_features``)."""
from __future__ import annotations

import logging
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
    _asof_date,
    _is_dome,
    _mean,
    _open_ro,
    _park_factor,
    _safe_stdev,
    _trend_slope,
)
from tools.mlfeat.fetchers import (
    _batter_stands,
    _fetch_event_context,
    _fetch_opp_allowed,
    _fetch_player_clv_deviation,
    _fetch_player_history,
    _pitcher_handedness,
)

logger = logging.getLogger("callisto.ml_features")


_PLAYER_ROLL_WINDOWS: tuple[int, ...] = (5, 10, 20)

_PLAYER_FEATURE_NAMES: tuple[str, ...] = (
    # Rolling recent stat features (mean/std/slope for each window)
    *[f"p_roll{w}_mean" for w in _PLAYER_ROLL_WINDOWS],
    *[f"p_roll{w}_std" for w in _PLAYER_ROLL_WINDOWS],
    *[f"p_roll{w}_slope" for w in _PLAYER_ROLL_WINDOWS],
    "p_games_sampled",            # how many prior games fed the rolling stats
    # Opponent defensive rate
    "opp_last10_allowed_mean",
    "opp_last10_allowed_count",
    # Park / venue
    "park_factor",
    "is_dome",
    # Timing / rest
    "days_rest",
    "local_hour",
    "is_day_game",
    # Day-of-week one-hot (Mon=0 .. Sun=6)
    *[f"dow_{i}" for i in range(7)],
    # Month one-hot (1..12)
    *[f"month_{i}" for i in range(1, 13)],
    # Handedness match (batter vs pitcher handedness if known; else NaN)
    "pitcher_is_rhp",
    "handedness_match",
    # Rolling CLV / line-beat deviation
    "clv_dev_last10",
    "clv_dev_count",
)


def feature_names_player_prop() -> list[str]:
    return list(_PLAYER_FEATURE_NAMES)


def build_player_prop_features(
    player: str,
    stat_type: str,
    event_id: str,
    asof_ts: Union[str, datetime, date],
    sport: str = "basketball_nba",
    conn: Optional[sqlite3.Connection] = None,
) -> FeatureVector:
    """Build an ordered feature vector for a player-prop prediction.

    Parameters
    ----------
    player : canonical player name as stored in ``player_stats.player_name``
    stat_type : ``player_stats.stat_type`` key (e.g. ``'points'``, ``'strikeouts'``)
    event_id : ESPN event id or equivalent — used to resolve venue/home_team
    asof_ts : training cutoff. Rolling windows strictly exclude this date.
    sport : odds-api sport key, e.g. ``basketball_nba`` or ``baseball_mlb``
    conn : optional sqlite connection; opened read-only if omitted.

    Returns
    -------
    FeatureVector whose ``names`` align with ``feature_names_player_prop()``.
    ``target_stat_value`` is filled when the actual stat is already resolved
    in ``player_stats`` for this (player, event_id, stat_type).
    """
    close_after = conn is None
    if conn is None:
        conn = _open_ro()

    try:
        asof_d = _asof_date(asof_ts)

        # Event context — venue, home team, day/night
        ctx = _fetch_event_context(conn, sport, event_id)
        home = ctx.get("home_team")
        away = ctx.get("away_team")
        venue = ctx.get("venue")

        # Rolling stats
        history = _fetch_player_history(
            conn, sport, player, stat_type, asof_d,
            limit=max(_PLAYER_ROLL_WINDOWS) * 2,
        )
        hist_vals = [float(r["stat_value"]) for r in history if r["stat_value"] is not None]

        roll_means = []
        roll_stds = []
        roll_slopes = []
        for w in _PLAYER_ROLL_WINDOWS:
            window = hist_vals[:w]
            roll_means.append(_mean(window) if window else float("nan"))
            roll_stds.append(_safe_stdev(window) if window else float("nan"))
            # Slope over window in chronological order (oldest first)
            roll_slopes.append(_trend_slope(list(reversed(window))))

        games_sampled = float(len(hist_vals))

        # Opponent defensive rate — pick opp team as the team the player is NOT on.
        player_team = history[0]["team"] if history else None
        if home and away and player_team:
            opp_team = away if player_team == home else home
        else:
            opp_team = None
        opp_mean, opp_n = _fetch_opp_allowed(
            conn, sport, stat_type, opp_team, asof_d, last_n=10
        )

        # Park / dome
        pf = _park_factor(venue) if (sport == "baseball_mlb" or home == player_team) else 1.0
        dome = _is_dome(venue)

        # Days rest for player — distance between asof_d and most-recent prior game
        if history and history[0]["game_date"]:
            try:
                prev_d = date.fromisoformat(history[0]["game_date"][:10])
                days_rest = float((asof_d - prev_d).days)
            except Exception:
                days_rest = float("nan")
        else:
            days_rest = float("nan")

        # Local hour / day-of-week / month — derive from snapshot_time if we can,
        # else from the game_date midnight (0). snapshot_time is UTC commence.
        snapshot_time = ctx.get("snapshot_time")
        local_hr: Optional[int] = None
        dow: Optional[int] = None
        day_game: Optional[bool] = None
        if snapshot_time and home:
            try:
                local_hr = local_hour_of_day(snapshot_time, sport, home)
                dow = local_day_of_week(snapshot_time, sport, home)
                day_game = is_day_game(snapshot_time, sport, home)
            except Exception:
                pass
        game_d_iso = ctx.get("game_date")
        if dow is None and game_d_iso:
            try:
                dow = date.fromisoformat(game_d_iso[:10]).weekday()
            except Exception:
                pass
        dow_onehot = [0.0] * 7
        if dow is not None and 0 <= dow < 7:
            dow_onehot[dow] = 1.0

        month_onehot = [0.0] * 12
        game_month: Optional[int] = None
        if game_d_iso:
            try:
                game_month = date.fromisoformat(game_d_iso[:10]).month
            except Exception:
                pass
        if game_month is not None and 1 <= game_month <= 12:
            month_onehot[game_month - 1] = 1.0

        # Handedness — only meaningful for MLB batter props
        pitcher_is_rhp = float("nan")
        handedness_match = float("nan")
        if sport == "baseball_mlb":
            p_hand = _pitcher_handedness(conn, event_id)
            b_hand = _batter_stands(conn, player)
            if p_hand:
                pitcher_is_rhp = 1.0 if p_hand == "R" else 0.0
            if p_hand and b_hand:
                # match = batter opposite-handed from pitcher (platoon advantage)
                handedness_match = 1.0 if p_hand != b_hand else 0.0

        # Rolling CLV deviation (how player's recent results compare to lines)
        clv_dev, clv_n = _fetch_player_clv_deviation(
            conn, sport, player, stat_type, asof_d, last_n=10
        )

        # Assemble target stat value if already resolved for this event
        cur = conn.execute(
            """SELECT stat_value FROM player_stats
                WHERE sport=? AND event_id=? AND player_name=? AND stat_type=?""",
            (sport, event_id, player, stat_type),
        )
        row = cur.fetchone()
        target = float(row["stat_value"]) if row and row["stat_value"] is not None else None

        values = np.array(
            [
                *roll_means,
                *roll_stds,
                *roll_slopes,
                games_sampled,
                opp_mean,
                float(opp_n),
                pf,
                float(dome),
                days_rest,
                float(local_hr) if local_hr is not None else float("nan"),
                1.0 if day_game else (0.0 if day_game is False else float("nan")),
                *dow_onehot,
                *month_onehot,
                pitcher_is_rhp,
                handedness_match,
                clv_dev,
                float(clv_n),
            ],
            dtype=float,
        )

        return FeatureVector(
            names=_PLAYER_FEATURE_NAMES,
            values=values,
            target_stat_value=target,
            meta={
                "player": player,
                "stat_type": stat_type,
                "event_id": event_id,
                "sport": sport,
                "asof": asof_d.isoformat(),
                "venue": venue,
                "home_team": home,
                "away_team": away,
                "opp_team": opp_team,
                "player_team": player_team,
            },
        )
    finally:
        if close_after:
            conn.close()
