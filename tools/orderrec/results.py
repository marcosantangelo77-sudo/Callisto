"""Async DB lookups for game results / contexts / player stats (split from
``tools/order_reconciler``)."""

from __future__ import annotations

import json
from typing import Optional


async def _lookup_game_result(db, sport: str, event_id: str) -> Optional[dict]:
    """Fetch a ``game_results`` row keyed on (sport, event_id).

    ``game_results`` doesn't carry event_id directly (unique key is
    (sport, game_date, home_team, away_team)), so we consult
    ``game_contexts`` first to map event_id -> (home, away, game_date),
    then lift the matching game_results row. Falls back to a best-effort
    match against home/away when game_contexts is empty — safeguard for
    tests that populate game_results directly.
    """
    if not event_id:
        return None
    # Primary path — game_contexts has the event_id <-> (home, away, date) map.
    # game_contexts may be absent in stripped-down test DBs; swallow and
    # fall through to the direct fallback.
    try:
        cur = await db.execute(
            "SELECT home_team, away_team, game_date FROM game_contexts "
            "WHERE sport = ? AND event_id = ? LIMIT 1",
            (sport, event_id),
        )
        ctx = await cur.fetchone()
    except Exception:
        ctx = None
    if ctx:
        gr = await db.execute(
            "SELECT home_team, away_team, home_score, away_score, "
            "total_score, spread_result, winner "
            "FROM game_results "
            "WHERE sport = ? AND game_date = ? "
            "AND home_team = ? AND away_team = ? LIMIT 1",
            (sport, ctx["game_date"], ctx["home_team"], ctx["away_team"]),
        )
        row = await gr.fetchone()
        if row:
            return dict(row)
    # Fallback: event_id might literally be a team abbrev that matches
    # home/away (used throughout the existing tests). Sport filter keeps
    # cross-sport collisions out.
    gr2 = await db.execute(
        "SELECT home_team, away_team, home_score, away_score, "
        "total_score, spread_result, winner "
        "FROM game_results "
        "WHERE sport = ? AND (home_team = ? OR away_team = ?) "
        "ORDER BY game_date DESC LIMIT 1",
        (sport, event_id, event_id),
    )
    row = await gr2.fetchone()
    return dict(row) if row else None


async def _lookup_game_context(
    db, sport: str, event_id: str
) -> Optional[dict]:
    """Game context row — carries game_date we use for stuck detection
    and ``context_json.status`` for void detection.
    """
    if not event_id:
        return None
    try:
        cur = await db.execute(
            "SELECT home_team, away_team, game_date, context_json "
            "FROM game_contexts WHERE sport = ? AND event_id = ? LIMIT 1",
            (sport, event_id),
        )
        row = await cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    out = dict(row)
    try:
        out["context"] = json.loads(out.get("context_json") or "{}")
    except Exception:
        out["context"] = {}
    return out


async def _lookup_player_stat(
    db, sport: str, event_id: str, player: str, stat_type: str
) -> Optional[float]:
    """Return ``stat_value`` from ``player_stats`` for a prop settle."""
    if not event_id or not player or not stat_type:
        return None
    cur = await db.execute(
        "SELECT stat_value FROM player_stats "
        "WHERE sport = ? AND event_id = ? "
        "AND LOWER(player_name) = LOWER(?) "
        "AND LOWER(stat_type) = LOWER(?) LIMIT 1",
        (sport, event_id, player, stat_type),
    )
    row = await cur.fetchone()
    return float(row["stat_value"]) if row else None
