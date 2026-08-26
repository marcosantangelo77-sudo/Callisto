"""
Configuration constants for the line monitor — slice-6 extraction.

Moved verbatim from tools/line_monitor.py. The facade re-imports every
name so `tools.line_monitor.SNAPSHOT_INTERVAL` etc. keep working.

Env knobs:
- CALLISTO_DB_PATH              — sqlite path
- ODDS_SNAPSHOT_INTERVAL        — seconds between poll cycles
- ODDS_MONITORED_SPORTS         — comma-separated sport slugs
- CALLISTO_WS_SPORTS            — odds-api.io WS sport slugs
- CALLISTO_WS_ENABLED           — 0 disables the WebSocket path
- CALLISTO_INCREMENTAL_ENABLED  — 0 disables /odds/updated polling
- CALLISTO_INCREMENTAL_INTERVAL_S — poll cadence for the gap-filler
- CALLISTO_REQUIRE_MODEL_AGREEMENT — gate ev_opportunities on model confirmation
"""

import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Snapshot interval in seconds — balance freshness vs credit burn
# 500 credits/month ≈ 16/day. Each snapshot = markets × regions credits.
# Default: 15 min intervals, 3 markets, 1 region = 3 credits/snap = ~5 snaps/day budget
SNAPSHOT_INTERVAL = int(os.getenv("ODDS_SNAPSHOT_INTERVAL", "900"))

# Sports to monitor — configurable via env, comma-separated
MONITORED_SPORTS = os.getenv(
    "ODDS_MONITORED_SPORTS",
    "basketball_nba,icehockey_nhl,americanfootball_nfl,baseball_mlb,basketball_ncaab,basketball_ncaaw,soccer_mls,golf_pga",
).split(",")

# --- Event-driven odds update config ----------------------------------------
# These knobs flip Callisto from "poll every 15 min" to event-driven freshness:
#   * WS_SPORTS — odds-api.io sport slugs to stream live (comma-separated).
#     Maps many-to-one onto MONITORED_SPORTS via WS_SPORT_TO_MONITORED.
#   * WS_ENABLED — flip to 0 to disable WS entirely (fall back to 15-min poll).
#   * INCREMENTAL_ENABLED — /odds/updated?since=X polling every 60s as a
#     gap-filler between WS drops.
#   * REQUIRE_MODEL_AGREEMENT — gate ev_opportunities on independent model
#     confirmation. Default on; set to 0 to revert to steam-only emission.
WS_SPORTS = os.getenv(
    "CALLISTO_WS_SPORTS", "basketball,american-football,baseball,ice-hockey"
)
WS_ENABLED = os.getenv("CALLISTO_WS_ENABLED", "1") == "1"
INCREMENTAL_ENABLED = os.getenv("CALLISTO_INCREMENTAL_ENABLED", "1") == "1"
INCREMENTAL_INTERVAL = int(os.getenv("CALLISTO_INCREMENTAL_INTERVAL_S", "60"))
REQUIRE_MODEL_AGREEMENT = os.getenv("CALLISTO_REQUIRE_MODEL_AGREEMENT", "1") == "1"
