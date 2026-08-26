"""Action Network scraper package.

Split out of the original monolithic tools/action_network_scraper.py:

- constants:   API config, book/league mappings
- team_names:  short-name -> full-name mapping and resolution
- http:        rate-limited HTTP client (curl_cffi with httpx fallback)
- parser:      scoreboard response parsing (games, odds, public betting)
- scraper:     high-level async entry points

The legacy ``tools.action_network_scraper`` module is kept as a thin
back-compat facade re-exporting this package's public API.
"""

from tools.actionnet.constants import (
    _API_BASE,
    _BOOK_IDS,
    BOOK_ID_MAP,
    LEAGUE_MAP,
    SPORT_TITLES,
)
from tools.actionnet.http import close_client, rate_limited_get
from tools.actionnet.parser import build_url, extract_public_betting, parse_game
from tools.actionnet.scraper import get_public_betting, scrape_action_network
from tools.actionnet.team_names import TEAM_NAME_MAP, _resolve_team_name

__all__ = [
    "BOOK_ID_MAP",
    "LEAGUE_MAP",
    "SPORT_TITLES",
    "TEAM_NAME_MAP",
    "build_url",
    "close_client",
    "extract_public_betting",
    "get_public_betting",
    "parse_game",
    "rate_limited_get",
    "scrape_action_network",
]
