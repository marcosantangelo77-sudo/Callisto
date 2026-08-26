"""Free player-prop scrapers (DraftKings, FanDuel, BetMGM).

Split out of tools/prop_scraper_free.py, which remains the stable facade.
"""

from tools.propscrape.betmgm import (
    classify_mgm_prop,
    close_mgm_session,
    mgm_decimal_to_american,
    mgm_parse_odds,
    scrape_mgm_props,
)
from tools.propscrape.common import (
    close_shared_sessions,
    parse_nash_american_odds,
)
from tools.propscrape.draftkings import classify_dk_nash_prop, scrape_dk_props
from tools.propscrape.fanduel import (
    classify_fd_prop,
    close_fd_client,
    scrape_fd_props,
)

__all__ = [
    # DraftKings
    "classify_dk_nash_prop",
    "scrape_dk_props",
    # FanDuel
    "classify_fd_prop",
    "scrape_fd_props",
    "close_fd_client",
    # BetMGM
    "classify_mgm_prop",
    "mgm_decimal_to_american",
    "mgm_parse_odds",
    "scrape_mgm_props",
    "close_mgm_session",
    # Shared
    "parse_nash_american_odds",
    "close_shared_sessions",
]
