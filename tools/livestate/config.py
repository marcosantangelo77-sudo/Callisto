"""Shared constants and module-level knobs for the live-state package.

Extracted from the original ``tools/live_state.py`` monolith. All
values are byte-identical to what the monolith used so behavior is
unchanged.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

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
