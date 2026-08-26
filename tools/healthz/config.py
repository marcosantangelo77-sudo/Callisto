"""Configuration constants and data-collector SLA tables for health monitoring."""

import os

from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Health check interval
CHECK_INTERVAL = 120  # 2 minutes

# Circuit breaker thresholds (default / "slow" path)
BREAKER_FAIL_THRESHOLD = 5      # Consecutive failures to trip
BREAKER_COOLDOWN = 600           # 10 min cooldown before retry
MAX_ERRORS_PER_HOUR = 50         # Error rate limit per subsystem

# Fast-path thresholds for infrastructure checks where rapid signal matters.
# A subsystem configured with fast_threshold can trip in ~1 minute rather than
# waiting the full slow window. Applied per-subsystem via SUBSYSTEM_BREAKER_CFG.
FAST_BREAKER_FAIL_THRESHOLD = 3
FAST_BREAKER_MIN_INTERVAL_S = 20  # Require failures within 60s total window

# Network check caching — transient Wi-Fi flakiness shouldn't silence
# higher-layer alerting. Cache ESPN/odds-api results for 5 minutes; only
# escalate from "warning" to failure if the outage persists.
NETWORK_CACHE_TTL_S = 300
NETWORK_ESCALATE_AFTER_S = 600  # 10 minutes of continuous failure → real failure

# Resource thresholds
MIN_DISK_GB = 2.0                # Alert below 2GB free
MAX_DB_SIZE_GB = 5.0             # Alert above 5GB
MAX_MEMORY_MB = 4096             # Alert above 4GB RSS
MEMORY_GROWTH_MB_PER_HOUR = 100  # Leak detection threshold

# Ollama inference timeout — if a model takes longer than this, it's stuck
OLLAMA_HEALTH_TIMEOUT = 15       # seconds for a simple health-check prompt

# Subsystem names
SUBSYSTEMS = [
    "ollama", "sqlite", "disk", "memory", "network",
    "research_loop", "embedding", "data_collector",
]

# ── Data collector SLA configuration ──
# Per-source maximum age (seconds) between successful ingestion runs before
# the source is considered stale. Two tiers:
#   * WARN tier  — logged; surfaced in /health but does not trip the breaker
#   * CRITICAL tier = 3x warn — trips the circuit breaker
#
# Source tags are hierarchical (`<api>.<resource>.<sport>`) — see
# tools/ingestion_tracking.py. When a source name isn't listed explicitly
# we fall back to SOURCE_SLA_DEFAULTS matched by prefix.
#
# These SLAs are tuned to the observed cadence of the callers (line_monitor
# polls scoreboards ~every 5 min; box scores hit ~15 min post-game). Tighten
# them after observing real throughput; loosening is safer than tightening.
SOURCE_SLAS: dict[str, int] = {
    # Scoreboards — frequent polling expected
    "odds_api_io.v3.odds.updated": 600,          # WebSocket-adjacent, 10 min
    "odds_api_io.v3.live_events.all": 900,
    "odds_api.v4.odds.basketball_nba": 900,
    # Game-day ESPN scrapes
    "espn.scoreboard.baseball_mlb": 900,         # 15 min — active day
    "espn.scoreboard.basketball_nba": 900,
    "espn.scoreboard.icehockey_nhl": 900,
    "espn.scoreboard.americanfootball_nfl": 900,
    # Box scores — post-game, more lenient
    "espn.boxscore.baseball_mlb": 1800,
    "espn.boxscore.basketball_nba": 1800,
    # Roster / injuries — daily cadence
    "espn.injuries.baseball_mlb": 21600,         # 6 hr
    "espn.injuries.basketball_nba": 21600,
    "mlb_stats.players": 172800,                 # 2 days
    "nhl_api.players": 172800,
    "nflverse.players": 172800,
    "nba_api.players": 172800,
    # Historical / backfill
    "nflverse.combine": 2592000,                 # 30 days
    # Calendar refresh
    "game_scheduler.refresh_calendar": 7200,     # 2 hr
}

# Prefix-based fallback for sources not explicitly listed.
SOURCE_SLA_DEFAULTS: list[tuple[str, int]] = [
    ("odds_api_io.", 900),
    ("odds_api.",    900),
    ("espn.scoreboard.", 1800),
    ("espn.boxscore.", 3600),
    ("espn.pbp.", 3600),
    ("espn.roster.", 21600),
    ("espn.injuries.", 21600),
    ("espn.odds.", 1800),
    ("espn.ncaa_hoops.", 3600),
    ("espn.golf.", 7200),
    ("nhl_api.", 3600),
    ("nba_api.", 3600),
    ("nflverse.", 3600),
    ("mlb_stats.", 86400),
    ("statcast.", 21600),
    ("openmeteo.", 3600),
    ("game_scheduler.", 7200),
]

# Critical-tier multiplier: source is CRITICAL (breaker trips) if
# last_success older than SLA * CRITICAL_MULTIPLIER.
CRITICAL_MULTIPLIER = 3


def resolve_sla_seconds(source: str) -> int:
    """Return the SLA (max age since last successful run) for a source tag."""
    if source in SOURCE_SLAS:
        return SOURCE_SLAS[source]
    for prefix, sla in SOURCE_SLA_DEFAULTS:
        if source.startswith(prefix):
            return sla
    # Unknown source — generous default so we don't false-alarm
    return 7200


def db_path() -> str:
    """Current SQLite path.

    Re-read from the environment at call time so callers that set
    ``CALLISTO_DB_PATH`` after :data:`DB_PATH` was first bound (test
    fixtures, embedded hosts) observe the new value without needing to
    invalidate module caches.
    """
    return os.getenv("CALLISTO_DB_PATH", DB_PATH)


# Per-subsystem breaker configuration. "fast" = fast-path thresholds so
# infrastructure subsystems trip quickly (~1 min) when failures cluster.
# Subsystems not listed use the slow/default thresholds.
SUBSYSTEM_BREAKER_CFG: dict[str, dict] = {
    "sqlite": {"fast": True},
    "ollama": {"fast": True},
    "disk":   {"fast": True},
    # network/memory keep slow thresholds — they're noisy by nature.
}
