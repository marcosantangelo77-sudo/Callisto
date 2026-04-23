"""Unit tests for book-key canonicalization.

Locks down the audit fix for close_reliable=False contamination: book
keys arriving as "Betfair Exchange" (the-odds-api title-case), "betfair
exchange" (some scraper output), and "betfair_exchange" (odds-api.io
underscore form) must all collapse onto ONE canonical key.
"""

import pytest

from tools.book_keys import canonicalize_book, canonicalize_book_set


@pytest.mark.parametrize("raw", [
    "Betfair Exchange",
    "betfair exchange",
    "BETFAIR EXCHANGE",
    "betfair_exchange",
    "Betfair  Exchange",  # double space
    " betfair exchange ",  # padding
])
def test_betfair_exchange_collapses(raw):
    """Every casing/spacing variant → 'betfair_exchange'."""
    assert canonicalize_book(raw) == "betfair_exchange"


@pytest.mark.parametrize("raw,expected", [
    ("DraftKings", "draftkings"),
    ("draft_kings", "draftkings"),
    ("DRAFT-KINGS", "draftkings"),
    ("draftkings", "draftkings"),
    ("FanDuel", "fanduel"),
    ("fan_duel", "fanduel"),
    ("BetMGM", "betmgm"),
    ("Bet MGM", "betmgm"),
    ("Pinnacle", "pinnacle"),
    ("pinnacle", "pinnacle"),
    ("LowVig.ag", "lowvig.ag"),
    ("lowvig.ag", "lowvig.ag"),
    ("lowvig", "lowvig.ag"),
    ("low_vig_ag", "lowvig.ag"),
    ("MyBookie.ag", "mybookie.ag"),
    ("mybookie", "mybookie.ag"),
    ("Fanatics", "fanatics"),
    ("Fanatics Sportsbook", "fanatics"),
    ("Circa", "circa"),
])
def test_common_book_aliases(raw, expected):
    assert canonicalize_book(raw) == expected


def test_canonicalize_none_and_empty():
    assert canonicalize_book(None) == ""
    assert canonicalize_book("") == ""
    assert canonicalize_book("   ") == ""


def test_canonicalize_set_drops_empties_and_dedupes():
    result = canonicalize_book_set(
        ["Pinnacle", "pinnacle", "PINNACLE", "", None, "DraftKings"]
    )
    assert result == frozenset({"pinnacle", "draftkings"})


def test_reliable_close_sources_membership():
    """Integration: the canonical allowlist resolves all three casings.

    This is the scenario from the audit — closing_source arrives in any
    form and the membership test must succeed for every form that's
    actually the same book.
    """
    from tools.clv_tracker import _RELIABLE_CLOSE_SOURCES
    for variant in (
        "Betfair Exchange", "betfair exchange", "betfair_exchange",
        "Pinnacle", "pinnacle",
    ):
        assert canonicalize_book(variant) in _RELIABLE_CLOSE_SOURCES, (
            f"'{variant}' should resolve to a reliable close source"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
