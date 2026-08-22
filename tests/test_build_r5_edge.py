"""R5 build — tools/edge.py: quote abstraction, devigged edge, Kelly, CLV.

Reference vectors hand-derived (findings/instance2.md Kelly derivation):
  - American -110 -> implied 110/210 = 0.5238095...
  - Fair two-way market -110/-110 devigs both sides to exactly 0.5.
  - p=0.55 at decimal 2.00 (even money): b=1, Kelly f* = b*p - q = 0.55-0.45
    = 0.10; EV per unit stake = 0.55*1 - 0.45 = +0.10.
  - p=0.55 at decimal 1.9091 (-110): b=0.90909..., f*=(0.90909*0.55-0.45)/0.90909
    = (0.5-0.45)/0.90909 = 0.055.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.edge import (
    EdgeAssessment,
    MarketQuote,
    _raw_implied,
    assess_edge,
    clv_basis_points,
    clv_points,
)


class TestQuoteNormalization:
    def test_american_minus_110(self):
        assert abs(_raw_implied(-110) - 110 / 210) < 1e-12

    def test_american_plus_150(self):
        assert abs(_raw_implied(150) - 100 / 250) < 1e-12

    def test_decimal(self):
        assert abs(_raw_implied(2.50) - 0.40) < 1e-12

    def test_contract_cents_requires_explicit_kind(self):
        # cent prices are NOT guessed in auto mode (47 as decimal odds is
        # legitimate); the caller must declare the kind.
        assert _raw_implied(2.13) == pytest.approx(1 / 2.13)   # plain decimal
        q = MarketQuote(price=47, counter_price=54, kind="contract_cents")
        assert q.implied_probability() == pytest.approx(0.47)
        fair, audit = q.fair_probability()
        assert audit["devigged"] and fair == pytest.approx(0.47 / (0.47 + 0.54), abs=1e-4)

    def test_probability_passthrough(self):
        assert abs(_raw_implied(0.62) - 0.62) < 1e-15

    def test_rejects_garbage(self):
        for bad in (0, "x", float("nan"), float("inf"), -3.0, True):
            with pytest.raises(ValueError):
                MarketQuote(price=bad).implied_probability()

    def test_bool_is_rejected_even_though_it_equals_one(self):
        with pytest.raises(ValueError):
            MarketQuote(price=True).implied_probability()

class TestDevig:
    def test_fair_two_way_devigs_to_half(self):
        q = MarketQuote(price=-110, counter_price=-110)
        fair, audit = q.fair_probability()
        assert audit["devigged"] is True
        # Overround exists (2 * 110/210 = 1.0476), so raw is NOT 0.5 but the
        # devigged probability must land on exactly even.
        assert abs(q.implied_probability() - 110.0 / 210.0) < 1e-12
        assert abs(fair - 0.5) < 1e-6

    def test_single_sided_flagged_not_devigged(self):
        q = MarketQuote(price=-110)
        fair, audit = q.fair_probability()
        assert audit["devigged"] is False
        assert abs(fair - 110 / 210) < 1e-12


class TestEdgeAndKelly:
    def test_hand_derived_even_money(self):
        """p=0.55, quoted -105/-105. Fair prob 0.5, decimal payout 1/0.523809...
        Kelly f* = (b*p - q)/b with b = 1/1.05238 - 1 = 0.090909...:
        f* = (0.090909*0.55 - 0.45)/0.090909 = (0.05-0.45)/0.090909 -> negative!
        No — b here is the payout at the QUOTED (vigged) price, while the edge
        is vs the devigged market prob. Correct vector: p=0.55 at -105:
        b = 100/105 = 0.9523809..., f* = (b*0.55 - 0.45)/b
          = (0.5238095 - 0.45)/0.9523809 = 0.0775."""
        a = assess_edge(
            "claim-1", 0.55,
            MarketQuote(price=-105, counter_price=-105),
        )
        assert abs(a.market_prob_fair - 0.5) < 1e-6
        assert abs(a.edge - 0.05) < 1e-6
        b = 1.0 / a.quote.implied_probability() - 1.0   # 100/105
        expected_kelly = (b * 0.55 - 0.45) / b           # hand-derived: 0.0775
        assert a.kelly_fraction_full == pytest.approx(min(expected_kelly, 0.25), abs=1e-9)
        assert a.actionable

    def test_no_edge_when_market_agrees(self):
        a = assess_edge(
            "claim-2", 0.50,
            MarketQuote(price=-110, counter_price=-110),
        )
        assert abs(a.edge) < 1e-9
        assert not a.actionable
        assert a.kelly_fraction_full == 0.0

    def test_ev_per_unit_sign_matches_actionability(self):
        # calibrated far below market -> negative EV, not actionable
        a = assess_edge(
            "claim-3", 0.30,
            MarketQuote(price=-300, counter_price=+260),  # fair ~0.53
        )
        assert a.ev_per_unit < 0
        assert not a.actionable

    def test_kelly_cap_applies(self):
        """Huge perceived edge must be capped, never sized full."""
        a = assess_edge(
            "claim-4", 0.98,
            MarketQuote(price=+400, counter_price=-500),  # fair ~0.18
        )
        assert a.kelly_fraction_full == 0.25  # MAX_FRACTION_FULL_KELLY
        assert any("capped" in n for n in a.notes)

    def test_quarter_kelly_is_quarter(self):
        a = assess_edge("claim-5", 0.55, MarketQuote(price=-108, counter_price=-102))
        assert abs(a.kelly_fraction_quarter * 4 - a.kelly_fraction_full) < 1e-12

    def test_summary_records_quote_for_clv(self):
        a = assess_edge("claim-6", 0.6,
                        MarketQuote(price=47, counter_price=54,
                                    source="polymarket", as_of="2026-08-22T00:00:00Z"))
        s = a.summary()
        assert s["quote"]["source"] == "polymarket"
        assert s["quote"]["as_of"] == "2026-08-22T00:00:00Z"
        assert s["market_prob_fair"] == pytest.approx(a.market_prob_fair, abs=1e-6)


class TestGeneralisedCLV:
    def test_clv_positive_when_line_moves_toward_claim(self):
        claim = MarketQuote(price=-110, counter_price=-110)
        close = MarketQuote(price=-130, counter_price=+115)
        pts = clv_points(claim, close)
        assert pts is not None and pts > 0.01
        bp = clv_basis_points(claim, close)
        assert bp == pytest.approx(pts * 10_000, abs=0.01)

    def test_clv_none_without_devig(self):
        # single-sided quotes: refuse to compute rather than produce phantom CLV
        assert clv_points(MarketQuote(price=-110), MarketQuote(price=-130)) is None

    def test_clv_zero_when_line_unchanged(self):
        same = dict(price=-110, counter_price=-110)
        v = clv_points(MarketQuote(**same), MarketQuote(**same))
        assert abs(v) < 1e-12
