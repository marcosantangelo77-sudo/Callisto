"""tools.lines.ws_stream — event-driven odds ingestion (WS + incremental).

Extracted from tools/line_monitor.py (slice 4). The monitor class keeps
only the state fields and thin delegating methods; the actual WS wiring,
delta handling, and /odds/updated polling live here.

No betting decisions are made in this module — it only ingests snapshots
into the shared _process_snapshot pipeline.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("callisto.line_monitor")


async def start_ws(monitor) -> None:
    """Open the odds-api.io WebSocket and wire updates into the monitor.

    The WS client has its own reconnect loop (5s→60s backoff with jitter)
    inside tools/odds_ws.py, so we just hand it a callback and let it run.
    Imported locally to avoid a hard dep at module import time — lets
    CALLISTO_WS_ENABLED=0 environments skip the websockets package.
    """
    from tools.odds_ws import OddsWebSocket

    monitor._ws_client = OddsWebSocket(
        on_update=monitor._handle_ws_update,
        sports=monitor.WS_SPORTS,
    )
    await monitor._ws_client.start()


async def stop_ws_and_incremental(monitor) -> None:
    """Tear down the WS client and incremental poller. Each teardown is
    wrapped in its own try so a failure in one doesn't block the other."""
    if monitor._ws_client is not None:
        try:
            await monitor._ws_client.stop()
        except Exception as e:
            logger.warning(f"WS stop error: {e}")
        monitor._ws_client = None
    if monitor._incremental_task is not None:
        monitor._incremental_task.cancel()
        try:
            await monitor._incremental_task
        except (asyncio.CancelledError, Exception):
            pass
        monitor._incremental_task = None


async def handle_ws_update(
    monitor,
    data: dict,
    *,
    process_snapshot: Callable[[str, dict], Awaitable[Any]],
    evaluate_live_detectors: Optional[Callable[[str], Awaitable[Any]]] = None,
) -> None:
    """WS callback body — merge a single delta into the latest snapshot.

    Each WS message covers ONE bookmaker's quotes for ONE event across
    several markets. We turn it into a minimal snapshot-shaped payload
    and route through the normal _process_snapshot pipeline so edge
    detection and movement evaluation fire on every delta.

    Additionally fires live-edge detectors for the event if a live game
    state exists — piggy-backing here catches rapid reactive edges
    between polls. Detector failures must NOT break odds ingestion.
    """
    from tools.lines.ingest import ws_update_to_snapshot

    mapped = ws_update_to_snapshot(data)
    if not mapped:
        return
    sport_key, snap = mapped
    snap["ingest_source"] = "ws"
    await process_snapshot(sport_key, snap)

    try:
        for game in (snap.get("games") or [])[:5]:
            eid = str(game.get("id") or "").strip()
            if not eid:
                continue
            if evaluate_live_detectors is not None:
                await evaluate_live_detectors(eid)
    except Exception as e:
        logger.debug(f"WS-path live-edge eval failed: {e}")


def ws_status_fields(monitor) -> dict:
    """Telemetry fields for get_ws_status()."""
    base = {
        "ws_enabled": monitor.WS_ENABLED,
        "incremental_enabled": monitor.INCREMENTAL_ENABLED,
        "ws_updates_received": monitor._ws_updates_received,
        "ws_last_update_ago_s": (
            round(time.time() - monitor._ws_last_update_at, 1)
            if monitor._ws_last_update_at else None
        ),
        "require_model_agreement": monitor.REQUIRE_MODEL_AGREEMENT,
    }
    if monitor._ws_client is not None:
        try:
            base.update({"ws_client": monitor._ws_client.get_status()})
        except Exception:
            pass
    return base


async def incremental_loop(monitor, *, monitored_sports: list[str]) -> None:
    """Poll /odds/updated?since=X every INCREMENTAL_INTERVAL seconds.

    This is the gap-filler between the WS firehose and the 15-min safety
    snapshot: if WS drops for 30s we still catch the delta on the next
    incremental tick. `since` is tracked per-sport so a crash-restart
    still resumes roughly where it left off.
    """
    interval = monitor.INCREMENTAL_INTERVAL
    try:
        from tools.odds_api_io import get_odds_updated as _incremental_fetch
    except Exception:
        logger.warning("odds_api_io.get_odds_updated unavailable — disabling incremental loop")
        return

    while monitor._running:
        try:
            await asyncio.sleep(interval)
            if monitor._paused:
                continue
            now_unix = int(time.time())
            for sport in monitored_sports:
                sport = sport.strip()
                since = monitor._last_incremental_since.get(sport, now_unix - 60)
                try:
                    result = await _incremental_fetch(since, sport=sport)
                except Exception as e:
                    logger.debug(f"Incremental fetch failed for {sport}: {e}")
                    continue
                monitor._last_incremental_since[sport] = now_unix
                if not isinstance(result, dict):
                    continue
                updates = result.get("updates") or []
                if not updates:
                    continue
                # Each update has the same shape as a WS message — reuse
                # the same converter.
                from tools.lines.ingest import ws_update_to_snapshot
                for upd in updates:
                    mapped = ws_update_to_snapshot(upd)
                    if not mapped:
                        continue
                    s_key, snap = mapped
                    snap["ingest_source"] = "incremental"
                    try:
                        await monitor._process_snapshot(s_key, snap)
                    except Exception as e:
                        logger.debug(f"Incremental _process_snapshot failed: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Incremental loop error: {e}")
