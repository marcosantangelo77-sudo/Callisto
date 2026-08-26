"""Detector bridge between live-state ingestion and ``tools.live_edges``.

Split out of ``tools/live_state.py``. The bridge imports lazily so
unit tests of the state store never pull the odds stack.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger("callisto.live_state")


def _fs():
    from tools import live_state as fs

    return fs


async def _evaluate_detectors(
    *,
    event_id: str,
    sport: str,
    state: dict,
    prev_state,
    db_path,
    now: datetime,
) -> int:
    """Run applicable detectors against the freshly-stored state.

    Returns the number of live-edge rows persisted. Currently we wire
    the MLB quiet-innings detector because it has a well-defined pure
    signal that doesn't require the live-odds WS (it reads the pre-game
    and live totals from DB). NBA/NHL detectors fire on the WS path
    (see ``line_monitor._handle_ws_update``) because they need the
    live_spread_home price which only arrives via the odds firehose.
    """
    if sport != "baseball_mlb":
        return 0
    try:
        from tools.live_edges import (
            LiveEdge,
            mlb_extract_state,
            mlb_quiet_innings_signal,
            emit_edge,
        )
    except Exception as e:
        logger.debug(f"live_edges import failed: {e}")
        return 0

    try:
        parsed = mlb_extract_state(state)
    except Exception as e:
        logger.debug(f"mlb_extract_state failed: {e}")
        return 0

    # Look up the pre-game + live totals lines from odds_snapshots. If
    # either is missing we cannot fire the signal — leave silently.
    pregame_total, live_total, live_over_price, bookmaker = await _lookup_mlb_totals(
        db_path=db_path, event_id=event_id, now=now,
    )
    if pregame_total is None or live_total is None or live_over_price is None:
        return 0

    edge = mlb_quiet_innings_signal(
        inning=int(parsed.get("inning") or 0),
        total_runs=int(parsed.get("total_runs") or 0),
        pregame_total=float(pregame_total),
        live_total=float(live_total),
        live_over_price=int(live_over_price),
    )
    if edge is None:
        return 0
    edge.event_id = event_id
    edge.bookmaker = bookmaker or ""
    try:
        new_id = await emit_edge(edge, db_path=db_path, now=now)
    except Exception as e:
        logger.debug(f"emit_edge failed for {event_id}: {e}")
        return 0
    return 1 if new_id else 0


async def _lookup_mlb_totals(
    *,
    db_path,
    event_id: str,
    now: datetime,
):
    """Return (pregame_total, live_total, live_over_price_american, book)
    pulled from ``odds_snapshots``. Any of the tuple elements may be
    None if the data isn't there.

    Pregame = earliest snapshot for (event_id, totals). Live = most
    recent. Book = the most-recent's bookmaker.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # Does odds_snapshots exist in this DB? If not, bail silently.
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='odds_snapshots' LIMIT 1"
            )
            if not await cur.fetchone():
                return None, None, None, None
            cur = await db.execute(
                "SELECT response_json, fetched_at FROM odds_snapshots "
                "WHERE event_id = ? AND market_type = 'totals' "
                "ORDER BY fetched_at ASC LIMIT 1",
                (event_id,),
            )
            first = await cur.fetchone()
            cur = await db.execute(
                "SELECT response_json, fetched_at FROM odds_snapshots "
                "WHERE event_id = ? AND market_type = 'totals' "
                "ORDER BY fetched_at DESC LIMIT 1",
                (event_id,),
            )
            last = await cur.fetchone()
    except Exception as e:
        logger.debug(f"_lookup_mlb_totals query failed: {e}")
        return None, None, None, None

    if not first or not last:
        return None, None, None, None

    pregame_total = _extract_total_point(first["response_json"])
    live_total, live_over_price, book = _extract_live_over(last["response_json"])
    return pregame_total, live_total, live_over_price, book


def _extract_total_point(response_json) -> float | None:
    """Pull the median totals 'point' value from a snapshot blob."""
    if not response_json:
        return None
    try:
        blob = json.loads(response_json)
    except Exception:
        return None
    points: list[float] = []
    for bm in (blob.get("bookmakers") or []) if isinstance(blob, dict) else []:
        for mkt in bm.get("markets") or []:
            if mkt.get("key") != "totals":
                continue
            for oc in mkt.get("outcomes") or []:
                p = oc.get("point")
                if p is not None:
                    try:
                        points.append(float(p))
                    except Exception:
                        pass
    if not points:
        return None
    points.sort()
    return points[len(points) // 2]


def _extract_live_over(response_json):
    """Pull the current OVER point, OVER price (American), and book from
    the most-recent totals snapshot. Returns (None, None, None) when the
    blob is missing or malformed."""
    if not response_json:
        return None, None, None
    try:
        blob = json.loads(response_json)
    except Exception:
        return None, None, None
    for bm in (blob.get("bookmakers") or []) if isinstance(blob, dict) else []:
        for mkt in bm.get("markets") or []:
            if mkt.get("key") != "totals":
                continue
            for oc in mkt.get("outcomes") or []:
                name = (oc.get("name") or "").lower()
                if name != "over":
                    continue
                try:
                    return (
                        float(oc.get("point")),
                        int(oc.get("price")),
                        bm.get("key") or bm.get("title") or "",
                    )
                except Exception:
                    continue
    return None, None, None


async def evaluate_detectors_for_event(
    event_id: str,
    *,
    db_path=None,
    now=None,
) -> int:
    """Public entry so ``line_monitor`` can fire detectors when a WS
    update arrives for an event that has a live state.

    Reads the two most recent snapshots for ``event_id``, derives sport
    from the most-recent, and runs detectors. Returns the number of
    edges persisted.

    This is invoked on the ODDS-movement path (in addition to the 30s
    poll) so that rapid reactive edges between polls get caught.
    """
    from tools.livestate.storage import _record_edge_emission

    if now is None:
        now = datetime.now(timezone.utc)
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT sport, state_json FROM live_game_states "
                "WHERE event_id = ? ORDER BY ts DESC LIMIT 2",
                (event_id,),
            )
            rows = await cur.fetchall()
    except Exception as e:
        logger.debug(f"evaluate_detectors_for_event query failed: {e}")
        return 0
    if not rows:
        return 0
    try:
        state = json.loads(rows[0]["state_json"])
    except Exception:
        return 0
    prev_state = None
    if len(rows) > 1:
        try:
            prev_state = json.loads(rows[1]["state_json"])
        except Exception:
            prev_state = None
    sport = rows[0]["sport"]
    try:
        emitted = await _evaluate_detectors(
            event_id=event_id,
            sport=sport,
            state=state,
            prev_state=prev_state,
            db_path=db_path,
            now=now,
        )
    except Exception as e:
        logger.debug(f"evaluate_detectors_for_event detector failed: {e}")
        return 0
    if emitted:
        _record_edge_emission(emitted)
    return emitted
