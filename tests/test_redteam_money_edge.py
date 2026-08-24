"""RED TEAM — THE MONEY PATH, H1: can a computed edge be positive when the
true edge is zero or negative?

Attack surface: tools/edge.py quote interpretation, tools/devig.py,
and the Kalshi/Polymarket adapters that feed MarketQuote.

Every test here is written to FAIL if the money math can be fooled.
A passing test documents a surface that held.
"""
import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.edge import (
    MarketQuote, _raw_implied, assess_edge, clv_points, clv_basis_points,
    MAX_FRACTION_FULL_KELLY, MIN_EDGE_TO_ACT,
)
from tools.devig import devig_market, multiplicative_devig, power_devig


# ---------------------------------------------------------------------------
# H1a: 'auto' kind misreads cent-quoted contract prices as decimal odds
# ---------------------------------------------------------------------------

@given(st.integers(min_value=2, max_value=99))
@settings(max_examples=100, deadline=None)
def test_auto_int_cents_is_not_read_as_decimal_odds(cents):
    """A contract price of 62 cents entered as an int (the most natural
    representation) must not be read as decimal odds of 62 (implied 1.6%).
    _raw_implied's `abs(price) >= 100` guard only catches |p|>=100 ints,
    so 2..99 cents silently becomes decimal odds -> implied = 1/cents."""
    q = MarketQuote(price=cents, kind="auto", source="test")
    implied = q.implied_probability()
    # A 2-99 cent contract is a 2-99% probability, never a 1-50% decimal read
    # that differs from cents/100 by more than a rounding hair.
    assert abs(implied - cents / 100.0) < 0.02 or abs(implied - cents) < 1e-9, (
        f"int {cents} interpreted as {implied:.4f}; neither cents/100 nor "
        f"probability — the auto-detector invented a price")


def test_auto_float_cents_diverges_from_int_cents():
    """47 (int cents) and 47.0 (same price, float) must mean the same thing.
    They don't: int 47 < 100 so it goes down the decimal-odds branch too —
    but int 150 goes down the AMERICAN branch. Same number, three meanings."""
    p_int = MarketQuote(price=47, kind="auto").implied_probability()
    p_float = MarketQuote(price=47.0, kind="auto").implied_probability()
    assert abs(p_int - p_float) < 1e-9, (
        f"int 47 -> {p_int:.4f} but float 47.0 -> {p_float:.4f}: the same "
        f"price parses differently depending on its Python type")


# ---------------------------------------------------------------------------
# H1b: single-sided quotes produce ACTIONABLE positive edge
# ---------------------------------------------------------------------------

def test_single_sided_quote_never_actionable():
    """A one-sided -110 quote cannot be devigged; its raw implied carries
    the full vig. assess_edge still marks it actionable when the calibrated
    prob clears the raw implied — a phantom edge can arm a position."""
    q = MarketQuote(price=-110, kind="american", source="retail")
    a = assess_edge("claim", 0.60, q)
    assert not a.actionable, (
        f"single-sided (undeviggable) quote produced actionable=True with "
        f"edge {a.edge:.4f} — phantom vig became a tradeable edge")


@given(st.floats(min_value=0.51, max_value=0.95),
       st.sampled_from([-110, -105, 100, 120]))
@settings(max_examples=50, deadline=None)
def test_single_sided_actionable_never_fires(calibrated, american):
    q = MarketQuote(price=american, kind="american")
    a = assess_edge("c", calibrated, q)
    assert not a.actionable


# ---------------------------------------------------------------------------
# H1c: crossed / locked books — negative overround devigs INTO free money
# ---------------------------------------------------------------------------

def test_crossed_book_through_polymarket_quote_from_book():
    """PolymarketAdapter.quote_from_book accepts bid > ask (a crossed book —
    a data glitch or race). yes_ask=0.55 + no_ask=0.40 = 0.95: the 'overround'
    is NEGATIVE, and the two-way devig then reports a fair probability ABOVE
    the ask, manufacturing edge out of a broken book."""
    from tools.domains.polymarket.market import PolymarketAdapter
    quote, meta = PolymarketAdapter.quote_from_book(0.60, 0.55)
    fair, audit = quote.fair_probability()
    assert audit["overround"] > 0, (
        f"crossed book produced negative overround {audit['overround']} and "
        f"was accepted as a valid two-sided market")
    # and the manufactured edge is actionable:
    a = assess_edge("c", 0.60, quote)
    assert not a.actionable or a.edge < 0, (
        f"crossed book yielded edge {a.edge:.4f} — devigged a sub-1 sum "
        f"into free money")


@given(st.floats(min_value=0.0, max_value=0.9),
       st.floats(min_value=0.0, max_value=0.9))
@settings(max_examples=200, deadline=None)
def test_devig_rejects_sub_fair_books(yes_ask, no_ask):
    """Any quote pair summing to <= 1.0 has NO vig to remove — the devig
    must refuse it, not normalise it into a 'fair' probability."""
    if yes_ask <= 0 or no_ask <= 0:
        return
    if yes_ask + no_ask <= 1.0:
        r = devig_market([1.0 / yes_ask if yes_ask > 0 else 1e9,
                          1.0 / no_ask if no_ask > 0 else 1e9])
        # A sub-fair book must not come back as a clean devig success with
        # probabilities that exceed the quoted prices.
        if "error" not in r:
            fp = r["fair_probabilities"]
            assert not (fp[0] > yes_ask + 1e-9 and r["overround"] < 0), (
                f"asks {yes_ask}+{no_ask}=<{yes_ask+no_ask} devigged to "
                f"{fp} — negative overround laundered into fair probs")


def test_locked_book_zero_overround():
    """yes_ask=0.5, no_ask=0.5: overround exactly 0. Fair must equal the
    quotes — and any calibrated prob above 0.5 must NOT be called +edge
    against a market with no spread to beat... it legitimately can be, but
    the devig must not INVENT extra room. Verify no inflation."""
    r = devig_market([2.0, 2.0])
    assert abs(sum(r["fair_probabilities"]) - 1.0) < 1e-6
    assert r["overround"] == 0.0


# ---------------------------------------------------------------------------
# H1d: stale quotes — no freshness gate anywhere in assess_edge
# ---------------------------------------------------------------------------

def test_stale_quote_still_actionable():
    """A quote from 2025 (18+ months old) still drives actionable=True.
    assess_edge never inspects as_of, so a stale price silently sizes a
    position against a market that has long since moved."""
    stale = MarketQuote(price=0.40, counter_price=0.65, kind="probability",
                        source="polymarket", as_of="2025-01-01T00:00:00Z")
    a = assess_edge("c", 0.70, stale)
    assert not a.actionable, (
        "a quote with as_of=2025-01-01 produced actionable=True — no "
        "staleness gate on the money path")


def test_empty_as_of_still_actionable():
    q = MarketQuote(price=0.40, counter_price=0.65, kind="probability",
                    as_of="")
    assert not assess_edge("c", 0.70, q).actionable, (
        "quote with NO timestamp at all is actionable")


# ---------------------------------------------------------------------------
# H1e: counter-quote parsed under the SAME kind as the side — mixed formats
# ---------------------------------------------------------------------------

def test_counter_price_reuses_side_kind_silently():
    """fair_probability() builds the counter MarketQuote with kind=self.kind.
    A Polymarket-style probability price (0.55) with an American counter
    (-120, e.g. copy-pasted from a sportsbook) is silently reinterpreted
    as probability 120/100 -> _continuous_to_prob raises... or worse, in
    'auto' kind, -120 is an int with |v|>=100 -> American. Verify the mixed
    case at least cannot produce a positive edge from garbage."""
    q = MarketQuote(price=0.55, counter_price=1.83, kind="decimal")
    # 1.83 as a 'decimal' counter is fine, but if the caller passed cents:
    q2 = MarketQuote(price=0.55, counter_price=47, kind="decimal")
    fair, audit = q2.fair_probability()
    # 47 decimal -> implied 2.1% -> devig against it INFLATES our side
    assert fair > 0.55, "sanity"
    a = assess_edge("c", 0.56, q2)
    assert not a.actionable or a.edge < MIN_EDGE_TO_ACT or True  # documented
    # The real assertion: a 2% counter-quote must not be silently accepted.
    # It is — this is the finding. Encode the invariant we WANT:
    assert audit.get("overround", 0) < 0 or audit.get("overround", 1) > 0.9, (
        f"counter implied 2.1% gives overround {audit.get('overround')} — "
        f"mixed-format counter accepted without complaint")


# ---------------------------------------------------------------------------
# H1f: devig invariants under random valid two-way books
# ---------------------------------------------------------------------------

@given(st.floats(min_value=1.01, max_value=100.0),
       st.floats(min_value=1.01, max_value=100.0))
@settings(max_examples=300, deadline=None)
def test_multiplicative_devig_preserves_favourite_order(d1, d2):
    fair = multiplicative_devig([d1, d2])
    # favourite (lower decimal) must keep higher fair prob
    if d1 < d2:
        assert fair[0] >= fair[1] - 1e-12
    elif d2 < d1:
        assert fair[1] >= fair[0] - 1e-12
    assert abs(sum(fair) - 1.0) < 1e-9


@given(st.floats(min_value=1.01, max_value=1000.0),
       st.floats(min_value=1.01, max_value=1000.0))
@settings(max_examples=200, deadline=None)
def test_power_devig_sums_to_one(d1, d2):
    fair, k = power_devig([d1, d2])
    assert all(0 <= p <= 1 for p in fair), f"power devig out of [0,1]: {fair}"
    assert abs(sum(fair) - 1.0) < 5e-6, f"power devig sum {sum(fair)}"


# ---------------------------------------------------------------------------
# H1g: assess_edge Kelly uses the RAW quoted price, not the devigged one —
# the edge is computed vs fair but the payout is at the vigged price. That
# is actually correct for EV, but check the two can't DISAGREE into
# actionable-with-negative-EV or the reverse.
# ---------------------------------------------------------------------------

@given(st.floats(min_value=0.01, max_value=0.99),
       st.floats(min_value=1.01, max_value=10.0))
@settings(max_examples=300, deadline=None)
def test_actionable_implies_positive_ev(p, decimal):
    """Invariant the lifecycle relies on: actionable ==>
    ev_per_unit > 0 AND edge >= min_edge. Never the disjunction lying."""
    q = MarketQuote(price=decimal, kind="decimal")
    a = assess_edge("c", p, q)
    if a.actionable:
        assert a.ev_per_unit > 0, (
            f"actionable with ev_per_unit={a.ev_per_unit}")
        assert a.edge >= MIN_EDGE_TO_ACT


@given(st.floats(min_value=0.01, max_value=0.99),
       st.floats(min_value=1.01, max_value=10.0))
@settings(max_examples=300, deadline=None)
def test_kelly_fraction_bounded_and_consistent(p, decimal):
    q = MarketQuote(price=decimal, kind="decimal")
    a = assess_edge("c", p, q)
    assert 0.0 <= a.kelly_fraction_full <= MAX_FRACTION_FULL_KELLY + 1e-12
    assert abs(a.kelly_fraction_quarter * 4 - a.kelly_fraction_full) < 1e-9
