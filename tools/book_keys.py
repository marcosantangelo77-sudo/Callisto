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
