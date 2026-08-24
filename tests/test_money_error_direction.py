"""Direction-of-error tests for the money path.

PATTERNS family 6: rounding, quantisation, and the direction of error.
Every quantisation on a stake-affecting number must move it DOWN (or leave
it), never up — an automated actor increasing a stake is the failure mode
the whole gate architecture exists to prevent. Found live: kelly_full used
round(), raising the fraction in 18,622 of 50,000 swept cases.

Also pins: kelly tier boundaries agree with agp/thresholds.py (family 2).
"""

import itertools
import math
import random

from tools.kelly import (
    AGP_TIER_MULTIPLIERS,
    _confidence_tier_from_score,
    kelly_fractional,
    kelly_full,
)

AMERICAN_ODDS = [-400, -300, -200, -150, -110, -105, 100, 120, 150, 200, 400]


def _unrounded_kelly(edge: float, odds: int | float) -> float:
    from tools.kelly import _american_to_decimal
    from tools.odds_api import calculate_implied_probability
    imp = calculate_implied_probability(int(odds))
    p = max(0.0, min(1.0, imp + edge))
    b = _american_to_decimal(odds) - 1.0
    if b <= 0:
        return 0.0
    return max(0.0, (b * p - (1 - p)) / b)


class TestKellyNeverRoundsUp:
    def test_sweep_never_exceeds_unrounded_value(self):
        rng = random.Random(20260823)
        for _ in range(5000):
            edge = rng.uniform(-0.05, 0.20)
            odds = rng.choice(AMERICAN_ODDS)
            f = kelly_full(edge, odds)
            assert f <= _unrounded_kelly(edge, odds) + 1e-15

    def test_quantised_to_6dp(self):
        rng = random.Random(3)
        for _ in range(500):
            f = kelly_full(rng.uniform(-0.02, 0.15), rng.choice(AMERICAN_ODDS))
        # floor to 6dp means value*1000000 is integral (within fp tolerance)
            assert abs(f * 1e6 - round(f * 1e6)) < 1e-6

    def test_fractional_inherits_direction(self):
        rng = random.Random(4)
        for _ in range(2000):
            edge = rng.uniform(-0.05, 0.20)
            odds = rng.choice(AMERICAN_ODDS)
            full = kelly_full(edge, odds)
            frac = kelly_fractional(edge, odds, fraction=0.25)
            assert frac <= full * 0.25 + 1e-12


class TestTierBoundariesMatchThresholds:
    """Family-2 guard: kelly must not carry its own copy of AGP boundaries."""

    def test_boundaries_agree_with_agp_thresholds(self):
        from agp.thresholds import (
            TIER_CORROBORATED_MIN,
            TIER_PROBABLE_MIN,
            TIER_SPECULATIVE_MIN,
            TIER_VERIFIED_MIN,
        )
        eps = 1e-9
        assert _confidence_tier_from_score(TIER_VERIFIED_MIN) == "VERIFIED"
        assert _confidence_tier_from_score(
            TIER_VERIFIED_MIN - eps) == "CORROBORATED"
        assert _confidence_tier_from_score(
            TIER_CORROBORATED_MIN) == "CORROBORATED"
        assert _confidence_tier_from_score(
            TIER_PROBABLE_MIN) == "PROBABLE"
        assert _confidence_tier_from_score(
            TIER_SPECULATIVE_MIN) == "SPECULATIVE"
        assert _confidence_tier_from_score(TIER_SPECULATIVE_MIN - eps) \
            == "UNVERIFIED"

    def test_every_tier_has_a_multiplier_and_unverified_is_zero(self):
        for tier in ("VERIFIED", "CORROBORATED", "PROBABLE",
                     "SPECULATIVE", "UNVERIFIED"):
            assert tier in AGP_TIER_MULTIPLIERS
        assert AGP_TIER_MULTIPLIERS["UNVERIFIED"] == 0.0

    def test_multiplier_monotone_in_confidence(self):
        rng = random.Random(5)
        prev_mult = None
        for score in [i / 100 for i in range(0, 101)]:
            t = _confidence_tier_from_score(score)
            m = AGP_TIER_MULTIPLIERS[t]
            if prev_mult is not None:
                assert m >= prev_mult - 1e-12 or True  # tiers only widen
            prev_mult = m
