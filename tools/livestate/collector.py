"""Background polling loop + process-wide collector lifecycle.

Split out of ``tools/live_state.py``: ``poll_sport`` (tracked
ingestion), ``LiveStateCollector``, and the ``start_collector`` /
``stop_collector`` / status / 24h-counter helpers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

import aiosqlite

from tools.ingestion_tracking import tracked_ingestion

logger = logging.getLogger("callisto.live_state")


def _fs():
    from tools import live_state as fs

    return fs


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
async def poll_sport(sport: str, db_path=None) -> dict:
    """Poll one sport once — fetch every active event's summary and store.

    Respects per-sport backoff: if the sport is currently in a cooldown
    window (because of a recent 403/429), this returns ``{"backoff":true}``
    and tracked_ingestion records the row as a no-op success. Other
    sports stay unaffected.

    Returns ``{"snapshots": <int>, "events": <int>}`` so the ingestion
    tracker extracts a rows_ingested value.
    """
    fs = _fs()
    # Resolve helpers through the facade so monkeypatches applied to
    # tools.live_state propagate (same behavior as pre-split monolith).
    from tools.livestate.storage import store_state

    _RateLimited = fs._RateLimited
    _apply_backoff = fs._apply_backoff
    _clear_backoff = fs._clear_backoff
    _fetch_event_summary = fs._fetch_event_summary
    _is_backed_off = fs._is_backed_off
    _list_active_events = fs._list_active_events

    if sport not in fs.LIVE_SPORTS:
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
        sports: Iterable[str] | None = None,
        *,
        db_path=None,
    ) -> None:
        fs = _fs()
        self.sports = tuple(fs.LIVE_SPORTS.keys()) if sports is None else tuple(sports)
        self.db_path = db_path
        self._running = False
        self._task = None
        self._last_round_ts = 0.0
        self._last_active_games = 0

    async def start(self) -> None:
        if self._running:
            return
        # Schema guard — if migration hasn't run, don't create the task.
        from tools.livestate.storage import _check_schema

        ok = await _check_schema(self.db_path or "memory/callisto.db")
        if not ok:
            logger.warning(
                "live_game_states table missing — live-state collector disabled. "
                "Run migrations 007_live_game_states to enable."
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
        from tools.livestate.espn import close_client

        await close_client()
        logger.info("Live state collector stopped")

    def status(self) -> dict:
        fs = _fs()
        backoff_until = fs._sport_backoff_until
        return {
            "running": self._running,
            "sports": list(self.sports),
            "last_round_age_s": round(time.time() - self._last_round_ts, 1)
                if self._last_round_ts else None,
            "active_games_polling": self._last_active_games,
            "states_collected_lifetime": self._states_collected_lifetime(),
            "edges_emitted_lifetime": self._edges_emitted_lifetime(),
            "backoff_sports": {
                s: max(0.0, round(backoff_until[s] - time.time(), 1))
                for s in backoff_until
                if backoff_until.get(s, 0.0) > time.time()
            },
        }

    @staticmethod
    def _states_collected_lifetime() -> int:
        return _fs()._states_collected_counter

    @staticmethod
    def _edges_emitted_lifetime() -> int:
        return _fs()._edges_emitted_counter

    async def _run(self) -> None:
        while self._running:
            t0 = time.monotonic()
            # First pass: count active events across all sports so we can
            # decide whether to stagger. We DON'T spend a semaphore slot
            # here — scoreboard calls are cheap (1/sport) and already
            # gated by the semaphore inside _list_active_events.
            counts = await self._count_active_per_sport()
            self._last_active_games = sum(counts.values())

            if self._last_active_games > _fs().STAGGER_THRESHOLD:
                # Split sports into two batches, second fires at +half-interval.
                ordered = sorted(self.sports, key=lambda s: -counts.get(s, 0))
                half = (len(ordered) + 1) // 2
                await self._poll_batch(ordered[:half])
                await asyncio.sleep(_fs().POLL_INTERVAL_S / 2.0)
                await self._poll_batch(ordered[half:])
            else:
                await self._poll_batch(self.sports)

            self._last_round_ts = time.time()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(1.0, _fs().POLL_INTERVAL_S - elapsed))

    async def _count_active_per_sport(self) -> dict[str, int]:
        """Best-effort count of in-progress events per sport — used to
        decide whether to stagger. Failures return 0 (the subsequent
        poll will still run and surface the failure through
        tracked_ingestion)."""
        fs = _fs()
        # Resolve through the facade so monkeypatches propagate.
        _RateLimited = fs._RateLimited
        _apply_backoff = fs._apply_backoff
        _is_backed_off = fs._is_backed_off
        _list_active_events = fs._list_active_events

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


async def start_collector(db_path=None):
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


def set_collector_for_tests(c) -> None:
    """Test hook — replace the process-wide collector handle."""
    global _collector
    _collector = c


async def get_collector_counters_24h(db_path=None) -> dict:
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
