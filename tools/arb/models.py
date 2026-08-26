"""Shared constants and data classes for the arbitrage scanner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — conservative defaults.
#
# EPSILON: the total-implied-prob cutoff. A pure arb has total < 1.0, but we
# demand total < 1.0 - EPSILON so we have room for (a) rounding in decimal
# odds, (b) partial-fill on the slower leg, and (c) the 1-3bp drift during
# the ~5s round-trip to place both tickets.
#
# STALE_SECONDS: maximum age of any leg's fetched_at. 120s is generous for
# ws-fed lines (typically <10s) and tight enough that a 15-min poll's final
# quote is still inside the window right after a refresh.
# ---------------------------------------------------------------------------
DEFAULT_EPSILON = 0.002
DEFAULT_STALE_SECONDS = 120.0
DEFAULT_BUDGET = 1000.0
MIN_EFFECTIVE_BUDGET_PCT = 0.5  # below this fraction the arb is "too small"

# A small positive profit floor below which we don't bother recording an arb.
# Rounding errors can produce total_implied = 0.9999 which yields 0.01% profit;
# not worth surfacing as actionable.
MIN_PROFIT_PCT = 0.005  # 0.5% minimum expected profit

# SANITY CEILING — above this profit_pct the "arb" is almost certainly a
# data quality bug (team-name mixup, stale feed, wrong side classification).
# Real arbs above 5% are extraordinarily rare; above 10% are mythical.
# Rows above this ceiling are dropped with a warning.
MAX_PROFIT_PCT = 0.10

# Price-range sanity: if one leg is quoted at +1000 or worse (massive dog)
# alongside another at -200 or better (heavy favorite at a different book)
# for the SAME binary market, the two quotes disagree about reality so hard
# that at least one is wrong. We reject pairs where abs(implied_A - implied_B)
# exceeds this delta on a binary market — a real 2% arb has legs that agree
# to within 5-10% implied; anything beyond this is a data mismatch.
MAX_IMPLIED_DIVERGENCE = 0.20


# ---------------------------------------------------------------------------
# Data classes — what the scanner emits.
# ---------------------------------------------------------------------------
@dataclass
class ArbLeg:
    """One leg of an arbitrage opportunity."""
    bookmaker: str
    bookmaker_canonical: str
    outcome: str
    american_odds: int
    decimal_odds: float
    implied_prob: float
    point: Optional[float] = None
    stake: float = 0.0          # filled in once budget is applied
    stake_capped_by_book: bool = False
    fetched_at: Optional[str] = None
    age_seconds: Optional[float] = None


@dataclass
class ArbOpportunity:
    """A complete arb/dutch-book/synthetic-arb opportunity."""
    game_id: str
    game: str                   # "away @ home" display
    sport: str
    market_type: str            # 'h2h', 'spreads', 'totals', or synthetic label
    thesis_tag: str             # 'arb' | 'dutch' | 'synthetic_arb'
    total_implied: float
    profit_pct: float           # expected profit per dollar of effective budget
    expected_profit: float      # USD at the effective budget
    budget_requested: float
    effective_budget: float     # budget reduced by book limits
    legs: list[ArbLeg] = field(default_factory=list)
    limited_by_book_caps: bool = False
    max_leg_age_s: float = 0.0
    detected_at: str = ""
    expires_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d
