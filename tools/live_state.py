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
from datetime import datetime, timedelta, timezone
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

# ESPN rate-limit guards — one semaphore gates concurrent in-flight HTTP.
# ESPN's public endpoints tolerate ~10 req/s sustained before soft-throttling;
# we sit well below that (5 concurrent × ~15-20s round-trip per game).
ESPN_MAX_CONCURRENT = int(os.getenv("CALLISTO_LIVE_ESPN_MAX_CONCURRENT", "5"))
_espn_semaphore: Optional[asyncio.Semaphore] = None

# Active-game threshold for stagger-polling. Above this count we split
# each 30s tick into two batches offset by POLL_INTERVAL_S/2.
STAGGER_THRESHOLD = 20

# Per-sport backoff — populated on 403/429. Value is the wall-clock unix
# time before which we skip this sport entirely.
_sport_backoff_until: dict[str, float] = {}
_sport_backoff_step: dict[str, float] = {}  # current backoff length
BACKOFF_STEPS_S = (30.0, 60.0, 120.0, 300.0)

# Observability counters — reset at module import, exposed via /system/full-status.
_states_collected_counter = 0  # lifetime; "_24h" is derived from DB
_edges_emitted_counter = 0     # lifetime

# Set True once we've verified the live_game_states table exists. If the
# migration hasn't run (fresh DB), the collector self-disables and the
# lifespan logs a warning instead of crashing.
_schema_ok: Optional[bool] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _espn_semaphore
    if _espn_semaphore is None:
        _espn_semaphore = asyncio.Semaphore(ESPN_MAX_CONCURRENT)
    return _espn_semaphore


async def _check_schema(db_path: str) -> bool:
    """Return True iff live_game_states exists. Cached in ``_schema_ok``."""
    global _schema_ok
    if _schema_ok is not None:
        return _schema_ok
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='live_game_states' LIMIT 1"
            )
            row = await cur.fetchone()
            _schema_ok = row is not None
    except Exception as e:
        logger.warning(f"live_game_states schema probe failed: {e}")
        _schema_ok = False
    return _schema_ok


def _is_backed_off(sport: str) -> bool:
    """True if this sport is currently in its cooldown window."""
    until = _sport_backoff_until.get(sport, 0.0)
    return until > time.time()


def _apply_backoff(sport: str) -> float:
    """Escalate backoff for ``sport`` after a hard rate-limit. Returns
    the new cooldown length in seconds. Caps at the last ladder step.
    """
    cur = _sport_backoff_step.get(sport, 0.0)
    # Find next step strictly greater than current; if none, hold at cap.
    next_step = BACKOFF_STEPS_S[-1]
    for step in BACKOFF_STEPS_S:
        if step > cur:
            next_step = step
            break
    _sport_backoff_step[sport] = next_step
    _sport_backoff_until[sport] = time.time() + next_step
    logger.warning(f"ESPN backoff for {sport} -> {next_step:.0f}s")
    return next_step


def _clear_backoff(sport: str) -> None:
    """Reset the backoff ladder for ``sport`` after a clean round."""
    if sport in _sport_backoff_step:
        _sport_backoff_step.pop(sport, None)
        _sport_backoff_until.pop(sport, None)


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


class _RateLimited(Exception):
    """Raised internally so the caller can escalate backoff for a sport."""


async def _list_active_events(sport_key: str) -> list[dict]:
    """Return the set of ESPN event dicts currently in-progress for a sport.

    Raises ``_RateLimited`` on HTTP 403/429 so the caller can apply per-sport
    exponential backoff. Other errors log and return ``[]`` (treat as empty
    round; do NOT back off — might be a transient DNS / connection blip).
    """
    espn = LIVE_SPORTS.get(sport_key)
    if not espn:
        return []
    category, league = espn
    url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
    client = await _get_client()
    sem = _get_semaphore()
    try:
        async with sem:
            resp = await client.get(url)
        if resp.status_code in (403, 429):
            raise _RateLimited(f"HTTP {resp.status_code} on scoreboard")
        resp.raise_for_status()
        data = resp.json()
    except _RateLimited:
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (403, 429):
            raise _RateLimited(str(e))
        logger.warning(f"ESPN scoreboard fetch failed for {sport_key}: {e}")
        return []
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch failed for {sport_key}: {e}")
        return []
    return [e for e in (data.get("events") or []) if _is_active(e)]


async def _fetch_event_summary(sport_key: str, event_id: str) -> Optional[dict]:
    """Return ESPN summary payload for a single event or None on failure.

    Raises ``_RateLimited`` on 403/429 — the sport-level caller decides how
    to fold it into the backoff ladder. Non-rate-limit failures are logged
    and return None (detector just sees no new state this tick).
    """
    espn = LIVE_SPORTS.get(sport_key)
    if not espn:
        return None
    category, league = espn
    url = f"{ESPN_BASE}/{category}/{league}/summary"
    client = await _get_client()
    sem = _get_semaphore()
    try:
        async with sem:
            resp = await client.get(url, params={"event": event_id})
        if resp.status_code in (403, 429):
            raise _RateLimited(f"HTTP {resp.status_code} on summary")
        resp.raise_for_status()
        return resp.json()
    except _RateLimited:
        raise
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (403, 429):
            raise _RateLimited(str(e))
        logger.debug(f"ESPN summary fetch failed for {sport_key}/{event_id}: {e}")
        return None
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
    global _states_collected_counter
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.isoformat()

    # Capture prior state BEFORE the insert so detectors can see the
    # delta. If no prior exists this is the first snapshot of the game.
    prev_state: Optional[dict] = None
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

    _states_collected_counter += 1

    if fire_detectors:
        try:
            emitted = await _evaluate_detectors(
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


def _record_edge_emission(count: int) -> None:
    """Bump the lifetime edges-emitted counter."""
    global _edges_emitted_counter
    _edges_emitted_counter += int(count)


# ──────────────────────────────────────────────────────────────────────
# Detector bridge — keeps the live_edges module loosely coupled. We
# import lazily so unit tests of live_state don't pull the odds stack.
# ──────────────────────────────────────────────────────────────────────


async def _evaluate_detectors(
    *,
    event_id: str,
    sport: str,
    state: dict,
    prev_state: Optional[dict],
    db_path: str,
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
    db_path: str,
    event_id: str,
    now: datetime,
) -> tuple[Optional[float], Optional[float], Optional[int], Optional[str]]:
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


def _extract_total_point(response_json: Optional[str]) -> Optional[float]:
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


def _extract_live_over(
    response_json: Optional[str],
) -> tuple[Optional[float], Optional[int], Optional[str]]:
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
    source=lambda sport, **_: f"espn.live.boxscore.{sport}",
    sla_seconds=180,  # a 3-min gap while games are live means the
                     # poller is broken (poll interval is 30s).
)
async def poll_sport(sport: str, db_path: str = DB_PATH) -> dict:
    """Poll one sport once — fetch every active event's summary and store.

    Respects per-sport backoff: if the sport is currently in a cooldown
    window (because of a recent 403/429), this returns ``{"backoff":true}``
    and tracked_ingestion records the row as a no-op success. Other
    sports stay unaffected.

    Returns ``{"snapshots": <int>, "events": <int>}`` so the ingestion
    tracker extracts a rows_ingested value.
    """
    if sport not in LIVE_SPORTS:
        return {"error": f"unsupported sport: {sport}", "snapshots": 0}
    if _is_backed_off(sport):
        return {"backoff": True, "events": 0, "snapshots": 0}

    try:
        events = await _list_active_events(sport)
    except _RateLimited as e:
        _apply_backoff(sport)
        return {"rate_limited": str(e), "events": 0, "snapshots": 0}

    stored = 0
    hit_rl = False
    for ev in events:
        eid = str(ev.get("id") or "").strip()
        if not eid:
            continue
        try:
            summary = await _fetch_event_summary(sport, eid)
        except _RateLimited:
            # Stop early for this sport this tick; escalate backoff.
            hit_rl = True
            _apply_backoff(sport)
            break
        if not summary:
            continue
        try:
            await store_state(eid, sport, summary, db_path=db_path)
            stored += 1
        except Exception as e:
            logger.warning(f"store_state failed for {sport}/{eid}: {e}")

    if not hit_rl:
        _clear_backoff(sport)
    return {"events": len(events), "snapshots": stored}


# ──────────────────────────────────────────────────────────────────────
# Background loop — the public entrypoint is ``start()``. Typical usage
# is from api.py startup alongside line_monitor.
# ──────────────────────────────────────────────────────────────────────


class LiveStateCollector:
    """Owns the polling task for all LIVE_SPORTS."""

    def __init__(
        self,
        sports: Iterable[str] = tuple(LIVE_SPORTS.keys()),
        *,
        db_path: str = DB_PATH,
    ) -> None:
        self.sports = tuple(sports)
        self.db_path = db_path
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_round_ts = 0.0
        self._last_active_games = 0

    async def start(self) -> None:
        if self._running:
            return
        # Schema guard — if migration hasn't run, don't create the task.
        ok = await _check_schema(self.db_path)
        if not ok:
            logger.warning(
                "live_game_states table missing — live-state collector disabled. "
                "Run migrations 005_live_game_states to enable."
            )
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
            "active_games_polling": self._last_active_games,
            "states_collected_lifetime": _states_collected_counter,
            "edges_emitted_lifetime": _edges_emitted_counter,
            "backoff_sports": {
                s: max(0.0, round(_sport_backoff_until[s] - time.time(), 1))
                for s in _sport_backoff_until
                if _sport_backoff_until.get(s, 0.0) > time.time()
            },
        }

    async def _run(self) -> None:
        while self._running:
            t0 = time.monotonic()
            # First pass: count active events across all sports so we can
            # decide whether to stagger. We DON'T spend a semaphore slot
            # here — scoreboard calls are cheap (1/sport) and already
            # gated by the semaphore inside _list_active_events.
            counts = await self._count_active_per_sport()
            self._last_active_games = sum(counts.values())

            if self._last_active_games > STAGGER_THRESHOLD:
                # Split sports into two batches, second fires at +half-interval.
                ordered = sorted(self.sports, key=lambda s: -counts.get(s, 0))
                half = (len(ordered) + 1) // 2
                await self._poll_batch(ordered[:half])
                await asyncio.sleep(POLL_INTERVAL_S / 2.0)
                await self._poll_batch(ordered[half:])
            else:
                await self._poll_batch(self.sports)

            self._last_round_ts = time.time()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(1.0, POLL_INTERVAL_S - elapsed))

    async def _count_active_per_sport(self) -> dict[str, int]:
        """Best-effort count of in-progress events per sport — used to
        decide whether to stagger. Failures return 0 (the subsequent
        poll will still run and surface the failure through
        tracked_ingestion)."""
        out: dict[str, int] = {}
        for sport in self.sports:
            if _is_backed_off(sport):
                out[sport] = 0
                continue
            try:
                events = await _list_active_events(sport)
                out[sport] = len(events)
            except _RateLimited:
                _apply_backoff(sport)
                out[sport] = 0
            except Exception:
                out[sport] = 0
        return out

    async def _poll_batch(self, sports: Iterable[str]) -> None:
        for sport in sports:
            if not self._running:
                return
            try:
                await poll_sport(sport, db_path=self.db_path)
            except Exception as e:
                # tracked_ingestion already recorded the failure row;
                # we just keep the loop alive.
                logger.debug(f"poll_sport({sport}) raised: {e}")


_collector: Optional[LiveStateCollector] = None


async def start_collector(db_path: str = DB_PATH) -> Optional[LiveStateCollector]:
    """Construct + start the process-wide collector.

    Returns the collector instance, or None if the underlying schema is
    missing (see ``LiveStateCollector.start``). Callers in api.py
    lifespan should tolerate None.
    """
    global _collector
    if _collector is None:
        _collector = LiveStateCollector(db_path=db_path)
    await _collector.start()
    if not _collector._running:
        # start() bailed — null it out so a later retry (e.g. after
        # migrations apply) can re-attempt cleanly.
        _collector = None
        return None
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


async def get_collector_counters_24h(db_path: str = DB_PATH) -> dict:
    """Return 24h ground-truth counts from the DB — source of truth for
    observability (the in-memory counters reset on restart).

    Keys: ``states_collected_24h``, ``edges_emitted_24h``.
    """
    out = {"states_collected_24h": 0, "edges_emitted_24h": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        async with aiosqlite.connect(db_path) as db:
            # Table existence probes — fresh DBs won't have these.
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='live_game_states' LIMIT 1"
            )
            if await cur.fetchone():
                cur = await db.execute(
                    "SELECT COUNT(*) FROM live_game_states WHERE ts >= ?",
                    (cutoff,),
                )
                row = await cur.fetchone()
                out["states_collected_24h"] = int((row or [0])[0])
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='live_edge_emissions' LIMIT 1"
            )
            if await cur.fetchone():
                cur = await db.execute(
                    "SELECT COUNT(*) FROM live_edge_emissions WHERE emitted_at >= ?",
                    (cutoff,),
                )
                row = await cur.fetchone()
                out["edges_emitted_24h"] = int((row or [0])[0])
    except Exception as e:
        logger.debug(f"get_collector_counters_24h failed: {e}")
    return out


async def evaluate_detectors_for_event(
    event_id: str,
    *,
    db_path: str = DB_PATH,
    now: Optional[datetime] = None,
) -> int:
    """Public entry so ``line_monitor`` can fire detectors when a WS
    update arrives for an event that has a live state.

    Reads the two most recent snapshots for ``event_id``, derives sport
    from the most-recent, and runs detectors. Returns the number of
    edges persisted.

    This is invoked on the ODDS-movement path (in addition to the 30s
    poll) so that rapid reactive edges between polls get caught.
    """
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
    "get_collector_counters_24h",
    "evaluate_detectors_for_event",
]
