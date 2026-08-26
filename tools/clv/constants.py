"""Shared constants for the CLV tracking package."""

import os

from dotenv import load_dotenv

from tools.book_keys import canonicalize_book_set

load_dotenv()

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

logger_name = "callisto.clv_tracker"

# Raw implied-prob numbers from two books carry DIFFERENT vig loads, so you
# cannot subtract them directly — a 1% gap may be entirely vig-difference,
# not signal. These per-book half-vig estimates let the devig routine produce
# a fair-probability estimate from a single leg. Values are rough field
# averages on two-way MLB/NBA markets; tuned for bias-reduction, not precision.
BOOK_VIG_ESTIMATE: dict[str, float] = {
    "pinnacle": 0.025,
    "lowvig": 0.02,
    "circa": 0.03,
    "betfair_exchange": 0.02,
    "draftkings": 0.05,
    "fanduel": 0.05,
    "betmgm": 0.06,
    "caesars": 0.06,
    "fanatics": 0.05,
}

# Sources whose closing number we trust as the "real" market close. Anything
# else still gets logged, but close_reliable=False so analysis queries can
# filter it out. Stored as canonical keys; callers MUST canonicalize the
# incoming `closing_source` before membership-testing here — otherwise
# "Pinnacle", "pinnacle ", and "pinnacle" are three different values and
# close_reliable will be wrong for two of them.
RELIABLE_CLOSE_SOURCES: frozenset[str] = canonicalize_book_set(
    {"pinnacle", "lowvig.ag", "circa", "betfair_exchange", "Betfair Exchange"}
)

# Default vig assumptions when a book isn't in BOOK_VIG_ESTIMATE.
DEFAULT_CLOSING_VIG = 0.025
DEFAULT_PLACEMENT_VIG = 0.05
