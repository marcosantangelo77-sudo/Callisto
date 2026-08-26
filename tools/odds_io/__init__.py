"""tools.odds_io — HTTP / parse / persist helpers extracted from
tools/odds_api_io.py.

The public surface remains importable from tools.odds_api_io (facade);
import from here for the internal building blocks:

  - config:     constants (keys, sport map, bookmaker slugs), client mgmt
  - usage:      persisted hourly request budget tracking
  - http_client: authenticated GET with 429 exponential backoff
  - normalize:  odds-api.io -> Callisto standard format conversion
  - persist:    pre-commence snapshot assembly + best-line comparison
"""

from tools.odds_io.config import (  # noqa: F401
    BOOKMAKER_SLUG_MAP,
    HOURLY_LIMIT,
    ODDS_API_IO_BASE,
    ODDS_API_IO_KEY,
    SELECTED_BOOKMAKERS,
    SPORT_MAP,
    SPORT_TITLES,
    close_client,
    get_client,
    resolve_sport,
)
from tools.odds_io.usage import (  # noqa: F401
    check_budget,
    get_usage_status,
    hourly_remaining,
    increment_usage,
    load_usage,
    save_usage,
)
from tools.odds_io.http_client import (  # noqa: F401
    BACKOFF_MAX_RETRIES,
    api_get,
    compute_backoff,
)
from tools.odds_io.normalize import (  # noqa: F401
    decimal_to_american,
    extract_movement_snapshots,
    find_best_line,
    normalize_event_odds,
    parse_iso,
    pick_pre_commence_entry,
    pick_primary_spread,
    pick_primary_total,
    safe_float,
    snapshot_to_market_outcomes,
)

__all__ = [
    "BOOKMAKER_SLUG_MAP", "HOURLY_LIMIT", "ODDS_API_IO_BASE", "ODDS_API_IO_KEY",
    "SELECTED_BOOKMAKERS", "SPORT_MAP", "SPORT_TITLES",
    "close_client", "get_client", "resolve_sport",
    "check_budget", "get_usage_status", "hourly_remaining", "increment_usage",
    "load_usage", "save_usage",
    "BACKOFF_MAX_RETRIES", "api_get", "compute_backoff",
    "decimal_to_american", "extract_movement_snapshots", "find_best_line",
    "normalize_event_odds", "parse_iso", "pick_pre_commence_entry",
    "pick_primary_spread", "pick_primary_total", "safe_float",
    "snapshot_to_market_outcomes",
]
