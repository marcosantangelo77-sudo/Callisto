"""
Characterization tests pinning the canonical Kelly contract.

tools.kelly.kelly_full(edge, american) is the CANONICAL implementation
(per tools/sizing.py module docstring). tools.sizing.kelly_binary must be
a thin wrapper over the same formula: same f* = (b*p - q)/b, with b =
decimal - 1, differing only in units (American vs decimal odds).

These tests were written BEFORE the delegation refactor and pin the
pre-refactor numeric behavior of both paths.
"""

import math

import pytest

from tools.kelly import kelly_full, _american_to_decimal
from tools.sizing import kelly_binary


class TestKellyFullCanonical:
    def test_docstring_example(self):
        # -110 => implied 0.5238095..., decimal 1.909090...
        # edge=0.03 -> p=0.553810, b=0.909091
        f = kelly_full(0.03, -110)
        assert f == pytest.approx((0.909091 * 0.553810 - 0.446190) / 0.909091, abs=1e-5)

    def test_is_rounded_to_six_places(self):
        f = kelly_full(0.0333, -110)
        assert f == round(f, 6)

    def test_negative_edge_clamps_to_zero(self):
        assert kelly_full(-0.05, -110) == 0.0

    def test_even_money(self):
        # +100: implied 0.5, b=1.0, edge=0.04 -> p=0.54, f=0.08
        assert kelly_full(0.04, 100) == pytest.approx(0.08)


class TestKellyBinaryCharacterization:
    def test_docstring_verified_value(self):
        # prob=.55 odds=2.10: b=1.1; f=(1.1*.55-.45)/1.1=.140909...
        assert kelly_binary(0.55, 2.10) == pytest.approx(0.1409090909, abs=1e-9)

    def test_no_edge_returns_zero(self):
        assert kelly_binary(1 / 2.10, 2.10) == pytest.approx(0.0, abs=1e-12)

    def test_negative_ev_clamps_to_zero(self):
        assert kelly_binary(0.40, 2.10) == 0.0

    def test_zero_or_negative_net_payout_guard(self):
        assert kelly_binary(0.55, 1.0) == 0.0
        assert kelly_binary(0.55, 0.5) == 0.0

    def test_not_rounded_pre_refactor(self):
        # Pre-refactor kelly_binary returns full float precision.
        f = kelly_binary(0.55, 2.10)
        assert abs(f - round(f, 6)) < 1e-12 or f != round(f, 6)


class TestEquivalence:
    CASES = [
        (0.03, -110),
        (0.05, 150),
        (0.02, -200),
        (0.075, 275),
        (-0.01, -105),
        (0.0, 100),
    ]

    def test_same_f_star_both_units(self):
        for edge, american in self.CASES:
            dec = _american_to_decimal(american)
            implied = (
                100 / (american + 100) if american > 0
                else abs(american) / (abs(american) + 100)
            )
            via_american = kelly_full(edge, american)
            via_decimal = kelly_binary(implied + edge, dec)
            # Wrapper rounding tolerance: +/-1e-6 max drift
            assert via_american == pytest.approx(via_decimal, abs=1e-6), (
                edge, american, via_american, via_decimal
            )

    def test_fixture_drift_within_tolerance(self):
        """Refactor must not move fixtures by more than +/-1e-6."""
        pre = {
            (0.55, 2.10): 0.14090909090909083,
            # p=.60, dec=1.909091: b=.909091; f=(.909091*.60-.40)/.909091=.16
            (0.60, 1.9090909090909092): 0.16000000000000003,
            (1 / 2.10, 2.10): 0.0,
            (0.40, 2.10): 0.0,
        }
        for (p, dec), expected in pre.items():
            assert kelly_binary(p, dec) == pytest.approx(expected, abs=1e-9)
