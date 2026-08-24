"""Property tests for tools/edge.assess_edge payout pricing.

Regression context (2026-08-23): assess_edge computed EV and Kelly from
_to_decimal(quote.price), which re-runs AUTO price detection and ignored
quote.kind entirely. A kind='contract_cents' quote of 47 (= $0.47, decimal
2.128) was read as decimal odds 47.0, inflating ev_per_unit ~98x (27.2 vs
0.277) and pinning Kelly at its cap. Existing unit tests missed it because
every one of them fed American odds, where auto-detection coincides.

The property below is the sweep PATTERNS.md asks for: for ANY representation
of the SAME market price, every derived quantity in EdgeAssessment must be
identical (up to devig rounding). A label must never decide a money number.
"""

import itertools
import random

import pytest

from tools.edge import MarketQuote, assess_edge


def _same_market_quotes():
    """One market price ($0.47 / $0.54 two-sided) in every accepted encoding."""
    return [
        MarketQuote(price=0.47, counter_price=0.54, kind="probability"),
        MarketQuote(price=47, counter_price=54, kind="contract_cents"),
        MarketQuote(price=1.0 / 0.47, counter_price=1.0 / 0.54, kind="decimal"),
        # auto-detection paths that happen to land on 0.47:
        MarketQuote(price=0.47, counter_price=0.54),          # prob in (0,1]
    ]


class TestKindNeverChangesTheMoney:
    def test_identical_price_every_encoding_same_assessment(self):
        assessments = [
            assess_edge("prop", 0.60, q) for q in _same_market_quotes()
        ]
        ref = assessments[0]
        for a in assessments[1:]:
            assert a.market_prob_raw == pytest.approx(ref.market_prob_raw)
            assert a.market_prob_fair == pytest.approx(
                ref.market_prob_fair, abs=1e-6)
            assert a.edge == pytest.approx(ref.edge, abs=1e-6)
            assert a.ev_per_unit == pytest.approx(ref.ev_per_unit, abs=1e-9)
            assert a.kelly_fraction_full == pytest.approx(
                ref.kelly_fraction_full, abs=1e-9)
            assert a.kelly_fraction_quarter == pytest.approx(
                ref.kelly_fraction_quarter, abs=1e-9)

    def test_ev_is_bounded_by_brute_truth(self):
        """EV per unit can never exceed b = 1/p - 1 even at p_calibrated=1."""
        for p_mkt in [0.05, 0.3, 0.47, 0.5, 0.7, 0.95]:
            q = MarketQuote(price=p_mkt, counter_price=min(p_mkt + 0.07, 0.99),
                            kind="probability")
            a = assess_edge("bound", 0.999999, q)
            assert a.ev_per_unit <= 1.0 / p_mkt - 1.0 + 1e-9

    def test_random_prices_kind_consistency(self):
        """Sweep: for random p in (0,1), contract_cents and probability
        encodings of the same price must agree exactly on EV and Kelly."""
        rng = random.Random(20260823)
        for _ in range(500):
            p = rng.uniform(0.02, 0.98)
            c = min(p + rng.uniform(0.0, 0.1), 0.995)
            cal = rng.uniform(0.01, 0.99)
            a_prob = assess_edge("r", cal,
                                 MarketQuote(price=p, counter_price=c,
                                             kind="probability"))
            a_cents = assess_edge("r", cal,
                                  MarketQuote(price=p * 100, counter_price=c * 100,
                                              kind="contract_cents"))
            assert a_prob.ev_per_unit == pytest.approx(
                a_cents.ev_per_unit, rel=1e-6), (p, c, cal)
            assert a_prob.kelly_fraction_full == pytest.approx(
                a_cents.kelly_fraction_full, rel=1e-6)

    def test_kelly_quarter_is_quarter_of_full(self):
        rng = random.Random(7)
        for _ in range(200):
            p = rng.uniform(0.05, 0.95)
            a = assess_edge("k", rng.uniform(0.01, 0.99),
                            MarketQuote(price=p,
                                        counter_price=min(p + 0.05, 0.99),
                                        kind="probability"))
            assert a.kelly_fraction_quarter == pytest.approx(
                a.kelly_fraction_full / 4.0, abs=1e-12)


class TestContractCentsReferenceCase:
    """Hand-derived reference for the exact case that was broken."""

    def test_47_cents_reference(self):
        q = MarketQuote(price=47, counter_price=54, kind="contract_cents")
        a = assess_edge("ref", 0.60, q)
        b = 1.0 / 0.47 - 1.0                      # true payout ratio
        assert a.ev_per_unit == pytest.approx(0.60 * b - 0.40, rel=1e-9)
        assert a.kelly_fraction_full == pytest.approx(
            (b * 0.60 - 0.40) / b, rel=1e-9)      # ~0.245, NOT the 0.25 cap

    def test_cap_note_only_when_genuinely_over_cap(self):
        # 0.245 < cap: no cap note may appear.
        q = MarketQuote(price=47, counter_price=54, kind="contract_cents")
        a = assess_edge("ref", 0.60, q)
        assert not any("capped" in n for n in a.notes)
