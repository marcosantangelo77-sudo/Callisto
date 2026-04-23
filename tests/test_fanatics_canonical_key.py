"""Canonicalization round-trip for every spelling of "Fanatics" we've
seen in the wild — odds-api.io, the-odds-api.com, the live scraper, the
edge_scanner soft-book list, and legacy hard-coded strings.

If any of these fail to resolve to 'fanatics', downstream membership
tests (edge_scanner soft-book set, enrichment merges, CLV close-source
lookups) will silently drop rows.
"""

from __future__ import annotations

import pytest

from tools.book_keys import canonicalize_book, canonicalize_book_set


@pytest.mark.parametrize(
    "raw",
    [
        "Fanatics",
        "fanatics",
        "FANATICS",
        "Fanatics Sportsbook",
        "fanatics sportsbook",
        "Fanatics_Sportsbook",
        "fanatics_sportsbook",
        "Fanatics-Sportsbook",
        " Fanatics ",
        "Fanatics  Sportsbook",  # double space
    ],
)
def test_fanatics_all_spellings_canonicalize(raw):
    assert canonicalize_book(raw) == "fanatics"


def test_fanatics_in_canonical_set():
    result = canonicalize_book_set(
        ["Fanatics", "Fanatics Sportsbook", "fanatics_sportsbook"]
    )
    assert result == frozenset({"fanatics"})


def test_fanatics_matches_edge_scanner_soft_books():
    """The edge_scanner soft-book SOFT_TITLES set includes 'fanatics'
    (it was added when odds-api.io adopted the book). Every canonical
    form of Fanatics must resolve to the same membership key."""
    # Import lazily — the soft-book set lives inline in edge_scanner.
    import tools.edge_scanner as es
    # The module defines SOFT_TITLES inside detect_sharp_money; grep
    # the module source for canonical inclusion instead of pulling it
    # out of the function scope.
    src = open(es.__file__, encoding="utf-8").read()
    assert '"fanatics"' in src, "edge_scanner soft-book set should include 'fanatics'"
    for variant in ("Fanatics", "Fanatics Sportsbook", "fanatics_sportsbook"):
        assert canonicalize_book(variant) == "fanatics"


def test_scraper_emits_canonical_book_key():
    """The Fanatics scraper must emit the same canonical key that
    downstream code expects — otherwise enrichment won't match."""
    from tools.fanatics_scraper import _BOOK_KEY
    assert _BOOK_KEY == "fanatics"
    assert canonicalize_book(_BOOK_KEY) == "fanatics"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
