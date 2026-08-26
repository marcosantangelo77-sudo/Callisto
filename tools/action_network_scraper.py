"""
Action Network public API scraper — free, no API key required.

Scrapes odds from Action Network's public scoreboard API, which returns
odds from up to 9 bookmakers: DraftKings, FanDuel, Caesars, BetMGM,
BetRivers, PointsBet, Bet365, Hard Rock Bet, and ESPNBet.

Also provides public betting percentages (ml_home_public / ml_away_public),
which is unique data no other free source exposes.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.

Back-compat facade: the implementation lives in tools/actionnet/.
"""

# Re-export the full public API from the split package so existing
# importers (tools/line_monitor.py, tools/lines/fallback_cascade.py)
# continue to work unchanged.
from tools.actionnet import *  # noqa: F401,F403
from tools.actionnet import __all__  # noqa: F401

# Legacy private aliases kept for any code that reached into internals.
from tools.actionnet import parser as _parser_mod  # noqa: F401
from tools.actionnet.constants import _API_BASE as _API_BASE  # noqa: F401
from tools.actionnet.constants import _BOOK_IDS as _BOOK_IDS  # noqa: F401
from tools.actionnet.constants import (
    SPORT_TITLES as _SPORT_TITLES,  # noqa: F401
)
from tools.actionnet.http import (
    _HAS_CURL_CFFI as _HAS_CURL_CFFI,  # noqa: F401
    _HEADERS as _HEADERS,  # noqa: F401
    RATE_LIMIT_SECONDS as _RATE_LIMIT_SECONDS,  # noqa: F401
    _get_client as _get_client,  # noqa: F401
    _get_cffi_session as _get_cffi_session,  # noqa: F401
)
from tools.actionnet.http import _cffi_get_sync as _cffi_get_sync  # noqa: F401
from tools.actionnet.parser import (
    extract_public_betting as _extract_public_betting,  # noqa: F401
    parse_game as _parse_game,  # noqa: F401
    build_url as _build_url,  # noqa: F401
)
from tools.actionnet.team_names import (
    _SPORT_SPECIFIC_NAMES as _SPORT_SPECIFIC_NAMES,  # noqa: F401
)
from tools.actionnet.team_names import _resolve_team_name as _resolve_team_name  # noqa: F401
