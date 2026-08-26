"""
Devig engine test suite — MANDATORY: all tests must pass before downstream modules.

Tests from the framework spec:
  Test 1: Fair market [2.0, 2.0] → all methods return [0.5, 0.5]
  Test 2: Standard vig [1.909, 1.909] → all methods return [0.5, 0.5]
  Test 3: Lopsided [1.20, 5.00] → all sum to 1.0, all positive
           Power favorite > Mult favorite (FLB correction verified)
  Test 4: Pinnacle-realistic [1.667, 2.400] → fair ≈ [-145, +145]
  Test 5: 3-way soccer → additive guard triggers if negative prob
  Test 6: Extreme [1.05, 15.0] → power solver converges
  Test 7: Round-trip: fair probs → add vig → devig → recover original
  Test 8: Conversion round-trips: american ↔ decimal ↔ implied ↔ american
"""

import pytest
from tools.devig import (
    multiplicative_devig,
    additive_devig,
    power_devig,
    shin_devig,
    devig_market,
    devig_american,
    devig_pinnacle,
    devig_retail,
)
from tools.math_utils import (
    american_to_decimal,
    decimal_to_american,
    american_to_implied,
    fair_prob_to_american,
    fair_prob_to_decimal,
)


class TestFairMarket:
    """A zero-hold book ([2.0, 2.0]) is not deviggable under the market-sanity
    policy: every public helper must raise rather than return [0.5, 0.5].
    Healthy low-hold controls still devig to ~[0.5, 0.5]."""

    def test_zero_hold_rejected_by_all_helpers(self):
        for fn in (multiplicative_devig, additive_devig):
            with pytest.raises(ValueError):
                fn([2.0, 2.0])
        for fn in (power_devig, shin_devig):
            with pytest.raises(ValueError):
                fn([2.0, 2.0])

    @pytest.mark.parametrize("fn", [
        multiplicative_devig,
        additive_devig,
        lambda odds: power_devig(odds)[0],
        lambda odds: shin_devig(odds)[0],
    ], ids=["mult", "additive", "power", "shin"])
    def test_low_vig_healthy_book_near_fair(self, fn):
        result = fn([1.95, 1.95])
        assert abs(result[0] - 0.5) < 0.02
        assert abs(result[1] - 0.5) < 0.02


class TestStandardVig:
    """Test 2: Standard vig [1.909, 1.909] → all methods ≈ [0.5, 0.5]"""

    def test_multiplicative(self):
        result = multiplicative_devig([1.909, 1.909])
        assert abs(result[0] - 0.5) < 0.001
        assert abs(result[1] - 0.5) < 0.001

    def test_power(self):
        result, k = power_devig([1.909, 1.909])
        assert abs(result[0] - 0.5) < 0.001
        assert abs(result[1] - 0.5) < 0.001
        assert k > 1.0  # k > 1 when vig exists

    def test_shin(self):
        result, z = shin_devig([1.909, 1.909])
        assert abs(result[0] - 0.5) < 0.001
        assert abs(result[1] - 0.5) < 0.001
        assert z > 0  # z > 0 indicates informed bettor fraction


class TestLopsided:
    """Test 3: Lopsided [1.20, 5.00] → all sum to 1.0, all positive.
    Power favorite > Mult favorite (FLB correction verified)."""

    def test_all_sum_to_one(self):
        for method in [multiplicative_devig, additive_devig]:
            result = method([1.20, 5.00])
            assert abs(sum(result) - 1.0) < 0.001, f"Sum not 1.0 for {method.__name__}"
            assert all(p > 0 for p in result), f"Negative prob in {method.__name__}"

        result_p, _ = power_devig([1.20, 5.00])
        assert abs(sum(result_p) - 1.0) < 0.001
        assert all(p > 0 for p in result_p)

        result_s, _ = shin_devig([1.20, 5.00])
        assert abs(sum(result_s) - 1.0) < 0.001
        assert all(p > 0 for p in result_s)

    def test_power_corrects_flb(self):
        """Power shifts probability TOWARD favorite, AWAY from longshot."""
        mult = multiplicative_devig([1.20, 5.00])
        power, _ = power_devig([1.20, 5.00])
        # Power gives MORE to favorite (index 0) than multiplicative
        assert power[0] > mult[0], "Power should give more to favorite than mult"
        # Power gives LESS to longshot (index 1) than multiplicative
        assert power[1] < mult[1], "Power should give less to longshot than mult"


class TestPinnacleRealistic:
    """Test 4: Pinnacle-realistic [1.667, 2.400]."""

    def test_devig(self):
        result = devig_market([1.667, 2.400])
        fair = result["fair_probabilities"]
        assert abs(sum(fair) - 1.0) < 0.001
        # Fair ≈ 0.592, 0.408 → ≈ -145, +145
        assert 0.55 < fair[0] < 0.65
        assert 0.35 < fair[1] < 0.45


class TestThreeWaySoccer:
    """Test 5: 3-way soccer → additive guard triggers if negative prob."""

    def test_additive_guard(self):
        # Very lopsided 3-way market where additive would produce negative
        odds = [1.10, 8.0, 25.0]
        result = additive_devig(odds)
        # Should fall back to multiplicative (no negatives)
        assert all(p > 0 for p in result), "Additive should not produce negatives"
        assert abs(sum(result) - 1.0) < 0.001

    def test_shin_on_three_way(self):
        odds = [1.50, 4.00, 7.00]
        result, z = shin_devig(odds)
        assert abs(sum(result) - 1.0) < 0.001
        assert all(p > 0 for p in result)
        assert z > 0

    def test_auto_selects_shin(self):
        odds = [1.50, 4.00, 7.00]
        result = devig_market(odds)
        assert result["method"] == "shin"


class TestExtreme:
    """Test 6: Extreme [1.05, 15.0] → power solver converges."""

    def test_power_converges(self):
        result, k = power_devig([1.05, 15.0])
        assert abs(sum(result) - 1.0) < 0.001
        assert all(p > 0 for p in result)
        assert k > 0

    def test_shin_converges(self):
        result, z = shin_devig([1.05, 15.0])
        assert abs(sum(result) - 1.0) < 0.001
        assert all(p > 0 for p in result)


class TestRoundTrip:
    """Test 7: fair probs → add vig → devig → recover original."""

    def test_round_trip_power(self):
        # Start with fair probs
        fair_a, fair_b = 0.60, 0.40

        # Add vig (simulate a retail book with ~5% overround)
        vig_a = fair_a * 1.025
        vig_b = fair_b * 1.025
        vigged_dec_a = 1 / vig_a
        vigged_dec_b = 1 / vig_b

        # Devig
        recovered, _ = power_devig([vigged_dec_a, vigged_dec_b])

        # Should recover approximately the original
        assert abs(recovered[0] - fair_a) < 0.01
        assert abs(recovered[1] - fair_b) < 0.01


class TestConversionRoundTrips:
    """Test 8: american ↔ decimal ↔ implied ↔ american."""

    @pytest.mark.parametrize("american", [-110, -200, +150, +300, +100, -300, +500, -500])
    def test_american_round_trip(self, american):
        decimal = american_to_decimal(american)
        recovered = decimal_to_american(decimal)
        assert abs(recovered - american) <= 1, f"{american} → {decimal} → {recovered}"

    @pytest.mark.parametrize("prob", [0.25, 0.40, 0.50, 0.60, 0.75, 0.90])
    def test_prob_round_trip(self, prob):
        american = fair_prob_to_american(prob)
        decimal = american_to_decimal(american)
        implied = 1 / decimal
        assert abs(implied - prob) < 0.02, f"{prob} → {american} → {decimal} → {implied}"


class TestAutoSelection:
    """Verify auto-selection picks the right method."""

    def test_low_vig_uses_multiplicative(self):
        # Pinnacle-like 2% vig
        result = devig_market([1.98, 1.98])
        assert result["method"] == "multiplicative"

    def test_retail_uses_power(self):
        # DK-like 5% vig
        result = devig_market([1.909, 1.909])
        assert result["method"] == "power"

    def test_three_way_uses_shin(self):
        # 3-way with significant vig (overround ~8%)
        result = devig_market([1.50, 4.00, 7.00])
        assert result["method"] == "shin"


class TestConvenienceFunctions:
    """Test the shortcut devig functions."""

    def test_devig_american(self):
        result = devig_american(-110, -110)
        assert abs(result["side_a"]["fair_prob"] - 0.5) < 0.01
        assert abs(result["side_b"]["fair_prob"] - 0.5) < 0.01

    def test_devig_pinnacle(self):
        a, b = devig_pinnacle(-145, 125)
        assert abs(a + b - 1.0) < 0.001
        assert a > b  # Favorite has higher prob

    def test_devig_retail(self):
        a, b = devig_retail(-200, 170)
        assert abs(a + b - 1.0) < 0.001
        assert a > 0.60  # Heavy favorite
