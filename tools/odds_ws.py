"""
Odds-API.io WebSocket client — real-time odds streaming.

Connects to wss://api.odds-api.io/v3/ws for sub-150ms odds updates.
Streams pre-match and live odds for all monitored US sports, feeding
changes directly into the line monitor and edge scanner.

Constraints:
  - One connection per API key (new connection kills old)
  - markets parameter required
  - Max 10 sports, 20 markets, 50 event IDs per connection
  - Cannot combine leagues and eventIds filters

Message types: welcome, created, updated, deleted, no_markets
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.odds_ws")

ODDS_API_IO_KEY = os.getenv("ODDS_API_IO_KEY", "")
WS_BASE = "wss://api.odds-api.io/v3/ws"

# US sports to stream
DEFAULT_SPORTS = "basketball,american-football,baseball,ice-hockey"
# Markets to stream
DEFAULT_MARKETS = "ML,Spread,Totals"


class OddsWebSocket:
    """Persistent WebSocket connection for real-time odds streaming."""

    def __init__(
        self,
        on_update: Optional[Callable] = None,
        sports: str = DEFAULT_SPORTS,
        markets: str = DEFAULT_MARKETS,
        status: str = "prematch",
    ):
        self.on_update = on_update or self._default_handler
        self.sports = sports
        self.markets = markets
        self.status = status
        self._ws = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._updates_received = 0
        self._last_update_time = 0.0
        self._connected_at = 0.0
        self._reconnects = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def url(self) -> str:
        return (
            f"{WS_BASE}?apiKey={ODDS_API_IO_KEY}"
            f"&markets={self.markets}"
            f"&sport={self.sports}"
            f"&status={self.status}"
        )

    def get_status(self) -> dict:
        return {
            "connected": self._ws is not None and not self._ws.closed if self._ws else False,
            "running": self._running,
            "updates_received": self._updates_received,
            "last_update_ago_seconds": round(time.time() - self._last_update_time, 1) if self._last_update_time else None,
            "connected_since": self._connected_at,
            "reconnects": self._reconnects,
            "sports": self.sports,
            "markets": self.markets,
            "status": self.status,
        }

    async def start(self) -> None:
        """Start the WebSocket connection in a background task."""
        if self._running:
            logger.warning("WebSocket already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info(f"Odds WebSocket started (sports={self.sports}, markets={self.markets})")

    async def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Odds WebSocket stopped")

    async def _run_forever(self) -> None:
        """Main loop with automatic reconnection."""
        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**23,  # 8MB — exchange data with depth can be large
                ) as ws:
                    self._ws = ws
                    self._connected_at = time.time()
                    self._reconnect_delay = 1.0  # Reset backoff on success
                    logger.info("Odds WebSocket connected")

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            # Handle both str and bytes messages
                            if isinstance(message, bytes):
                                message = message.decode("utf-8", errors="replace")
                            data = json.loads(message)
                            msg_type = data.get("type", "")

                            if msg_type == "welcome":
                                books = data.get("bookmakers", [])
                                logger.info(
                                    f"WS welcome: {len(books)} bookmakers, "
                                    f"filters={data.get('filters', {})}"
                                )

                            elif msg_type in ("updated", ""):
                                # Some update messages lack a "type" field —
                                # detect by presence of bookie/markets fields
                                if "bookie" in data or "markets" in data or msg_type == "updated":
                                    self._updates_received += 1
                                    self._last_update_time = time.time()
                                    await self.on_update(data)

                            elif msg_type == "created":
                                logger.debug(
                                    f"WS new event: {data.get('id')} "
                                    f"{data.get('home', '')} vs {data.get('away', '')}"
                                )

                            elif msg_type == "deleted":
                                logger.debug(f"WS event removed: {data.get('id')}")

                        except json.JSONDecodeError:
                            logger.debug(f"WS parse error (len={len(message)})")
                        except Exception as e:
                            logger.warning(f"WS handler error: {e}")

            except websockets.ConnectionClosed as e:
                logger.warning(f"WS connection closed: {e.code} {e.reason}")
            except Exception as e:
                logger.warning(f"WS connection error: {e}")

            # Reconnect with exponential backoff
            if self._running:
                self._reconnects += 1
                self._ws = None
                logger.info(f"WS reconnecting in {self._reconnect_delay:.0f}s (attempt #{self._reconnects})")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    @staticmethod
    async def _default_handler(data: dict) -> None:
        """Default handler — logs updates."""
        bookie = data.get("bookie", "?")
        event_id = data.get("id", "?")
        markets = data.get("markets", [])
        market_names = [m.get("name", "?") for m in markets]
        logger.debug(f"WS update: event={event_id} book={bookie} markets={market_names}")


# Module-level singleton
_ws_client: Optional[OddsWebSocket] = None


async def start_odds_stream(on_update: Optional[Callable] = None) -> OddsWebSocket:
    """Start the global WebSocket odds stream."""
    global _ws_client
    if _ws_client and _ws_client._running:
        return _ws_client
    _ws_client = OddsWebSocket(on_update=on_update)
    await _ws_client.start()
    return _ws_client


async def stop_odds_stream() -> None:
    """Stop the global WebSocket odds stream."""
    global _ws_client
    if _ws_client:
        await _ws_client.stop()
        _ws_client = None


def get_ws_status() -> dict:
    """Get WebSocket connection status."""
    if _ws_client:
        return _ws_client.get_status()
    return {"connected": False, "running": False}
