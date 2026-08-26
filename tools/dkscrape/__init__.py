"""DraftKings pregame odds scraper package.

Split from the former monolithic ``tools/dk_scraper.py`` (~1250 lines).
This package is a drop-in replacement: all public names are re-exported here,
and ``tools/dk_scraper`` remains as a facade for backwards compatibility.

Scrape/parse only — this module never arms live betting.
"""

from tools.dkscrape.constants import (
    DK_ENDPOINTS,
    DK_GOLF_CATEGORY_SLUGS,
    DK_GOLF_DISPLAY_GROUP,
    DK_GOLF_EVENTGROUPS,
    DK_PROP_CATEGORIES,
    DK_PROP_NAME_PATTERNS,
    LEAGUE_IDS,
    _DK_ABBREV_TO_CITY,
    _expand_dk_short_name,
)
from tools.dkscrape.client import (
    _HAS_CURL_CFFI,
    close_client,
    _cffi_get_sync,
    _dk_american_odds,
    _get_cffi_session,
    _get_client,
    _nash_get,
    _parse_nash_american_odds,
    _rate_limited_get,
)
from tools.dkscrape.normalize import _NASH_MARKET_TYPE_MAP, _normalize_nash_response
from tools.dkscrape.legacy import (
    _build_event_map,
    _classify_market,
    _extract_events,
    _extract_offers,
    _parse_outcomes,
)
from tools.dkscrape.scrape import (
    _sport_title,
    scrape_dk_odds,
    scrape_dk_props,
)
from tools.dkscrape.discover import (
    _effective_prop_categories,
    _prop_category_cache,
    discover_prop_categories,
)
from tools.dkscrape.golf import (
    _golf_category_cache,
    discover_golf_categories,
    list_dk_golf_tournaments,
    scrape_dk_golf_odds,
)

__all__ = [
    "DK_ENDPOINTS",
    "DK_GOLF_CATEGORY_SLUGS",
    "DK_GOLF_DISPLAY_GROUP",
    "DK_GOLF_EVENTGROUPS",
    "DK_PROP_CATEGORIES",
    "DK_PROP_NAME_PATTERNS",
    "LEAGUE_IDS",
    "_DK_ABBREV_TO_CITY",
    "_expand_dk_short_name",
    "_HAS_CURL_CFFI",
    "close_client",
    "_cffi_get_sync",
    "_dk_american_odds",
    "_get_cffi_session",
    "_get_client",
    "_nash_get",
    "_parse_nash_american_odds",
    "_rate_limited_get",
    "_NASH_MARKET_TYPE_MAP",
    "_normalize_nash_response",
    "_build_event_map",
    "_classify_market",
    "_extract_events",
    "_extract_offers",
    "_parse_outcomes",
    "_sport_title",
    "scrape_dk_odds",
    "scrape_dk_props",
    "_effective_prop_categories",
    "_prop_category_cache",
    "_golf_category_cache",
    "discover_prop_categories",
    "discover_golf_categories",
    "list_dk_golf_tournaments",
    "scrape_dk_golf_odds",
]
