"""
Game-time scheduler — calendar-driven triggers relative to game start times.

Maintains a calendar of upcoming games from the markets table (already populated
by every scraper cycle). Fires events on the event bus at configurable offsets
before/after game time:
  - T-60min: game_starting (increase snapshot frequency)
  - T-15min: game_imminent (pre-game edge scan)

Also supports cron-like periodic tasks.

The scheduler reads commence_time from existing data — zero new API calls.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from tools.event_bus import (
    EventBus,
    EVENT_GAME_STARTING,
    EVENT_GAME_IMMINENT,
    get_event_bus,
)

logger = logging.getLogger("callisto.game_scheduler")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# How often the scheduler checks for trigger conditions
TICK_INTERVAL = 30  # seconds

# How often to refresh the game calendar from the database
CALENDAR_REFRESH_INTERVAL = 1800  # 30 minutes


@dataclass
class GameTrigger:
    """A trigger relative to game time."""
    offset_minutes: int       # negative = before game, positive = after
    event_type: str           # event bus event to fire
    extra_data: dict = field(default_factory=dict)
    fired: bool = False


@dataclass
class ScheduledGame:
    """An upcoming game with registered triggers."""
    sport: str
    event_id: str
    home_team: str
    away_team: str
    commence_time: datetime
    triggers: list[GameTrigger] = field(default_factory=list)


class GameScheduler:
    """Calendar-driven game-time scheduler with event bus integration."""

    def __init__(self, event_bus: Optional[EventBus] = None, db_path: str = DB_PATH):
        self._event_bus = event_bus or get_event_bus()
        self._db_path = db_path
        self._games: dict[str, ScheduledGame] = {}  # event_id -> game
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_refresh: float = 0
        self._triggers_fired = 0

    async def start(self) -> None:
        """Start the scheduler background loop."""
        self._running = True
        await self.refresh_calendar()
        self._task = asyncio.create_task(self._loop())
        logger.info(f"Game scheduler started with {len(self._games)} upcoming games")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def refresh_calendar(self) -> int:
        """
        Load upcoming games from markets table.
        Returns count of games loaded.
        """
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT DISTINCT event_id, sport, home_team, away_team, commence_time "
                    "FROM markets "
                    "WHERE commence_time > datetime('now', '-2 hours') "
                    "AND commence_time < datetime('now', '+48 hours') "
                    "AND event_id IS NOT NULL "
                    "ORDER BY commence_time"
                )
                rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"Calendar refresh failed: {e}")
            return 0

        new_games = {}
        for event_id, sport, home, away, commence_str in rows:
            if not event_id or not commence_str:
                continue

            # Parse commence_time
            try:
                if "T" in commence_str:
                    ct = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                else:
                    ct = datetime.strptime(commence_str, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
            except (ValueError, TypeError):
                continue

            # Preserve existing triggers' fired state
            existing = self._games.get(event_id)
            if existing:
                new_games[event_id] = existing
                new_games[event_id].commence_time = ct
            else:
                game = ScheduledGame(
                    sport=sport or "",
                    event_id=event_id,
                    home_team=home or "",
                    away_team=away or "",
                    commence_time=ct,
                    triggers=self._default_triggers(),
                )
                new_games[event_id] = game

        self._games = new_games
        self._last_refresh = asyncio.get_event_loop().time()
        return len(new_games)

    def _default_triggers(self) -> list[GameTrigger]:
        """Default triggers for each game."""
        return [
            GameTrigger(
                offset_minutes=-60,
                event_type=EVENT_GAME_STARTING,
                extra_data={"reason": "T-60min: increase snapshot frequency"},
            ),
            GameTrigger(
                offset_minutes=-15,
                event_type=EVENT_GAME_IMMINENT,
                extra_data={"reason": "T-15min: pre-game edge scan"},
            ),
        ]

    async def _loop(self) -> None:
        """Main scheduler loop — checks triggers every TICK_INTERVAL seconds."""
        while self._running:
            try:
                await asyncio.sleep(TICK_INTERVAL)

                # Periodic calendar refresh
                now_mono = asyncio.get_event_loop().time()
                if now_mono - self._last_refresh > CALENDAR_REFRESH_INTERVAL:
                    count = await self.refresh_calendar()
                    logger.debug(f"Calendar refreshed: {count} games")

                # Check triggers
                now = datetime.now(timezone.utc)
                for game in self._games.values():
                    for trigger in game.triggers:
                        if trigger.fired:
                            continue
                        trigger_time = game.commence_time + timedelta(
                            minutes=trigger.offset_minutes
                        )
                        if now >= trigger_time:
                            trigger.fired = True
                            self._triggers_fired += 1
                            await self._event_bus.publish(
                                trigger.event_type,
                                {
                                    "sport": game.sport,
                                    "event_id": game.event_id,
                                    "home_team": game.home_team,
                                    "away_team": game.away_team,
                                    "commence_time": game.commence_time.isoformat(),
                                    "minutes_until": round(
                                        (game.commence_time - now).total_seconds() / 60, 1
                                    ),
                                    **trigger.extra_data,
                                },
                            )
                            logger.info(
                                f"Trigger fired: {trigger.event_type} for "
                                f"{game.away_team} @ {game.home_team} "
                                f"({game.sport}, T{trigger.offset_minutes:+d}min)"
                            )

                # Prune old games (>4 hours past commence)
                cutoff = now - timedelta(hours=4)
                self._games = {
                    eid: g for eid, g in self._games.items()
                    if g.commence_time > cutoff
                }

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Scheduler tick error: {e}")

    def get_upcoming(self, hours: int = 24) -> list[dict]:
        """Get upcoming games within the next N hours."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)
        result = []
        for game in sorted(self._games.values(), key=lambda g: g.commence_time):
            if game.commence_time <= cutoff:
                result.append({
                    "sport": game.sport,
                    "event_id": game.event_id,
                    "home_team": game.home_team,
                    "away_team": game.away_team,
                    "commence_time": game.commence_time.isoformat(),
                    "minutes_until": round(
                        (game.commence_time - now).total_seconds() / 60, 1
                    ),
                    "triggers_pending": sum(
                        1 for t in game.triggers if not t.fired
                    ),
                })
        return result

    def get_stats(self) -> dict:
        """Return scheduler statistics."""
        now = datetime.now(timezone.utc)
        return {
            "games_tracked": len(self._games),
            "triggers_fired_total": self._triggers_fired,
            "next_game": min(
                (g.commence_time for g in self._games.values()),
                default=None,
            ),
            "games_starting_within_1hr": sum(
                1 for g in self._games.values()
                if 0 <= (g.commence_time - now).total_seconds() <= 3600
            ),
        }
