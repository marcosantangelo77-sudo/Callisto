"""
Bookmaker key canonicalizer.

Odds flow into Callisto from five sources (odds-api.io, the-odds-api.com,
DK/FD/BetMGM scrapers, Action Network) and each one spells bookmaker names
slightly differently. odds-api.io emits "betfair_exchange", the-odds-api.com
emits "betfair exchange", scrapers capitalize display titles ("Betfair
Exchange"). When downstream code does membership tests against literal sets
like _RELIABLE_CLOSE_SOURCES = {"betfair_exchange", ...}, a single casing
mismatch silently drops rows — close_reliable=False everywhere, CLV ledger
looks empty even when the data is fine.

This module is the ONE place where we canonicalize. Every read site that
compares book keys to a literal set MUST go through `canonicalize_book`.
Every write site that stores a book identifier SHOULD go through it too,
so the database stays in a single casing convention.
"""

from __future__ import annotations

import re

# Regex: one or more whitespace/dash/period characters → single underscore
_NON_ID_RUN = re.compile(r"[\s\-.]+")

# Explicit aliases for books whose naive canonicalization doesn't match the
# form odds-api.io/the-odds-api emit. Extend sparingly; prefer matching the
# provider's underscore-lowercase form so lookups stay O(1).
_ALIASES: dict[str, str] = {
    "draft_kings": "draftkings",
    "fan_duel": "fanduel",
    "bet_mgm": "betmgm",
    "lowvig": "lowvig.ag",  # the-odds-api uses lowvig.ag as the key
    "low_vig_ag": "lowvig.ag",
    "lowvig_ag": "lowvig.ag",
    "mybookie": "mybookie.ag",
    "mybookie_ag": "mybookie.ag",
    "fanatics_sportsbook": "fanatics",
    "sharp_exchange": "sharp",
}


def canonicalize_book(key: object) -> str:
    """Normalize a book name/key to the canonical odds-api.io-style slug.

    Rules:
      1. Lowercase.
      2. Strip leading/trailing whitespace.
      3. Collapse runs of whitespace/hyphens/periods (except for known
         period-bearing keys like 'lowvig.ag', 'mybookie.ag') into a
         single underscore. We preserve '.ag' suffixes because that's how
         the-odds-api emits exchange keys.
      4. Map through _ALIASES so "draft_kings" and "draftkings" collapse.

    Returns the canonical key, or an empty string for None/empty inputs.
    """
    if key is None:
        return ""
    s = str(key).strip().lower()
    if not s:
        return ""

    # Preserve '.ag' suffix — strip it, normalize the prefix, then put it back.
    suffix = ""
    if s.endswith(".ag"):
        suffix = ".ag"
        s = s[:-3]

    s = _NON_ID_RUN.sub("_", s).strip("_")
    if suffix:
        s = f"{s}{suffix}"

    return _ALIASES.get(s, s)


def canonicalize_book_set(keys) -> frozenset[str]:
    """Canonicalize every member of an iterable into a frozenset."""
    return frozenset(canonicalize_book(k) for k in keys if k)


# ---------------------------------------------------------------------------
# Book-specific max-stake caps (USD).
#
# Used by the arbitrage scanner to filter "paper arbs" that require posting
# more on a leg than the book will actually accept for that market. These are
# conservative typical-user limits, NOT sharp/premium account limits. If you
# have a rated account or have been flagged, your real caps may be lower.
#
# Format: dict keyed by canonicalized book slug. Each entry maps a broad
# market family to an approximate per-ticket cap; callers fall back to the
# 'default' entry when the specific market_type isn't listed.
#
# Sources / reasoning:
#   - DraftKings / FanDuel / BetMGM: public reports and DK's published limit
#     matrix, in-play/props are dramatically lower than sides/totals.
#   - Pinnacle: sharpest of the listed books, sides limits are much higher;
#     props much lower.
#   - Bovada / MyBookie: US-facing offshore, modest limits.
#   - Fanatics: relatively new, conservative limits on anything non-featured.
#
# NOTE: These are heuristics. Real caps change per-event, per-account, and
# per-time-window. When scanner output says "arb limited to $250 at Fanatics",
# treat that as a ceiling, not a guarantee.
# ---------------------------------------------------------------------------
BOOK_MAX_STAKE: dict[str, dict[str, float]] = {
    "pinnacle": {
        "h2h": 25000.0,
        "spreads": 25000.0,
        "totals": 25000.0,
        "props": 2500.0,
        "default": 10000.0,
    },
    "draftkings": {
        "h2h": 5000.0,
        "spreads": 5000.0,
        "totals": 5000.0,
        "props": 500.0,
        "default": 2500.0,
    },
    "fanduel": {
        "h2h": 5000.0,
        "spreads": 5000.0,
        "totals": 5000.0,
        "props": 500.0,
        "default": 2500.0,
    },
    "betmgm": {
        "h2h": 2500.0,
        "spreads": 2500.0,
        "totals": 2500.0,
        "props": 250.0,
        "default": 1500.0,
    },
    "caesars": {
        "h2h": 2500.0,
        "spreads": 2500.0,
        "totals": 2500.0,
        "props": 250.0,
        "default": 1500.0,
    },
    "betrivers": {
        "h2h": 1500.0,
        "spreads": 1500.0,
        "totals": 1500.0,
        "props": 250.0,
        "default": 1000.0,
    },
    "fanatics": {
        "h2h": 1000.0,
        "spreads": 1000.0,
        "totals": 1000.0,
        "props": 250.0,
        "default": 500.0,
    },
    "bovada": {
        "h2h": 2000.0,
        "spreads": 2000.0,
        "totals": 2000.0,
        "props": 500.0,
        "default": 1000.0,
    },
    "mybookie.ag": {
        "h2h": 1000.0,
        "spreads": 1000.0,
        "totals": 1000.0,
        "props": 250.0,
        "default": 500.0,
    },
    "lowvig.ag": {
        "h2h": 2000.0,
        "spreads": 2000.0,
        "totals": 2000.0,
        "props": 500.0,
        "default": 1000.0,
    },
}

# Fallback cap for unknown books; deliberately conservative.
_UNKNOWN_BOOK_CAP_DEFAULT = 500.0


def get_book_max_stake(book: str, market_type: str = "default") -> float:
    """Return the approximate max per-ticket stake for a given book/market.

    Unknown books fall back to a conservative $500. The market_type can be
    "h2h"/"spreads"/"totals"/"props"/"default" — broader market families map
    through "default" when the specific key isn't present.
    """
    key = canonicalize_book(book)
    caps = BOOK_MAX_STAKE.get(key)
    if caps is None:
        return _UNKNOWN_BOOK_CAP_DEFAULT
    mt = (market_type or "default").lower()
    # Map prop-shaped market_types onto the "props" bucket.
    if mt not in caps and ("player_" in mt or mt.endswith("_prop")):
        mt = "props"
    return float(caps.get(mt, caps.get("default", _UNKNOWN_BOOK_CAP_DEFAULT)))
