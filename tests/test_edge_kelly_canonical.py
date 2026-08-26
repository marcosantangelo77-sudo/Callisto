"""Characterization tests: freeze assess_edge's Kelly numbers before the
canonical-kelly_full delegation (OX task tools.edge -> tools.kelly).

These pin the CURRENT inline f* = (b*p_calibrated - q)/b behaviour on
american-price fixtures so the refactor to kelly_full() can be verified
numerically identical (±1e-6). Single-sided fixtures exercise the exact
algebraic equivalence; two-sided (devigged) fixtures pin that the Kelly
fraction continues to be computed against the OFFERED price with
p = calibrated_prob (not p = fair + edge), which is the documented intent
in tests/test_build_r5_edge.py.
"""

import pytest

from tools.edge import MarketQuote, assess_edge

# (name, calibrated_prob, price, counter_price_or_None,
#  expected_kelly_fraction_full, expected_actionable)
FIXTURES = [
    # Single-sided: raw implied == market fair -> algebraically identical
    # to kelly_full(edge_vs_price, price) up to its round(..., 6).
    ("fav_single_sided", 0.60, -150, None, 0.0, False),
    ("dog_single_sided", 0.40, 200, None, 0.10, True),
    ("near_even_single", 0.53, -105, None, 0.0365, True),
    ("big_fav_single", 0.75, -250, None, 0.125, True),
    ("no_edge_single", 0.45, -130, None, 0.0, False),
    # Two-sided devigged quotes: edge is vs market_fair but the stake is
    # priced at the quoted side. Freeze today's numbers exactly.
    ("fav_two_sided", 0.60, -150, -130, 0.0, False),
    ("dog_two_sided", 0.40, 200, 180, 0.10, False),
    ("near_even_two_sided", 0.53, -105, -115, 0.0365, True),
    ("big_fav_two_sided", 0.75, -250, -220, 0.125, True),
]


@pytest.mark.parametrize(
    "name,p,price,counter,expected_kelly,actionable",
    FIXTURES,
    ids=[f[0] for f in FIXTURES],
)
def test_kelly_fraction_frozen(name, p, price, counter, expected_kelly, actionable):
    a = assess_edge(
        name, p, MarketQuote(price=price, counter_price=counter, kind="american")
    )
    assert a.kelly_fraction_full == pytest.approx(expected_kelly, abs=1e-6)
    assert a.actionable is actionable


@pytest.mark.parametrize(
    "name,p,price,counter,expected_kelly,actionable",
    FIXTURES,
    ids=[f[0] for f in FIXTURES],
)
def test_quarter_kelly_is_quarter_of_full(name, p, price, counter, expected_kelly, actionable):
    a = assess_edge(
        name, p, MarketQuote(price=price, counter_price=counter, kind="american")
    )
    assert a.kelly_fraction_quarter == pytest.approx(expected_kelly / 4.0, abs=1e-9)


def test_cap_note_still_fires_above_max():
    # Full Kelly far above the 0.25 cap -> fraction capped, note recorded.
    a = assess_edge("capped", 0.95, MarketQuote(price=+300, kind="american"))
    assert a.kelly_fraction_full <= 0.25
    assert any("capped" in n for n in a.notes)
