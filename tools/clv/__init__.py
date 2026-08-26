"""Closing Line Value (CLV) tracking package.

If you consistently beat the closing line, you're +EV even during losing streaks.
CLV is the only reliable predictor of long-term profitability.

How it works:
1. Record the line when a bet is placed
2. Record the closing line (Pinnacle/sharp book) at game start
3. CLV = placement line - closing line (positive = you got a better number)

Sustained positive CLV = edge exists, regardless of short-term results.
This is how sharps measure themselves.

Module layout:
- ``constants``   — DB path, per-book vig estimates, reliable-close sources
- ``odds_math``   — pure helpers (devig, odds conversion, regime stamp)
- ``clv_log``     — writers for the append-only ``clv_log`` table
- ``reporting``   — reports, bankroll history, forecasts, bet queries
- ``tracker``     — the ``CLVTracker`` class itself
"""

from tools.clv.constants import (
    BOOK_VIG_ESTIMATE,
    DB_PATH,
    RELIABLE_CLOSE_SOURCES,
)
from tools.clv.clv_log import CLVLogMixin
from tools.clv.odds_math import (
    american_to_decimal,
    half_vig_devig,
    interpret_clv,
    regime_stamp,
)
from tools.clv.reporting import CLVReportingMixin
from tools.clv.tracker import CLVTracker

__all__ = [
    "BOOK_VIG_ESTIMATE",
    "DB_PATH",
    "RELIABLE_CLOSE_SOURCES",
    "CLVLogMixin",
    "CLVReportingMixin",
    "CLVTracker",
    "american_to_decimal",
    "half_vig_devig",
    "interpret_clv",
    "regime_stamp",
]
