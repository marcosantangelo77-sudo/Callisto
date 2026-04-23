"""ml_features — read-only feature store for Callisto's ML baseline.

Callisto has accumulated substantial history in ``player_stats``,
``backtest_events``, ``game_results``, ``game_contexts``, ``line_movements``
and ``closing_lines``. The hand-crafted thesis seeds cover specific cells in
that space; an ML baseline can learn patterns the seeds miss — but only if
we can produce a clean feature vector for any ``(player, stat, asof)`` or
``(event, asof)`` tuple without leakage.

This module is STRICTLY read-only. It does not insert/update/delete rows and
it never reaches over the network. Every query is bounded by an ``asof_ts``
cutoff so the same feature vector can be reproduced deterministically at
training time and at inference time. No lookahead — rolling windows filter
``game_date < asof_date`` (strict).

Public surface:

    build_player_prop_features(
        player, stat_type, event_id, asof_ts, sport=..., conn=None
    ) -> FeatureVector

    build_game_total_features(
        event_id, asof_ts, sport=..., conn=None
    ) -> FeatureVector

    feature_names_player_prop() -> list[str]
    feature_names_game_total()  -> list[str]

A ``FeatureVector`` is a thin wrapper around an ordered numpy array plus a
name list so downstream callers can keep train/predict alignment without
relying on pandas column ordering. All features are float64; missing values
are surfaced as ``numpy.nan`` (the classifier layer uses XGBoost, which
handles NaN natively).
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence, Union

import numpy as np

# Local imports — these are pure, read-only helpers we are allowed to depend
# on per the constraints in the task brief.
try:
    from tools.game_dates import (
        get_venue_timezone,
        is_day_game,
        local_day_of_week,
        local_hour_of_day,
    )
except ImportError:  # pragma: no cover - fallback for test discovery
    from game_dates import (  # type: ignore
        get_venue_timezone,
        is_day_game,
        local_day_of_week,
        local_hour_of_day,
    )

logger = logging.getLogger("callisto.ml_features")


# ──────────────────────────────────────────────────────────────────────────
# Static venue park factors (inlined mirror of data_collector.VENUE_METADATA)
# ──────────────────────────────────────────────────────────────────────────
#
# We intentionally mirror the park-factor subset of VENUE_METADATA rather
# than importing from tools.data_collector — the data_collector module is in
# the DO-NOT-TOUCH list, importing it pulls in httpx + credentials on every
# feature extraction. These are static numbers that rarely change.
_PARK_FACTORS: dict[str, float] = {
    "Coors Field": 1.35,
    "Great American Ball Park": 1.13,
    "Yankee Stadium": 1.11,
    "Fenway Park": 1.07,
    "American Family Field": 1.05,
    "Wrigley Field": 1.05,
    "Minute Maid Park": 1.04,
    "Chase Field": 1.04,
    "Rogers Centre": 1.00,
    "Globe Life Field": 0.98,
    "Dodger Stadium": 0.96,
    "T-Mobile Park": 0.93,
    "Petco Park": 0.90,
    "Tropicana Field": 0.90,
    "loanDepot park": 0.88,
    "Oracle Park": 0.83,
}

_DOME_VENUES: set[str] = {
    # Subset of VENUE_METADATA["<name>"]["dome"] == True entries.
    "Rogers Centre",
    "Globe Life Field",
    "Chase Field",
    "T-Mobile Park",
    "Tropicana Field",
    "Minute Maid Park",
    "American Family Field",
    "loanDepot park",
    "Allegiant Stadium",
    "AT&T Stadium",
    "Caesars Superdome",
    "Lucas Oil Stadium",
    "Mercedes-Benz Stadium",
    "U.S. Bank Stadium",
    "State Farm Stadium",
    "NRG Stadium",
    "SoFi Stadium",
}


def _park_factor(venue_name: Optional[str]) -> float:
    if not venue_name:
        return 1.0
    v = venue_name.strip()
    if v in _PARK_FACTORS:
        return _PARK_FACTORS[v]
    # Partial / fuzzy match — short-circuit for e.g. "Fenway Park (test)"
    for key, pf in _PARK_FACTORS.items():
        if key.lower() in v.lower() or v.lower() in key.lower():
            return pf
    return 1.0


def _is_dome(venue_name: Optional[str]) -> int:
    if not venue_name:
        return 0
    v = venue_name.strip()
    if v in _DOME_VENUES:
        return 1
    for key in _DOME_VENUES:
        if key.lower() in v.lower():
            return 1
    return 0


# ──────────────────────────────────────────────────────────────────────────
# FeatureVector container
# ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeatureVector:
    """Ordered feature values + their names.

    Immutable by design so downstream batch-training code can't accidentally
    shuffle column order between rows.
    """

    names: tuple[str, ...]
    values: np.ndarray  # shape (len(names),), dtype float64
    target_stat_value: Optional[float] = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.values.shape != (len(self.names),):
            raise ValueError(
                f"FeatureVector shape mismatch: {len(self.names)} names, "
                f"{self.values.shape} values"
            )

    def as_dict(self) -> dict[str, float]:
        return {n: float(v) for n, v in zip(self.names, self.values)}


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────

def _resolve_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _open_ro(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a read-only SQLite connection. Callers SHOULD pass an existing
    connection when batching; this is a convenience for one-off calls."""
    path = db_path or _resolve_db_path()
    # URI mode + mode=ro protects us from accidentally mutating anything.
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────────────────────────────────
# Date / asof helpers
# ──────────────────────────────────────────────────────────────────────────

def _asof_date(asof_ts: Union[str, datetime, date]) -> date:
    """Normalise asof to a date — we filter history as ``< asof_date`` (strict)."""
    if isinstance(asof_ts, date) and not isinstance(asof_ts, datetime):
        return asof_ts
    if isinstance(asof_ts, datetime):
        return asof_ts.date()
    if isinstance(asof_ts, str):
        s = asof_ts.strip()
        # Accept ISO datetime or ISO date.
        try:
            if "T" in s or " " in s:
                s2 = s.replace("Z", "+00:00")
                return datetime.fromisoformat(s2).date()
            return date.fromisoformat(s[:10])
        except Exception:
            pass
    raise ValueError(f"Unparseable asof_ts: {asof_ts!r}")


def _safe_stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return float("nan")
    try:
        return float(statistics.stdev(xs))
    except statistics.StatisticsError:
        return float("nan")


def _trend_slope(xs: Sequence[float]) -> float:
    """Least-squares slope of ``xs`` against index 0..N-1. NaN if <2 points."""
    n = len(xs)
    if n < 2:
        return float("nan")
    arr = np.asarray(xs, dtype=float)
    if not np.isfinite(arr).any():
        return float("nan")
    idx = np.arange(n, dtype=float)
    mask = np.isfinite(arr)
    if mask.sum() < 2:
        return float("nan")
    try:
        slope = float(np.polyfit(idx[mask], arr[mask], 1)[0])
    except (np.linalg.LinAlgError, ValueError):
        return float("nan")
    return slope


def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return float("nan")
    arr = np.asarray(xs, dtype=float)
    mask = np.isfinite(arr)
    if not mask.any():
        return float("nan")
    return float(arr[mask].mean())


# ──────────────────────────────────────────────────────────────────────────
# Player-prop features
# ──────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────
# Game-total features
# ──────────────────────────────────────────────────────────────────────────

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
            return date.fromisoformat(row["gd"][:10])
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
    asof_ts: Union[str, datetime, date],
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


def _altitude_factor(venue: Optional[str]) -> float:
    altitudes = {
        "Coors Field": 5200,
        "Ball Arena": 5280,
        "Empower Field at Mile High": 5280,
        "Chase Field": 1082,
        "Allegiant Stadium": 2001,
        "State Farm Stadium": 1100,
        "Vivint Arena": 4226,
    }
    if not venue:
        return 0.0
    alt = altitudes.get(venue.strip(), 0)
    return float(alt) / 5280.0


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


__all__ = [
    "FeatureVector",
    "build_player_prop_features",
    "build_game_total_features",
    "feature_names_player_prop",
    "feature_names_game_total",
]
