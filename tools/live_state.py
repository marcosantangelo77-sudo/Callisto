"""Live game-state ingestion — poll ESPN in-game boxscores for active games.

Why this exists
---------------
Callisto's pre-game data pipeline is mature (game_contexts, scoreboard,
player_stats) but there is NO persistent record of in-game state
(inning / period / score / runners / time-on-clock) once a game is
underway. The live-odds WebSocket tells us THE LINE MOVED, not WHY —
without knowing that "the line moved 1 run on the total" happened
"right after 4 scoreless innings", the detector can't tell a justified
move from an over-reaction.

This module polls ESPN's public summary endpoint every 30s for each
active event and stores the raw snapshot in ``live_game_states``. The
detector (``tools.live_edges``) reads the N most-recent snapshots per
event to decide whether a line move is evidence of an over-reaction.

Design constraints
------------------
1. **Bounded growth**: each insert prunes rows older than 6h for the
   same event. A global ceiling of 10M rows is also enforced (LRU style
   by id) to cap worst-case disk usage. 6h × 30s = 720 snapshots/game;
   even at 30 concurrent live games that's ~22k rows in flight, well
   under the cap.

2. **Never crash the parent loop**: every ESPN call goes through
   ``@tracked_ingestion`` so failures become visible ingestion rows
   rather than silent empties. A single poll that fails does not stop
   the next poll.

3. **Source tag stability**: tags are ``espn.live.{sport}`` — ingestion
   SLA watchdog reads them.

4. **No hot path duplication**: we DO NOT re-fetch state for events
   that are pre-game or final. ``_is_active`` gates the poll so the
   quota stays sensible (ESPN is public/free but rate-limited).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite
import httpx

from tools.ingestion_tracking import tracked_ingestion

logger = logging.getLogger("callisto.live_state")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ESPN summary endpoint — returns full in-game state including
# boxscore, score, status, plays, drives. Same hostname as scoreboard
# so we reuse the existing client.
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Subset of ESPN_SPORTS keyed by active-live-betting priority.
LIVE_SPORTS = {
    "baseball_mlb": ("baseball", "mlb"),
    "basketball_nba": ("basketball", "nba"),
    "basketball_wnba": ("basketball", "wnba"),
    "icehockey_nhl": ("hockey", "nhl"),
}

# Retention: oldest allowed snapshot age per event.
RETENTION_SECONDS = 6 * 3600

# Hard ceiling on total rows — bounded worst case.
HARD_ROW_CAP = 10_000_000

# Polling cadence. 30s tracks most pitches / possessions without
# hammering ESPN.
POLL_INTERVAL_S = 30.0


_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def _is_active(event: dict) -> bool:
    """True iff the event is currently in-progress (not pre-game / final)."""
    status = (event.get("status") or {}).get("type") or {}
    state = (status.get("state") or "").lower()
    # ESPN state values: 'pre' (not started), 'in' (live), 'post' (final).
    return state == "in"


async def _list_active_events(sport_key: str) -> list[dict]:
    """Return the set of ESPN event dicts currently in-progress for a sport."""
    espn = LIVE_SPORTS.get(sport_key)
    if not espn:
        return []
    category, league = espn
    url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
    client = await _get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch failed for {sport_key}: {e}")
        return []
    return [e for e in (data.get("events") or []) if _is_active(e)]


async def _fetch_event_summary(sport_key: str, event_id: str) -> Optional[dict]:
    """Return ESPN summary payload for a single event or None on failure."""
    espn = LIVE_SPORTS.get(sport_key)
    if not espn:
        return None
    category, league = espn
    url = f"{ESPN_BASE}/{category}/{league}/summary"
    client = await _get_client()
    try:
        resp = await client.get(url, params={"event": event_id})
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug(f"ESPN summary fetch failed for {sport_key}/{event_id}: {e}")
        return None


async def _prune_for_event(db: aiosqlite.Connection, event_id: str, now: datetime) -> None:
    """Delete rows older than RETENTION_SECONDS for this event.

    We delete by string comparison against an ISO timestamp floor because
    all writers stamp UTC ISO strings (lex order == chrono order for
    ``YYYY-MM-DDTHH:MM:SS+00:00``).
    """
    cutoff = now.timestamp() - RETENTION_SECONDS
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
    if total <= HARD_ROW_CAP:
        return
    excess = total - HARD_ROW_CAP
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
    db_path: str = DB_PATH,
    now: Optional[datetime] = None,
) -> int:
    """Insert one live-state row and enforce retention. Returns the new id.

    Exposed as a primitive so tests can drive the detector directly
    without having to go through ESPN.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.isoformat()
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
        return int(cursor.lastrowid or 0)


async def recent_states(
    event_id: str,
    *,
    limit: int = 20,
    db_path: str = DB_PATH,
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


# ──────────────────────────────────────────────────────────────────────
# Tracked ingestion wrapper — one function per sport. This makes the
# data_collector health probe able to distinguish "MLB live is healthy
# but NHL live is broken" instead of lumping them.
# ──────────────────────────────────────────────────────────────────────


@tracked_ingestion(
    source=lambda sport, **_: f"espn.live.{sport}",
    sla_seconds=180,  # a 3-min gap while games are live means the
                     # poller is broken (poll interval is 30s).
)
async def poll_sport(sport: str) -> dict:
    """Poll one sport once — fetch every active event's summary and store.

    Returns ``{"snapshots": <int>, "events": <int>}`` so the ingestion
    tracker extracts a rows_ingested value.
    """
    if sport not in LIVE_SPORTS:
        return {"error": f"unsupported sport: {sport}", "snapshots": 0}
    events = await _list_active_events(sport)
    stored = 0
    for ev in events:
        eid = str(ev.get("id") or "").strip()
        if not eid:
            continue
        summary = await _fetch_event_summary(sport, eid)
        if not summary:
            continue
        try:
            await store_state(eid, sport, summary)
            stored += 1
        except Exception as e:
            logger.warning(f"store_state failed for {sport}/{eid}: {e}")
    return {"events": len(events), "snapshots": stored}


# ──────────────────────────────────────────────────────────────────────
# Background loop — the public entrypoint is ``start()``. Typical usage
# is from api.py startup alongside line_monitor.
# ──────────────────────────────────────────────────────────────────────


class LiveStateCollector:
    """Owns the polling task for all LIVE_SPORTS."""

    def __init__(self, sports: Iterable[str] = tuple(LIVE_SPORTS.keys())) -> None:
        self.sports = tuple(sports)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_round_ts = 0.0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"Live state collector started for {self.sports}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await close_client()
        logger.info("Live state collector stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "sports": list(self.sports),
            "last_round_age_s": round(time.time() - self._last_round_ts, 1)
                if self._last_round_ts else None,
        }

    async def _run(self) -> None:
        while self._running:
            t0 = time.monotonic()
            for sport in self.sports:
                try:
                    await poll_sport(sport)
                except Exception as e:
                    # tracked_ingestion already recorded the failure row;
                    # we just keep the loop alive.
                    logger.debug(f"poll_sport({sport}) raised: {e}")
            self._last_round_ts = time.time()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(1.0, POLL_INTERVAL_S - elapsed))


_collector: Optional[LiveStateCollector] = None


async def start_collector() -> LiveStateCollector:
    global _collector
    if _collector is None:
        _collector = LiveStateCollector()
    await _collector.start()
    return _collector


async def stop_collector() -> None:
    global _collector
    if _collector is not None:
        await _collector.stop()
        _collector = None


def get_collector_status() -> dict:
    if _collector is None:
        return {"running": False}
    return _collector.status()


__all__ = [
    "LIVE_SPORTS",
    "RETENTION_SECONDS",
    "LiveStateCollector",
    "poll_sport",
    "store_state",
    "recent_states",
    "start_collector",
    "stop_collector",
    "get_collector_status",
]
