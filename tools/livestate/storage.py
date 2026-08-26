"""SQLite storage primitives for live game-state snapshots.

Split out of ``tools/live_state.py``. Shared mutable state (schema-ok
cache, observability counters) is read/written through the
``tools.live_state`` facade so existing resets keep working.
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


async def _check_schema(db_path: str) -> bool:
    """Return True iff live_game_states exists. Cached in ``_schema_ok``."""
    fs = _fs()
    if fs._schema_ok is not None:
        return fs._schema_ok
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='live_game_states' LIMIT 1"
            )
            row = await cur.fetchone()
            fs._schema_ok = row is not None
    except Exception as e:
        logger.warning(f"live_game_states schema probe failed: {e}")
        fs._schema_ok = False
    return fs._schema_ok


async def _prune_for_event(db: aiosqlite.Connection, event_id: str, now: datetime) -> None:
    """Delete rows older than RETENTION_SECONDS for this event.

    We delete by string comparison against an ISO timestamp floor because
    all writers stamp UTC ISO strings (lex order == chrono order for
    ``YYYY-MM-DDTHH:MM:SS+00:00``).
    """
    cutoff = now.timestamp() - _fs().RETENTION_SECONDS
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
    await db.execute(
        "DELETE FROM live_game_states WHERE event_id = ? AND ts < ?",
        (event_id, cutoff_iso),
    )


async def _enforce_hard_cap(db: aiosqlite.Connection) -> None:
    """Truncate the oldest rows if we cross the hard row cap.

    Cheap: we only run this when row-count exceeds HARD_ROW_CAP, and
    SQLite keeps an estimate available via a cheap ``COUNT(*)`` on an
    indexed table. In practice this triggers rarely.
    """
    row = await (
        await db.execute("SELECT COUNT(*) FROM live_game_states")
    ).fetchone()
    total = int((row or [0])[0])
    cap = _fs().HARD_ROW_CAP
    if total <= cap:
        return
    excess = total - cap
    await db.execute(
        "DELETE FROM live_game_states WHERE id IN ("
        "  SELECT id FROM live_game_states ORDER BY id ASC LIMIT ?"
        ")",
        (excess,),
    )
    logger.warning(f"live_game_states cap exceeded; truncated {excess} oldest rows")


async def store_state(
    event_id: str,
    sport: str,
    state_json: dict,
    *,
    db_path: str | None = None,
    now: datetime | None = None,
    fire_detectors: bool = True,
) -> int:
    """Insert one live-state row and enforce retention. Returns the new id.

    After the commit we synchronously (but cheaply) evaluate the relevant
    live-edge detectors against the new state + the prior snapshot for
    the same event. Detector failures are caught and logged so a single
    bad ESPN payload never breaks ingestion.

    Exposed as a primitive so tests can drive the detector directly
    without having to go through ESPN. Set ``fire_detectors=False`` to
    skip evaluation — useful in unit tests that only want the row write.
    """
    fs = _fs()
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.isoformat()

    # Capture prior state BEFORE the insert so detectors can see the
    # delta. If no prior exists this is the first snapshot of the game.
    prev_state = None
    if fire_detectors:
        try:
            prior = await recent_states(event_id, limit=1, db_path=db_path)
            if prior:
                prev_state = prior[0].get("state")
        except Exception as e:
            logger.debug(f"recent_states lookup failed for {event_id}: {e}")

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        cursor = await db.execute(
            "INSERT INTO live_game_states (event_id, sport, ts, state_json) "
            "VALUES (?, ?, ?, ?)",
            (event_id, sport, ts, json.dumps(state_json, default=str)),
        )
        await _prune_for_event(db, event_id, now)
        # Hard cap check is relatively cheap — run it opportunistically
        # (every insert) to avoid needing a separate sweeper task. At
        # scale you could gate it behind a random sample.
        await _enforce_hard_cap(db)
        await db.commit()
        inserted_id = int(cursor.lastrowid or 0)

    fs._states_collected_counter += 1

    if fire_detectors:
        try:
            emitted = await _evaluate_detectors_call(
                event_id=event_id,
                sport=sport,
                state=state_json,
                prev_state=prev_state,
                db_path=db_path,
                now=now,
            )
            if emitted:
                _record_edge_emission(emitted)
        except Exception as e:
            # Detectors MUST NOT break ingestion — log + continue.
            logger.warning(f"detector eval failed for {sport}/{event_id}: {e}")

    return inserted_id


async def _evaluate_detectors_call(
    *,
    event_id: str,
    sport: str,
    state: dict,
    prev_state,
    db_path,
    now: datetime,
) -> int:
    """Late-bound bridge to ``detectors._evaluate_detectors`` (avoids a
    circular import between storage and detectors)."""
    from tools.livestate.detectors import _evaluate_detectors as _ev

    return await _ev(
        event_id=event_id,
        sport=sport,
        state=state,
        prev_state=prev_state,
        db_path=db_path,
        now=now,
    )


def _record_edge_emission(count: int) -> None:
    """Bump the lifetime edges-emitted counter."""
    _fs()._edges_emitted_counter += int(count)


async def recent_states(
    event_id: str,
    *,
    limit: int = 20,
    db_path: str | None = None,
) -> list[dict]:
    """Return the N most-recent state snapshots for an event, newest-first.

    Each row is a dict with keys: ``ts`` (ISO str), ``state`` (parsed
    JSON). Returns empty list if the event has no rows.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT ts, state_json FROM live_game_states "
            "WHERE event_id = ? ORDER BY ts DESC LIMIT ?",
            (event_id, int(limit)),
        )
        rows = await cur.fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append({"ts": r["ts"], "state": json.loads(r["state_json"])})
        except Exception:
            continue
    return out
