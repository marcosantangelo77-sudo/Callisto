"""
Closing Line Value (CLV) tracker — the single most important metric.

If you consistently beat the closing line, you're +EV even during losing streaks.
CLV is the only reliable predictor of long-term profitability.

How it works:
1. Record the line when a bet is placed
2. Record the closing line (Pinnacle/sharp book) at game start
3. CLV = placement line - closing line (positive = you got a better number)

Sustained positive CLV = edge exists, regardless of short-term results.
This is how sharps measure themselves.

This module is now a thin facade over ``tools.clv`` — the implementation
lives there. All public names are re-exported so existing
``from tools.clv_tracker import ...`` call sites keep working.
"""

from tools.book_keys import canonicalize_book_set

from tools.clv import (  # noqa: F401
    BOOK_VIG_ESTIMATE as _BOOK_VIG_ESTIMATE,
    DB_PATH,
    RELIABLE_CLOSE_SOURCES as _RELIABLE_CLOSE_SOURCES,
    CLVTracker,
    american_to_decimal as _american_to_decimal,
    half_vig_devig as _half_vig_devig,
    interpret_clv as _interpret_clv,
    regime_stamp as _regime_stamp,
)

__all__ = [
    "DB_PATH",
    "CLVTracker",
    "_BOOK_VIG_ESTIMATE",
    "_RELIABLE_CLOSE_SOURCES",
    "_half_vig_devig",
    "_regime_stamp",
    "_american_to_decimal",
    "_interpret_clv",
]
