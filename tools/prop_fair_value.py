"""
Prop fair-value model — minimum viable per-prop projection.

The goal is not "production prediction accuracy"; it's "a second independent
fair-value estimator that can disagree with the devig consensus on player
props". When the book line, devig consensus, and this projection all point
the same way, confidence compounds; when they diverge, at least one is
wrong and the edge scanner flags the prop for human-in-the-loop review.

Data source: the ``player_stats`` table populated by
``tools/data_collector.py`` (99k+ rows across MLB/NBA/NCAAB/NCAAW as of
2026-04-22). Each row is (sport, event_id, game_date, player_name, team,
stat_type, stat_value).

Confidence band:
    LOW        if < 10 recent games available
    MEDIUM     if 10–19 recent games
    HIGH       if 20+ recent games
The CI returned is the 1-sigma band of the rolling sample — callers that
need a tighter assumption can tighten the threshold downstream.

Public API:
    project_mlb_pitcher_strikeouts(db_path, player, expected_innings, opponent_k_rate=None, park_factor=1.0)
    project_mlb_batter_hits(db_path, player, expected_pas=4.0, park_factor_boost=0.0, opp_pitcher_quality_adj=0.0)
    project_nhl_skater_shots_on_goal(db_path, player, expected_toi_min=None)

Each returns ProjectionResult(fair_value, ci_low, ci_high, confidence, n_games, method).

Production upgrade paths (explicitly NOT implemented here):
    - Park-factor lookup table keyed by home team
    - Pitcher-vs-batter pitch-mix modelling via statcast_pitches
    - TOI estimation from lineup / line-combination sources
    - Mixed-effects regression against the training set
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("callisto.prop_fair_value")


# Confidence bands — tunable via env so the research loop can experiment
# without redeploying. Default thresholds: 10 / 20 games.
_MIN_GAMES_LOW = int(os.getenv("CALLISTO_FAIR_VALUE_MIN_GAMES_LOW", "10"))
_MIN_GAMES_HIGH = int(os.getenv("CALLISTO_FAIR_VALUE_MIN_GAMES_HIGH", "20"))


@dataclass
class ProjectionResult:
    fair_value: float
    ci_low: float
    ci_high: float
    confidence: str  # "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN"
    n_games: int
    method: str
    notes: str = ""


def _open(db_path: str) -> sqlite3.Connection:
    """Read-only DB connection. Callers must close."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _classify_confidence(n: int) -> str:
    if n <= 0:
        return "UNKNOWN"
    if n < _MIN_GAMES_LOW:
        return "LOW"
    if n < _MIN_GAMES_HIGH:
        return "MEDIUM"
    return "HIGH"


def _rolling_stat(
    conn: sqlite3.Connection,
    sport: str,
    player: str,
    stat_types: list[str],
    limit: int = 30,
) -> list[float]:
    """Return most recent ``limit`` values for any of ``stat_types``.

    Note: ``stat_type`` is case-sensitive in the table but ESPN ingestion
    sometimes produces either case — match both.
    """
    placeholders = ",".join("?" * len(stat_types))
    rows = conn.execute(
        f"""
        SELECT stat_value
        FROM player_stats
        WHERE sport = ?
          AND player_name = ?
          AND stat_type IN ({placeholders})
        ORDER BY game_date DESC
        LIMIT ?
        """,
        (sport, player, *stat_types, limit),
    ).fetchall()
    return [float(r["stat_value"]) for r in rows if r["stat_value"] is not None]


def _mean_std(values: list[float]) -> tuple[float, float]:
    n = len(values)
    if n == 0:
        return 0.0, 0.0
    mean = sum(values) / n
    if n == 1:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


# ---------------------------------------------------------------------------
# MLB pitcher strikeouts
# ---------------------------------------------------------------------------

def project_mlb_pitcher_strikeouts(
    db_path: str,
    player: str,
    expected_innings: float,
    opponent_k_rate: Optional[float] = None,
    park_factor: float = 1.0,
) -> ProjectionResult:
    """Projection: K / IP rolling rate × expected IP × opponent adj × park.

    ``opponent_k_rate`` — league-relative K rate (1.0 = league avg). Higher
    values mean the opponent strikes out more often.
    """
    try:
        conn = _open(db_path)
    except sqlite3.Error as e:
        logger.warning(f"prop_fair_value MLB K: DB open failed: {e}")
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "mlb_pitcher_k", notes=str(e))

    try:
        ks = _rolling_stat(conn, "mlb", player, ["strikeouts", "Strikeouts", "K", "SO"])
        ips = _rolling_stat(conn, "mlb", player, ["innings_pitched", "IP", "Innings Pitched"])
    finally:
        conn.close()

    n = min(len(ks), len(ips)) if ips else len(ks)
    if n == 0:
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "mlb_pitcher_k",
                                notes=f"no player_stats rows for {player}")

    if ips and len(ips) == len(ks):
        # Per-start K/IP rates
        rates = [k / max(i, 0.1) for k, i in zip(ks, ips)]
        rate_mean, rate_sd = _mean_std(rates)
    else:
        # Fall back to raw K counts divided by a 6IP assumed start length
        rate_mean, rate_sd = _mean_std([k / 6.0 for k in ks])

    opp_adj = opponent_k_rate if opponent_k_rate is not None else 1.0
    pf = max(0.5, min(park_factor, 1.5))  # sanity-clamp

    fair = rate_mean * expected_innings * opp_adj * pf
    sigma = rate_sd * expected_innings * opp_adj * pf

    return ProjectionResult(
        fair_value=round(fair, 3),
        ci_low=round(max(0.0, fair - sigma), 3),
        ci_high=round(fair + sigma, 3),
        confidence=_classify_confidence(n),
        n_games=n,
        method="mlb_pitcher_k",
        notes=f"rate={rate_mean:.3f} K/IP, opp_adj={opp_adj:.2f}, pf={pf:.2f}",
    )


# ---------------------------------------------------------------------------
# MLB batter hits
# ---------------------------------------------------------------------------

def project_mlb_batter_hits(
    db_path: str,
    player: str,
    expected_pas: float = 4.0,
    park_factor_boost: float = 0.0,
    opp_pitcher_quality_adj: float = 0.0,
) -> ProjectionResult:
    """Projection: rolling batting avg × expected PAs × park boost × opp adj.

    ``park_factor_boost`` — additive, e.g. 0.05 for +5% at a hitter park.
    ``opp_pitcher_quality_adj`` — additive, signed. Positive = easier pitcher.
    """
    try:
        conn = _open(db_path)
    except sqlite3.Error as e:
        logger.warning(f"prop_fair_value MLB hits: DB open failed: {e}")
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "mlb_batter_hits", notes=str(e))

    try:
        hits = _rolling_stat(conn, "mlb", player, ["hits", "Hits", "H"])
        abs_ = _rolling_stat(conn, "mlb", player, ["at_bats", "AB", "At Bats"])
    finally:
        conn.close()

    n = min(len(hits), len(abs_)) if abs_ else len(hits)
    if n == 0:
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "mlb_batter_hits",
                                notes=f"no player_stats rows for {player}")

    if abs_ and len(abs_) == len(hits):
        # AVG per game
        avgs = [h / max(a, 1.0) for h, a in zip(hits, abs_)]
        avg_mean, avg_sd = _mean_std(avgs)
    else:
        # Fall back to hits-per-game divided by assumed 4 ABs
        avg_mean, avg_sd = _mean_std([h / 4.0 for h in hits])

    adj = 1.0 + park_factor_boost + opp_pitcher_quality_adj
    adj = max(0.5, min(adj, 1.5))

    fair = avg_mean * expected_pas * adj
    sigma = avg_sd * expected_pas * adj

    return ProjectionResult(
        fair_value=round(fair, 3),
        ci_low=round(max(0.0, fair - sigma), 3),
        ci_high=round(fair + sigma, 3),
        confidence=_classify_confidence(n),
        n_games=n,
        method="mlb_batter_hits",
        notes=f"avg={avg_mean:.3f}, adj={adj:.3f}",
    )


# ---------------------------------------------------------------------------
# NHL skater shots on goal
# ---------------------------------------------------------------------------

def project_nhl_skater_shots_on_goal(
    db_path: str,
    player: str,
    expected_toi_min: Optional[float] = None,
) -> ProjectionResult:
    """Projection: rolling SOG / TOI × expected TOI (minutes).

    If TOI history is missing we fall back to raw SOG rolling mean — still
    better than nothing for a MVP model.
    """
    try:
        conn = _open(db_path)
    except sqlite3.Error as e:
        logger.warning(f"prop_fair_value NHL SOG: DB open failed: {e}")
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "nhl_skater_sog", notes=str(e))

    try:
        sogs = _rolling_stat(conn, "nhl", player, ["shots_on_goal", "Shots on Goal", "SOG", "shots"])
        tois = _rolling_stat(conn, "nhl", player, ["toi", "TOI", "Time on Ice", "time_on_ice"])
    finally:
        conn.close()

    n = len(sogs)
    if n == 0:
        return ProjectionResult(0.0, 0.0, 0.0, "UNKNOWN", 0, "nhl_skater_sog",
                                notes=f"no player_stats rows for {player}")

    if expected_toi_min is not None and tois and len(tois) == n:
        # SOG per minute × expected minutes
        rates = [s / max(t, 1.0) for s, t in zip(sogs, tois)]
        rate_mean, rate_sd = _mean_std(rates)
        fair = rate_mean * expected_toi_min
        sigma = rate_sd * expected_toi_min
        notes = f"rate={rate_mean:.3f} SOG/min × {expected_toi_min:.1f} min"
    else:
        mean, sd = _mean_std(sogs)
        fair, sigma = mean, sd
        notes = f"raw SOG/game mean={mean:.2f}"

    return ProjectionResult(
        fair_value=round(fair, 3),
        ci_low=round(max(0.0, fair - sigma), 3),
        ci_high=round(fair + sigma, 3),
        confidence=_classify_confidence(n),
        n_games=n,
        method="nhl_skater_sog",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Dispatch helper for the edge scanner / hypothesis runner
# ---------------------------------------------------------------------------

def project_prop(
    db_path: str,
    sport: str,
    prop_key: str,
    player: str,
    **kwargs,
) -> Optional[ProjectionResult]:
    """Route a (sport, prop_key) pair to the right projector.

    Unknown props return None so callers can fall back to devig-only.
    """
    routing = {
        ("baseball_mlb", "pitcher_strikeouts"): project_mlb_pitcher_strikeouts,
        ("baseball_mlb", "batter_hits"): project_mlb_batter_hits,
        ("icehockey_nhl", "skater_shots_on_goal"): project_nhl_skater_shots_on_goal,
    }
    fn = routing.get((sport, prop_key))
    if fn is None:
        return None
    try:
        return fn(db_path, player, **kwargs)
    except TypeError as e:
        logger.warning(f"project_prop: bad kwargs for {sport}/{prop_key}: {e}")
        return None
