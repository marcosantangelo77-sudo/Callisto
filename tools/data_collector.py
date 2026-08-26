"""
Organic data collector — free APIs for game stats, player data, and context.

Feeds the embedding pipeline and prop resolution engine with real data.
All sources are free (no API key required):
  - ESPN API: scores, player stats, injuries, schedules
  - The Odds API scores endpoint (free, no credit cost)

Data flow:
  1. After games complete → collect final scores + player box scores
  2. Store in game_contexts and player_stats tables
  3. Mark as ready for embedding
  4. Resolve outstanding paper trade props with actual stats

ESPN API is undocumented but stable. Endpoints used:
  - scoreboard: live/recent scores
  - boxscore: full player stats per game
  - injuries: team injury reports

SPLIT (2026-08): the collector implementations moved into ``tools/collect/``
(one module per source domain: espn, odds, resolution, play_by_play,
baseball, hockey, football, basketball, golf, venues, http). This file is
now a facade — it re-exports everything under the original names so that
existing imports (``from tools.data_collector import DataCollector``,
``close_client``, etc.) keep working unchanged. Each method on DataCollector
delegates to the corresponding module-level function in tools.collect,
passing itself as the ``dc`` first argument (the collectors only touch
``dc._db``, ``dc.db_path`` and ``dc._player_stat_insert_failures``).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

# Re-export the public surface of the split modules so existing imports
# (`from tools.data_collector import ESPN_SPORTS`, `VENUE_METADATA`, ...)
# continue to resolve.
from tools.collect.http import _get_client, _get_client_lock, close_client  # noqa: F401
from tools.collect.venues import VENUE_METADATA, _get_venue_metadata  # noqa: F401
from tools.collect.espn import (  # noqa: F401
    ESPN_BASE,
    ESPN_SPORTS,
    collect_box_scores,
    collect_date_range,
    collect_scores,
    get_today_event_ids,
    store_player_stats,
)
from tools.collect.odds import (  # noqa: F401
    ESPN_CORE_BASE,
    ESPN_CORE_LEAGUES,
    _fetch_event_odds,
    collect_espn_odds,
)
from tools.collect.resolution import (  # noqa: F401
    GAME_LEVEL_MARKETS,
    _closing_from_snapshot,
    fuzzy_team_match,
    resolve_game_level_outcomes,
    resolve_prop_outcomes,
)
from tools.collect.play_by_play import collect_play_by_play  # noqa: F401
from tools.collect.baseball import collect_mlb_players, collect_statcast  # noqa: F401
from tools.collect.hockey import NHL_API, collect_nhl_players, collect_nhl_shots  # noqa: F401
from tools.collect.football import (  # noqa: F401
    NFLFASTR_BASE,
    collect_nfl_combine,
    collect_nfl_plays,
    collect_nfl_players,
)
from tools.collect.basketball import (  # noqa: F401
    NBA_HEADERS,
    NBA_STATS_BASE,
    NCAA_BBALL_LEAGUES,
    collect_nba_players,
    collect_nba_shots,
    collect_ncaa_basketball_game_stats,
    collect_ncaa_basketball_players,
)
from tools.collect.golf import collect_golf_player_rounds  # noqa: F401
from tools.ingestion_tracking import tracked_ingestion

load_dotenv()

logger = logging.getLogger("callisto.data_collector")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


class DataCollector:
    """Collects game data from free sources for the embedding pipeline.

    Facade over :mod:`tools.collect` — each method delegates to the
    corresponding module-level function, passing itself as ``dc``.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        # Counter for silent player-stat insert failures. Pre-fix these
        # logged at INFO (invisible in production) and the drift was
        # undetectable. Now bumped at WARNING + exposed via
        # ``get_collection_stats()`` so drift shows up in /health.
        self._player_stat_insert_failures: int = 0

    async def initialize(self) -> None:
        from tools.schema import open_db
        self._db = await open_db(self.db_path)
        logger.info("Data collector initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── ESPN SCOREBOARD ──

    @tracked_ingestion(
        source=lambda self, sport, date=None, **_: f"espn.scoreboard.{sport}",
        sla_seconds=600,  # scoreboards should refresh every 10 min
    )
    async def collect_scores(self, sport: str, date: Optional[str] = None) -> dict:
        """Collect final scores from ESPN for a given date."""
        return await collect_scores(self, sport, date)

    # ── ESPN BOX SCORES ──

    @tracked_ingestion(
        source=lambda self, sport, date=None, **_: f"espn.boxscore.{sport}",
        sla_seconds=900,  # box scores post within 15 min of completion
    )
    async def collect_box_scores(self, sport: str, date: Optional[str] = None) -> dict:
        """Collect player box scores from ESPN for completed games."""
        return await collect_box_scores(self, sport, date)

    async def _store_player_stats(
        self, sport, event_id, game_date, player_name, team, stat_map, category,
    ) -> int:
        """Store individual player stats. Returns count of entries stored."""
        return await store_player_stats(
            self, sport, event_id, game_date, player_name, team, stat_map, category,
        )

    # ── ESPN ODDS (hidden core API, free, no auth) ──

    ESPN_CORE_BASE = ESPN_CORE_BASE
    ESPN_CORE_LEAGUES = ESPN_CORE_LEAGUES

    @tracked_ingestion(
        source=lambda self, sport, event_ids=None, **_: f"espn.odds.{sport}",
        sla_seconds=900,
    )
    async def collect_espn_odds(self, sport: str, event_ids: list[str] = None) -> list[dict]:
        """Fetch odds data from ESPN's hidden core API."""
        return await collect_espn_odds(self, sport, event_ids)

    async def _get_today_event_ids(self, sport: str) -> list[str]:
        """Pull today's event IDs from the ESPN site scoreboard."""
        return await get_today_event_ids(sport)

    async def _fetch_event_odds(self, client, core_category, core_league, event_id):
        """Fetch odds and win probabilities for a single ESPN event."""
        return await _fetch_event_odds(client, core_category, core_league, event_id)

    # ── BATCH COLLECTION ──

    async def collect_date_range(self, sport: str, start_date: str, end_date: str) -> dict:
        """Collect scores and box scores for a date range (YYYY-MM-DD)."""
        return await collect_date_range(self, sport, start_date, end_date)

    # ── PROP RESOLUTION ──

    async def resolve_prop_outcomes(self, sport: str, game_date: str) -> dict:
        """Resolve paper trades using collected player stats."""
        return await resolve_prop_outcomes(self, sport, game_date)

    # ── GAME-LEVEL RESOLUTION ──

    GAME_LEVEL_MARKETS = GAME_LEVEL_MARKETS

    @staticmethod
    def _fuzzy_team_match(name: str, candidates: list[str], threshold: float = 0.8) -> Optional[str]:
        """Match a team name against candidates with progressively looser strategies."""
        return fuzzy_team_match(name, candidates, threshold)

    async def resolve_game_level_outcomes(self, sport: str, game_date: str) -> dict:
        """Resolve paper trades for game-level markets (spreads, totals, h2h)."""
        return await resolve_game_level_outcomes(self, sport, game_date)

    async def _closing_from_snapshot(self, sport, game_date, event_id, market, side):
        """Extract closing odds from the last odds snapshot containing this game."""
        return await _closing_from_snapshot(self, sport, game_date, event_id, market, side)

    # ── ESPN PLAY-BY-PLAY ──

    @tracked_ingestion(
        source=lambda self, sport, date=None, **_: f"espn.pbp.{sport}",
        sla_seconds=1800,
    )
    async def collect_play_by_play(self, sport: str, date: Optional[str] = None) -> dict:
        """Collect play-by-play and win probability data from ESPN summary endpoint."""
        return await collect_play_by_play(self, sport, date)

    # ── BASEBALL SAVANT / STATCAST ──

    @tracked_ingestion(source="statcast.savant.pitches", sla_seconds=3600)
    async def collect_statcast(
        self, start_date: str, end_date: Optional[str] = None, player_type: str = "pitcher",
    ) -> dict:
        """Collect pitch-level Statcast data from Baseball Savant (free, no key)."""
        return await collect_statcast(self, start_date, end_date, player_type)

    # ── MLB PLAYER METADATA ──

    @tracked_ingestion(source="mlb_stats.players", sla_seconds=86400)
    async def collect_mlb_players(self) -> dict:
        """Refresh the mlb_players table from the free MLB Stats API."""
        return await collect_mlb_players(self)

    # ── NHL ──

    NHL_API = NHL_API

    @tracked_ingestion(source="nhl_api.players", sla_seconds=86400)
    async def collect_nhl_players(self) -> dict:
        """Refresh the nhl_players table from api.nhle.com."""
        return await collect_nhl_players(self)

    @tracked_ingestion(source="nhl_api.shots", sla_seconds=3600)
    async def collect_nhl_shots(self, date: Optional[str] = None) -> dict:
        """Per-shot event ingestion from api-web.nhle.com for games on `date`."""
        return await collect_nhl_shots(self, date)

    # ── NFL ──

    NFLFASTR_BASE = NFLFASTR_BASE

    @tracked_ingestion(source="nflverse.players", sla_seconds=86400)
    async def collect_nfl_players(self, season: Optional[int] = None) -> dict:
        """Refresh nfl_players from nflverse seasonal roster CSV."""
        return await collect_nfl_players(self, season)

    @tracked_ingestion(source="nflverse.combine", sla_seconds=604800)
    async def collect_nfl_combine(self, start_year: int = 2000) -> dict:
        """Refresh nfl_combine_results from nflverse combine CSV."""
        return await collect_nfl_combine(self, start_year)

    @tracked_ingestion(source="nflverse.plays", sla_seconds=3600)
    async def collect_nfl_plays(self, season: Optional[int] = None) -> dict:
        """Stream-ingest nflfastR per-season play_by_play CSV into nfl_play_events."""
        return await collect_nfl_plays(self, season)

    # ── NBA + NCAA BASKETBALL ──

    NBA_STATS_BASE = NBA_STATS_BASE
    NBA_HEADERS = NBA_HEADERS
    NCAA_BBALL_LEAGUES = NCAA_BBALL_LEAGUES

    @tracked_ingestion(source="nba_api.players", sla_seconds=86400)
    async def collect_nba_players(self, season: Optional[str] = None) -> dict:
        """Refresh nba_players from stats.nba.com commonallplayers."""
        return await collect_nba_players(self, season)

    @tracked_ingestion(source="nba_api.shots", sla_seconds=3600)
    async def collect_nba_shots(self, date: Optional[str] = None) -> dict:
        """Per-shot events from stats.nba.com shotchartdetail for games on `date`."""
        return await collect_nba_shots(self, date)

    @tracked_ingestion(
        source=lambda self, sport, **_: f"espn.ncaa_hoops.players.{sport}",
        sla_seconds=86400,
    )
    async def collect_ncaa_basketball_players(self, sport: str) -> dict:
        """Refresh ncaa_basketball_players for a given sport."""
        return await collect_ncaa_basketball_players(self, sport)

    @tracked_ingestion(
        source=lambda self, sport, date=None, **_: f"espn.ncaa_hoops.boxscore.{sport}",
        sla_seconds=900,
    )
    async def collect_ncaa_basketball_game_stats(self, sport: str, date: Optional[str] = None) -> dict:
        """Fetch ESPN boxscores for completed NCAA games and upsert per-player stats."""
        return await collect_ncaa_basketball_game_stats(self, sport, date)

    # ── GOLF ──

    @tracked_ingestion(source="espn.golf.rounds", sla_seconds=3600)
    async def collect_golf_player_rounds(self, season: Optional[int] = None) -> dict:
        """Ingest per-round SG data for the PGA Tour via DataGolf public JSON."""
        return await collect_golf_player_rounds(self, season)

    # ── EMBEDDING PIPELINE ──

    async def get_unembedded_contexts(
        self, sport: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        """Get game contexts that haven't been embedded yet."""
        import json as _json

        if sport:
            cursor = await self._db.execute(
                "SELECT id, sport, event_id, game_date, home_team, away_team, "
                "home_score, away_score, context_json "
                "FROM game_contexts WHERE embedded = FALSE AND sport = ? "
                "ORDER BY game_date DESC LIMIT ?",
                (sport, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, sport, event_id, game_date, home_team, away_team, "
                "home_score, away_score, context_json "
                "FROM game_contexts WHERE embedded = FALSE "
                "ORDER BY game_date DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        contexts = []
        for row in rows:
            ctx = dict(zip(cols, row))
            ctx["context"] = _json.loads(ctx.pop("context_json"))
            contexts.append(ctx)
        return contexts

    async def mark_embedded(self, context_id: int) -> None:
        """Mark a game context as embedded."""
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "UPDATE game_contexts SET embedded = TRUE WHERE id = ?",
            (context_id,),
            operation="data_collector mark_embedded",
        )
        await commit_with_retry(self._db, operation="data_collector mark_embedded")

    async def get_collection_stats(self) -> dict:
        """Return data collection statistics."""
        stats = {}
        for table in ["game_contexts", "player_stats"]:
            cursor = await self._db.execute(
                f"SELECT sport, COUNT(*) as count, "
                f"MIN(game_date) as earliest, MAX(game_date) as latest "
                f"FROM {table} GROUP BY sport"
            )
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            stats[table] = [dict(zip(cols, r)) for r in rows]

        # Embedding pipeline status
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM game_contexts WHERE embedded = FALSE"
        )
        stats["unembedded_contexts"] = (await cursor.fetchone())[0]

        # Silent-drift visibility: player stat insert failures should be 0
        # in steady state (see tools/collect/espn.py store_player_stats).
        stats["player_stat_insert_failures"] = self._player_stat_insert_failures

        return stats
