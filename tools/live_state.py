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

Facade note (post-split)
------------------------
Since the split into ``tools/livestate/``, this module is a thin
facade: it owns ALL shared mutable state (semaphore cache, HTTP client
handle, per-sport backoff ladders, schema-ok flag, observability
counters, collector handle) and re-exports every public name from the
implementation submodules. Submodules read/write this module's
attributes at call time, so monkeypatching or resetting attributes on
``tools.live_state`` keeps working exactly as before the split.
"""

from __future__ import annotations

# Re-export the public API from the implementation package.
from tools.livestate.collector import (
    LiveStateCollector,
    get_collector_counters_24h,
    get_collector_status,
    poll_sport,
    set_collector_for_tests,
    start_collector,
    stop_collector,
)
from tools.livestate.detectors import (
    evaluate_detectors_for_event,
    _extract_live_over,
    _extract_total_point,
    _lookup_mlb_totals,
)
from tools.livestate.espn import (
    _RateLimited,
    _apply_backoff,
    _clear_backoff,
    _fetch_event_summary,
    _get_client,
    _get_semaphore,
    _is_active,
    _is_backed_off,
    _list_active_events,
    close_client,
    reset_semaphore,
)
from tools.livestate.storage import (
    _check_schema,
    _enforce_hard_cap,
    _prune_for_event,
    _record_edge_emission,
    recent_states,
    store_state,
)

# ──────────────────────────────────────────────────────────────────────
# Constants + shared mutable state — THE single home. Submodules reach
# these through this module at call time so resets/monkeypatches land.
# ──────────────────────────────────────────────────────────────────────
import asyncio
import os

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
_espn_semaphore: asyncio.Semaphore | None = None

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
_schema_ok: bool | None = None

# HTTP client handle owned by livestate.espn but stored here so tests /
# shutdown can reset it in one place.
_client = None

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
