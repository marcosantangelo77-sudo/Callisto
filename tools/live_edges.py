"""Live in-game reactive edge detectors.

This module defines three detectors that read recent rows from
``live_game_states`` plus live odds (via ``edge_scanner.weighted_sharp
_consensus`` and ``odds_snapshots``) and emit rows to
``ev_opportunities`` with ``is_live=1`` and a ``thesis_tag``.

Detectors
---------
A. ``mlb_quiet_innings`` — MLB totals market. Trigger: through
   N≥3 innings with run totals ≤1, the live total has dropped by ≥1 run
   from its pre-game value. Check whether the market drop is larger
   than the legitimate revision implied by residual-inning expected
   runs (pre-game total × residual_frac). If the live market implies
   fewer residual runs than the pre-game model expects, the OVER is
   the edge.

B. ``nba_nhl_late_overreaction`` — covers basketball and hockey.
   Trigger: at end of Q3 (NBA) or after period 2 (NHL), a ≥15-pt NBA
   lead (or ≥3-goal NHL lead) has compressed the live spread far past
   regression to a reasonable Q4 / P3 expectation. The underdog side
   is the edge when the implied remaining-period scoring differential
   is larger than an empirically sane delta.

C. ``live_prop_reactivity`` — any sport. Trigger: a player prop line
   shifts >15% within 30s. Compare the new line to the prior line
   weighted by remaining-time EV; flag as over-reaction (direction =
   opposite of the shift) when the unweighted shift implies a harder
   adjustment than the remaining-time decay justifies.

Each emission
-------------
1. Looks up ``live_edge_emissions`` for the same (event_id, market,
   thesis_tag) within the last 2min. If present, skip.
2. Looks up the per-game kill switch: if this event has ≥3 emissions
   within the last 5min AND none of them have a known good CLV
   outcome, skip.
3. Inserts one row into ``ev_opportunities`` with ``is_live=1``,
   ``thesis_tag=<detector>``, ``expires_at = now + 60s``.
4. Inserts a ledger row in ``live_edge_emissions``.

Safety
------
This module NEVER places bets. The executor agent is the only writer
to the betting endpoint. If an ``ev_opportunities`` row is still
present when the executor runs, it MUST check ``expires_at`` and
reject stale rows.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

from tools.book_keys import canonicalize_book

logger = logging.getLogger("callisto.live_edges")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Per-(event, market, thesis) emission rate-limit window.
EMISSION_COOLDOWN_S = 120

# Kill-switch thresholds.
KILL_SWITCH_WINDOW_S = 300
KILL_SWITCH_MAX_EMISSIONS = 3
KILL_SWITCH_MIN_GOOD_CLV = 1  # require at least 1 good-CLV hit to stay on

# Edge TTL — live edges decay fast.
LIVE_EDGE_TTL_S = 60

# MLB: minimum innings completed before quiet-innings triggers.
MLB_QUIET_MIN_INNINGS = 3

# MLB: threshold — live total must have dropped ≥ this much (in runs)
# from pre-game to be considered over-reaction candidate.
MLB_LINE_DROP_THRESHOLD = 1.0

# MLB: residual expected-runs must exceed live-implied residual by this
# many runs to flag an OVER edge. Chosen conservatively — real
# over-reactions in the data we've seen are typically >0.75 runs.
MLB_OVER_EDGE_RUNS = 0.50

# NBA: after Q3, absolute spread must have expanded past this many
# points beyond the Q3 score differential to flag regression.
NBA_SPREAD_OVEREXTEND_POINTS = 3.0

# NHL: same shape, in goals.
NHL_SPREAD_OVEREXTEND_GOALS = 0.75

# Prop reactivity: fractional shift over the window that triggers.
PROP_SHIFT_FRAC_THRESHOLD = 0.15
PROP_SHIFT_WINDOW_S = 30


# ──────────────────────────────────────────────────────────────────────
# Dataclasses — detector IO is structured so tests can construct inputs
# without reaching into SQLite.
# ──────────────────────────────────────────────────────────────────────


@dataclass
class LiveEdge:
    """Result of a detector firing. May be persisted or discarded."""
    event_id: str
    sport: str
    market: str
    team: str
    thesis_tag: str
    side: str  # "OVER", "UNDER", "HOME", "AWAY", etc.
    edge: float
    implied_probability: float
    estimated_true_prob: float
    bookmaker: str
    notes: str = ""


# ──────────────────────────────────────────────────────────────────────
# Rate-limit / kill-switch helpers
# ──────────────────────────────────────────────────────────────────────


async def _is_rate_limited(
    db: aiosqlite.Connection,
    event_id: str,
    market: str,
    thesis_tag: str,
    now: datetime,
) -> bool:
    """True if we already emitted this (event, market, thesis) in the
    last ``EMISSION_COOLDOWN_S`` seconds."""
    cutoff = (now - timedelta(seconds=EMISSION_COOLDOWN_S)).isoformat()
    cur = await db.execute(
        "SELECT 1 FROM live_edge_emissions "
        "WHERE event_id = ? AND market = ? AND thesis_tag = ? "
        "  AND emitted_at >= ? LIMIT 1",
        (event_id, market, thesis_tag, cutoff),
    )
    return (await cur.fetchone()) is not None


async def _kill_switch_active(
    db: aiosqlite.Connection,
    event_id: str,
    now: datetime,
) -> bool:
    """Per-game: if we've emitted ≥3 edges in last 5min with zero
    good-CLV hits, stop detecting for this event."""
    cutoff = (now - timedelta(seconds=KILL_SWITCH_WINDOW_S)).isoformat()
    cur = await db.execute(
        "SELECT COUNT(*) FROM live_edge_emissions "
        "WHERE event_id = ? AND emitted_at >= ?",
        (event_id, cutoff),
    )
    row = await cur.fetchone()
    recent = int((row or [0])[0])
    if recent < KILL_SWITCH_MAX_EMISSIONS:
        return False
    # Count good-CLV hits among recent emissions. We JOIN back to
    # ev_opportunities (via ev_opp_id) and clv_log to see which had a
    # positive CLV. If none did → kill switch on.
    cur = await db.execute(
        """
        SELECT COUNT(DISTINCT le.id)
        FROM live_edge_emissions le
        JOIN ev_opportunities eo ON eo.id = le.ev_opp_id
        LEFT JOIN clv_log c ON c.game_id = eo.game_id
        WHERE le.event_id = ?
          AND le.emitted_at >= ?
          AND (c.clv_prob_bp IS NOT NULL AND c.clv_prob_bp > 0)
        """,
        (event_id, cutoff),
    )
    row = await cur.fetchone()
    good = int((row or [0])[0])
    return good < KILL_SWITCH_MIN_GOOD_CLV


async def _persist_edge(
    db: aiosqlite.Connection,
    edge: LiveEdge,
    now: datetime,
) -> Optional[int]:
    """Write the ev_opportunities + live_edge_emissions rows, returning
    the inserted ``ev_opportunities.id`` or None on rejection."""
    if await _is_rate_limited(db, edge.event_id, edge.market, edge.thesis_tag, now):
        return None
    if await _kill_switch_active(db, edge.event_id, now):
        logger.info(
            f"kill-switch active for event={edge.event_id}; skipping "
            f"{edge.thesis_tag}/{edge.market}"
        )
        return None

    expires_at = (now + timedelta(seconds=LIVE_EDGE_TTL_S)).isoformat()
    cur = await db.execute(
        "INSERT INTO ev_opportunities "
        "(detected_at, sport, game_id, team, market, bookmaker, "
        " implied_probability, estimated_true_prob, edge, expected_value, "
        " source, is_live, thesis_tag, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live_ingame', 1, ?, ?)",
        (
            now.isoformat(),
            edge.sport,
            edge.event_id,
            edge.team,
            edge.market,
            canonicalize_book(edge.bookmaker),
            round(edge.implied_probability, 4),
            round(edge.estimated_true_prob, 4),
            round(edge.edge, 4),
            round(edge.edge, 4),  # ev ≈ edge for this first-pass.
            edge.thesis_tag,
            expires_at,
        ),
    )
    ev_id = int(cur.lastrowid or 0)
    await db.execute(
        "INSERT INTO live_edge_emissions "
        "(event_id, sport, market, thesis_tag, emitted_at, expires_at, "
        " ev_opp_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge.event_id, edge.sport, edge.market, edge.thesis_tag,
            now.isoformat(), expires_at, ev_id, edge.notes,
        ),
    )
    await db.commit()
    return ev_id


# ──────────────────────────────────────────────────────────────────────
# Detector A: MLB quiet innings over-reaction
# ──────────────────────────────────────────────────────────────────────


def mlb_extract_state(summary: dict) -> dict:
    """Pull the fields the quiet-innings detector needs from an ESPN
    summary payload.

    Returns dict with keys: ``inning`` (int), ``home_runs`` (int),
    ``away_runs`` (int). Missing fields default to 0 / None.
    """
    comps = (summary.get("header") or {}).get("competitions") or []
    if not comps:
        comps = summary.get("competitions") or []
    comp = comps[0] if comps else {}
    status = comp.get("status") or summary.get("status") or {}
    period = int(status.get("period") or 0)
    # ESPN encodes MLB inning as the "period" field (1-9+) and the half
    # via status.type.shortDetail ("Top 4th", "Bot 3rd").
    home_runs = away_runs = 0
    for team in comp.get("competitors", []):
        score = int(team.get("score") or 0)
        if (team.get("homeAway") or "").lower() == "home":
            home_runs = score
        else:
            away_runs = score
    return {
        "inning": period,
        "home_runs": home_runs,
        "away_runs": away_runs,
        "total_runs": home_runs + away_runs,
    }


def mlb_quiet_innings_signal(
    *,
    inning: int,
    total_runs: int,
    pregame_total: float,
    live_total: float,
    live_over_price: int,
) -> Optional[LiveEdge]:
    """Pure detector logic — extracted so tests can exercise it without
    SQLite or ESPN.

    Fires when:
      1. ``inning`` ≥ 3
      2. ``total_runs`` ≤ inning (i.e., a "quiet" start — averaging ≤1
         run/inning, a common over-reaction trigger)
      3. ``pregame_total - live_total`` ≥ MLB_LINE_DROP_THRESHOLD
      4. the live total implies fewer residual runs than the pre-game
         model expects over the remaining innings

    Returns a LiveEdge for OVER when the market has over-reacted; None
    otherwise.
    """
    if inning < MLB_QUIET_MIN_INNINGS:
        return None
    if total_runs > inning:
        # Not quiet — skip.
        return None
    line_drop = pregame_total - live_total
    if line_drop < MLB_LINE_DROP_THRESHOLD:
        return None

    # Residual frac based on 9-inning baseline. Runs are not uniformly
    # distributed across innings (slight backloading), but uniform is
    # a conservative lower bound: real residual is typically higher.
    residual_frac = max(0.0, (9 - inning) / 9.0)
    expected_residual = pregame_total * residual_frac
    live_implied_residual = live_total - total_runs
    gap = expected_residual - live_implied_residual
    if gap < MLB_OVER_EDGE_RUNS:
        return None

    # Translate the gap into an edge estimate. A 1-run gap on a live
    # total of 8 is roughly a 5-8% fair-price shift on the OVER side;
    # we pick a flat 0.04 per 0.5-run gap as a conservative floor and
    # let the executor apply its own sizing. This is deliberately not
    # a full distribution model — it's a trigger.
    edge_est = min(0.15, 0.04 * (gap / MLB_OVER_EDGE_RUNS))
    implied = _american_to_implied(live_over_price)
    if implied is None:
        return None
    true_prob = min(0.99, implied + edge_est)

    return LiveEdge(
        event_id="",  # filled in by caller
        sport="baseball_mlb",
        market="totals",
        team="OVER",
        thesis_tag="mlb_quiet_innings",
        side="OVER",
        edge=edge_est,
        implied_probability=implied,
        estimated_true_prob=true_prob,
        bookmaker="",
        notes=f"inning={inning} runs={total_runs} pre={pregame_total} "
              f"live={live_total} residual_gap={gap:.2f}",
    )


def _american_to_implied(american: Optional[int]) -> Optional[float]:
    """Standard conversion — duplicated locally so the pure-signal
    function has zero SQLite/async dependencies."""
    if american is None:
        return None
    a = int(american)
    if a > 0:
        return 100.0 / (a + 100.0)
    return (-a) / ((-a) + 100.0)


def _parse_clock_seconds(value: Any) -> Optional[int]:
    """Return ESPN clock value as integer seconds remaining in the period.

    ESPN returns ``clock``/``displayClock`` in several shapes across sports
    and endpoints:
      - ``float`` / ``int`` already expressed as seconds (common in the
        summary payload for MLB / NHL).
      - ``"10:35"`` — mm:ss string.
      - ``"0.0"`` / numeric string — seconds as string.
      - ``"00:00.0"`` — NHL overtime / shootout variant.

    Returns None when the value cannot be parsed. Callers treat None as
    "no clock signal" rather than assuming 0, so the detector does not
    fire prematurely on malformed payloads.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    s = str(value).strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            mins = int(float(parts[0]))
            secs = float(parts[1]) if len(parts) > 1 else 0.0
            return max(0, mins * 60 + int(secs))
        except (ValueError, TypeError):
            return None
    try:
        return max(0, int(float(s)))
    except (ValueError, TypeError):
        return None


def _extract_competition_block(summary: dict) -> tuple[dict, dict]:
    """Return (competition, status) dicts from an ESPN summary payload.

    Falls back across the handful of shapes ESPN returns depending on
    endpoint (summary vs scoreboard-event vs fastcast). Missing blocks
    return ``{}`` so downstream ``.get`` calls stay safe.
    """
    comps = (summary.get("header") or {}).get("competitions") or []
    if not comps:
        comps = summary.get("competitions") or []
    comp = comps[0] if comps else {}
    status = comp.get("status") or summary.get("status") or {}
    return comp, status


def _extract_scores_home_away(comp: dict) -> tuple[int, int]:
    """Return ``(home_score, away_score)`` from an ESPN competition dict.

    Unknown sides default to 0 — detectors gate on score differentials,
    so a silently-zero side simply fails the trigger rather than faking
    a lead."""
    home = away = 0
    for team in comp.get("competitors") or []:
        try:
            score = int(float(team.get("score") or 0))
        except (ValueError, TypeError):
            score = 0
        side = (team.get("homeAway") or "").lower()
        if side == "home":
            home = score
        elif side == "away":
            away = score
    return home, away


def nba_extract_state(summary: dict) -> dict:
    """Pull the fields the NBA late-overreaction detector needs.

    Returns dict with keys: ``period`` (int 1-4, 5+ = OT),
    ``time_remaining_s`` (int or None), ``home_score`` (int),
    ``away_score`` (int).

    Missing fields default to 0 / None so the signal function can gate
    on them without raising.
    """
    comp, status = _extract_competition_block(summary)
    try:
        period = int(status.get("period") or 0)
    except (ValueError, TypeError):
        period = 0
    clock = status.get("clock")
    if clock is None:
        clock = status.get("displayClock")
    time_remaining_s = _parse_clock_seconds(clock)
    home_score, away_score = _extract_scores_home_away(comp)
    return {
        "period": period,
        "time_remaining_s": time_remaining_s,
        "home_score": home_score,
        "away_score": away_score,
    }


def nhl_extract_state(summary: dict) -> dict:
    """Pull the fields the NHL late-overreaction detector needs.

    Same shape as ``nba_extract_state`` — the two sports share the
    ESPN summary schema; they differ only in period count (3 vs 4)
    and score magnitudes.
    """
    return nba_extract_state(summary)


# ──────────────────────────────────────────────────────────────────────
# Detector B: NBA / NHL late-game over-reaction
# ──────────────────────────────────────────────────────────────────────


def nba_late_overreaction_signal(
    *,
    period: int,
    time_remaining_s: int,
    home_score: int,
    away_score: int,
    live_spread_home: float,
    live_home_price: int,
) -> Optional[LiveEdge]:
    """Fire when NBA Q3 ends with a 15+ pt lead that has compressed
    the live spread past reasonable Q4 regression.

    live_spread_home is the current home spread (negative = home
    favored). The comeback side is the edge.
    """
    # Only consider end-of-Q3 state (period==3, time≈0). We use a
    # loose window: period >=3 and time_remaining_s <= 120 to capture
    # the late-Q3 over-reaction sweet spot.
    if period < 3 or time_remaining_s > 120:
        return None
    diff = home_score - away_score
    if abs(diff) < 15:
        return None
    # If home is up 18 and the live spread is home -9, the market is
    # pricing in essentially even Q4. That's the over-reaction — Q4
    # scoring differentials regress but not that hard. Fire the
    # COMEBACK side when live_spread implies an unrealistic Q4 swing.
    #
    # Expected Q4 diff regression (empirical floor): ~40% of Q3 lead
    # is preserved. If live_spread is tighter than 0.6*diff, OVERSHOOT.
    expected_end_diff = 0.60 * diff
    # live_spread_home, if home is favored, is negative. Convert to
    # "implied end-of-game home diff" = -live_spread_home.
    implied_end_diff = -live_spread_home
    # Over-extension: the market has moved past expected by at least
    # NBA_SPREAD_OVEREXTEND_POINTS in the direction of the trailing
    # side.
    overextend = expected_end_diff - implied_end_diff
    if diff > 0:
        # Home is up. Over-reaction compresses spread → implied_end_diff
        # < expected. If overextend > threshold, edge is HOME (cover).
        if overextend < NBA_SPREAD_OVEREXTEND_POINTS:
            return None
        side, team = "HOME", "HOME"
    else:
        # Away is up — symmetric.
        if -overextend < NBA_SPREAD_OVEREXTEND_POINTS:
            return None
        side, team = "AWAY", "AWAY"

    implied = _american_to_implied(live_home_price)
    if implied is None:
        return None
    edge_est = min(0.10, 0.03 * abs(overextend))
    true_prob = min(0.99, implied + edge_est)
    return LiveEdge(
        event_id="",
        sport="basketball_nba",
        market="spread",
        team=team,
        thesis_tag="nba_late_overreaction",
        side=side,
        edge=edge_est,
        implied_probability=implied,
        estimated_true_prob=true_prob,
        bookmaker="",
        notes=f"period={period} diff={diff} live_sp={live_spread_home} "
              f"overextend={overextend:.1f}",
    )


def nhl_late_overreaction_signal(
    *,
    period: int,
    time_remaining_s: int,
    home_score: int,
    away_score: int,
    live_puck_line_home: float,
    live_home_price: int,
) -> Optional[LiveEdge]:
    """Same shape as NBA, in goals, at end of P2."""
    if period < 2 or time_remaining_s > 120:
        return None
    diff = home_score - away_score
    if abs(diff) < 3:
        return None
    expected_end_diff = 0.60 * diff
    implied_end_diff = -live_puck_line_home
    overextend = expected_end_diff - implied_end_diff
    if diff > 0:
        if overextend < NHL_SPREAD_OVEREXTEND_GOALS:
            return None
        side, team = "HOME", "HOME"
    else:
        if -overextend < NHL_SPREAD_OVEREXTEND_GOALS:
            return None
        side, team = "AWAY", "AWAY"
    implied = _american_to_implied(live_home_price)
    if implied is None:
        return None
    edge_est = min(0.08, 0.04 * abs(overextend))
    true_prob = min(0.99, implied + edge_est)
    return LiveEdge(
        event_id="",
        sport="icehockey_nhl",
        market="puck_line",
        team=team,
        thesis_tag="nhl_late_overreaction",
        side=side,
        edge=edge_est,
        implied_probability=implied,
        estimated_true_prob=true_prob,
        bookmaker="",
        notes=f"period={period} diff={diff} live_pl={live_puck_line_home} "
              f"overextend={overextend:.2f}",
    )


# ──────────────────────────────────────────────────────────────────────
# Detector C: Live prop reactivity
# ──────────────────────────────────────────────────────────────────────


def prop_reactivity_signal(
    *,
    prior_line: float,
    new_line: float,
    window_s: float,
    remaining_time_frac: float,
    new_over_price: int,
) -> Optional[LiveEdge]:
    """Fire when a prop line shifts >15% in under 30s and the shift
    exceeds what remaining-time EV justifies.

    ``remaining_time_frac`` in [0, 1] is the fraction of the game's
    time left (e.g. 0.25 for Q4 of an NBA game with 3:00 left).
    """
    if window_s > PROP_SHIFT_WINDOW_S:
        return None
    if prior_line <= 0:
        return None
    shift_frac = (new_line - prior_line) / prior_line
    if abs(shift_frac) < PROP_SHIFT_FRAC_THRESHOLD:
        return None

    # Remaining-time-weighted justified shift: if 75% of the game is
    # already played, only 25% of time remains, so the line should
    # move ≲ 25% from a triggering event. If the actual shift is much
    # bigger than that ceiling, flag as over-reaction.
    justified_ceiling = max(0.05, remaining_time_frac)
    overreact = abs(shift_frac) - justified_ceiling
    if overreact < 0.05:
        return None

    # Direction: over-reaction → counter-party edge. If line dropped
    # (shift_frac<0), bet OVER at the new (cheaper) OVER; else UNDER.
    side = "OVER" if shift_frac < 0 else "UNDER"
    implied = _american_to_implied(new_over_price if side == "OVER" else -new_over_price)
    if implied is None:
        return None
    edge_est = min(0.10, overreact * 0.5)
    true_prob = min(0.99, implied + edge_est)
    return LiveEdge(
        event_id="",
        sport="",  # caller fills in
        market="player_prop",
        team=side,
        thesis_tag="live_prop_reactivity",
        side=side,
        edge=edge_est,
        implied_probability=implied,
        estimated_true_prob=true_prob,
        bookmaker="",
        notes=f"prior={prior_line} new={new_line} window={window_s}s "
              f"shift={shift_frac:+.1%} overreact={overreact:+.1%}",
    )


# ──────────────────────────────────────────────────────────────────────
# Public orchestrator — ties the pure signal functions to DB IO.
# ──────────────────────────────────────────────────────────────────────


async def emit_edge(
    edge: LiveEdge,
    *,
    db_path: str = DB_PATH,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Write ``edge`` to ev_opportunities + live_edge_emissions subject
    to rate-limit and kill-switch. Returns the new id or None if
    suppressed."""
    if now is None:
        now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        return await _persist_edge(db, edge, now)


# ──────────────────────────────────────────────────────────────────────
# Hypothesis seeding — one row per detector. Called idempotently at
# startup so backtest machinery can evaluate each thesis against
# live_game_states replay.
# ──────────────────────────────────────────────────────────────────────


LIVE_HYPOTHESES: tuple[dict, ...] = (
    {
        "hypothesis_id": "live_ingame.mlb_quiet_innings.v1",
        "name": "live_ingame.mlb_quiet_innings",
        "thesis": (
            "After 3+ quiet MLB innings the live total over-corrects below the "
            "pre-game total minus residual expected runs; OVER is +EV until "
            "the next scoring inning."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
    },
    {
        "hypothesis_id": "live_ingame.nba_late_overreaction.v1",
        "name": "live_ingame.nba_late_overreaction",
        "thesis": (
            "NBA end-of-Q3 with 15+ point lead compresses live spread below "
            "realistic Q4 regression; favorite-cover edge emerges when the "
            "market prices in near-even Q4 scoring."
        ),
        "sport": "basketball_nba",
        "market_type": "spread",
    },
    {
        "hypothesis_id": "live_ingame.nhl_late_overreaction.v1",
        "name": "live_ingame.nhl_late_overreaction",
        "thesis": (
            "NHL end-of-P2 with 3+ goal lead over-compresses live puck line; "
            "favorite-cover edge until an opposing goal."
        ),
        "sport": "icehockey_nhl",
        "market_type": "puck_line",
    },
    {
        "hypothesis_id": "live_ingame.live_prop_reactivity.v1",
        "name": "live_ingame.live_prop_reactivity",
        "thesis": (
            "Player prop line shifts >15% within 30s of a discrete event "
            "frequently over-react; the counter-party side is +EV when the "
            "remaining-time-weighted justified shift is smaller than observed."
        ),
        "sport": "multi",
        "market_type": "player_prop",
    },
)


async def register_live_hypotheses(db_path: str = DB_PATH) -> int:
    """Insert (if absent) one row per live-in-game detector. Returns
    the number of newly inserted rows."""
    inserted = 0
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        for h in LIVE_HYPOTHESES:
            # Stash the category+version in notes as JSON so we can
            # query by category without needing a schema migration.
            notes = json.dumps({"category": "live_ingame", "version": 1})
            try:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO hypotheses "
                    "(hypothesis_id, name, thesis, sport, market_type, "
                    " model_config, edge_threshold, status, notes) "
                    "VALUES (?, ?, ?, ?, ?, '{}', 0.02, 'draft', ?)",
                    (
                        h["hypothesis_id"], h["name"], h["thesis"],
                        h["sport"], h["market_type"], notes,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
            except Exception as e:
                logger.warning(f"register_live_hypotheses failed for {h['name']}: {e}")
        await db.commit()
    return inserted


__all__ = [
    "LiveEdge",
    "LIVE_EDGE_TTL_S",
    "EMISSION_COOLDOWN_S",
    "KILL_SWITCH_WINDOW_S",
    "mlb_extract_state",
    "mlb_quiet_innings_signal",
    "nba_extract_state",
    "nba_late_overreaction_signal",
    "nhl_extract_state",
    "nhl_late_overreaction_signal",
    "prop_reactivity_signal",
    "emit_edge",
    "register_live_hypotheses",
]
