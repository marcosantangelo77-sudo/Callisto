"""
DraftKings live odds scraper — free, unlimited pregame odds.

Uses the DraftKings Nash sportsbook content API which returns full
event/market/selection data without Akamai bot blocking. Falls back
to the legacy v5 eventgroups endpoint if curl_cffi is unavailable.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.

Implementation has been split into the ``tools.dkscrape`` package; this
module remains as a facade re-exporting the public API so existing
imports keep working.
"""

from tools.dkscrape import *  # noqa: F401,F403
from tools.dkscrape import (  # noqa: F401  (explicit re-export)
    DK_ENDPOINTS,
    DK_GOLF_CATEGORY_SLUGS,
    DK_GOLF_DISPLAY_GROUP,
    DK_GOLF_EVENTGROUPS,
    DK_PROP_CATEGORIES,
    DK_PROP_NAME_PATTERNS,
    LEAGUE_IDS,
    close_client,
    discover_golf_categories,
    discover_prop_categories,
    list_dk_golf_tournaments,
    scrape_dk_golf_odds,
    scrape_dk_odds,
    scrape_dk_props,
    _DK_ABBREV_TO_CITY,
    _expand_dk_short_name,
    _HAS_CURL_CFFI,
    _NASH_MARKET_TYPE_MAP,
    _build_event_map,
    _cffi_get_sync,
    _classify_market,
    _dk_american_odds,
    _effective_prop_categories,
    _expand_dk_short_name,
    _extract_events,
    _extract_offers,
    _get_client,
    _get_cffi_session,
    _nash_get,
    _golf_category_cache,
    _normalize_nash_response,
    _parse_nash_american_odds,
    _parse_outcomes,
    _prop_category_cache,
    _sport_title,
)
