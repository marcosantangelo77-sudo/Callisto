"""Shared constants and mutable state for the self-repair package."""

import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

STALE_ODDS_MINUTES = 30
EMPTY_BACKTEST_LOOKBACK = 10
REJECTION_RATE_THRESHOLD = 0.95
SIGNAL_DROUGHT_EVENTS = 500
DB_BLOAT_ROWS = 100_000
SCRAPER_DISABLE_SECONDS = 3600

SCRAPERS = {
    "dk":     ("tools.dk_scraper",        "scrape_dk_odds",     "basketball_nba"),
    "fd":     ("tools.fanduel_scraper",   "scrape_fd_odds",     "basketball_nba"),
    # "betmgm": disabled — redundant with odds-api.io Pro, consistently 403
}
BETMGM_ALT_SUBDOMAINS = ["co", "pa", "va", "az"]
_disabled_scrapers: dict[str, float] = {}  # name -> re-enable monotonic ts

# Safe-to-prune tables: table -> (date_column, keep_days)
PRUNE_SAFE = {
    "backtest_events": ("created_at", 90),
    "odds_snapshots": ("timestamp", 7),            # was 30 days — 2,880 snapshots × 100KB = 288MB bloat
    "odds_snapshots_v2": ("snapshot_time", 7),
    "integrity_checks": ("created_at", 14),
    "hermes_messages": ("timestamp", 90),
    "prop_snapshots": ("snapshot_time", 2),        # 360K rows/day at 15-min intervals — keep 2 days
    "deferred_work_queue": ("created_at", 3),      # was 7 days — 504 pending items cause WAL bloat
    "event_log": ("created_at", 7),                # was unbounded — 16K+ rows growing indefinitely
}

HEARTBEAT_INTERVAL = 300  # Check every 5 minutes
LOOP_STALL_THRESHOLD = 2400  # 40 min — cycles have 18 phases with up to 600s timeouts each
