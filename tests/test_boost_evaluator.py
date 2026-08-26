"""Tests for profit boost evaluator — devigging, boost EV, hedging."""

import pytest
from tools.boost_evaluator import (
    devig_multiplicative,
    devig_additive,
    devig_multibook,
    evaluate_fixed_boost,
    evaluate_percentage_boost,
    evaluate_free_bet,
    calculate_hedge,
    find_optimal_boost_target,
)
# boost_evaluator now delegates to math_utils; these aliases preserve the
# older test naming while pointing at the real public functions.
from tools.math_utils import (
    american_to_decimal as _american_to_decimal,
    fair_prob_to_american as _prob_to_american,
)


class TestDevig:
    def test_multiplicative_standard_vig(self):
        """Standard -110/-110 should devig to 50/50."""
        a, b = devig_multiplicative(-110, -110)
        assert abs(a - 0.5) < 0.01
        assert abs(b - 0.5) < 0.01

    def test_multiplicative_sums_to_one(self):
        """Devigged probabilities must sum to 1.0."""
        a, b = devig_multiplicative(-150, 130)
        assert abs(a + b - 1.0) < 0.001

    def test_multiplicative_lopsided(self):
        """Heavy favorite / underdog devig."""
        a, b = devig_multiplicative(-300, 250)
        assert a > 0.7  # Favorite
        assert b < 0.3  # Underdog
        assert abs(a + b - 1.0) < 0.001

    def test_additive_standard(self):
        a, b = devig_additive(-110, -110)
        assert abs(a - 0.5) < 0.02
        assert abs(b - 0.5) < 0.02

    def test_additive_sums_near_one(self):
        a, b = devig_additive(-150, 130)
        assert abs(a + b - 1.0) < 0.05  # Additive is less precise

    def test_multibook_weights_sharp(self):
        """Pinnacle's odds should carry more weight."""
        books = [
            {"bookmaker": "Pinnacle", "odds_for": -140, "odds_against": 120},
            {"bookmaker": "FanDuel", "odds_for": -130, "odds_against": 110},
            {"bookmaker": "DraftKings", "odds_for": -135, "odds_against": 115},
        ]
        fair = devig_multibook(books)
        assert 0.5 < fair < 0.7  # Favorite side
        # Fair should be closer to Pinnacle's devigged value
        pinnacle_fair, _ = devig_multiplicative(-140, 120)
        fanduel_fair, _ = devig_multiplicative(-130, 110)
        # Weighted average should be closer to Pinnacle
        assert abs(fair - pinnacle_fair) < abs(fair - fanduel_fair)

    def test_multibook_empty(self):
        """Empty/malformed input is expected invalid external data: the safe
        no-result is None, never a fabricated neutral 0.5 fair value."""
        assert devig_multibook([]) is None
        assert devig_multibook([{}]) is None

    def test_multibook_invalid_odds_entry_dropped(self):
        """An entry with invalid American odds (fractional 100.9) contributes
        nothing rather than poisoning the consensus."""
        books = [
            {"bookmaker": "pinnacle", "odds_for": -110, "odds_against": 100.9},
            {"bookmaker": "fanduel", "odds_for": -112, "odds_against": -108},
        ]
        # Only one valid book remains → its devigged fair value.
        assert abs(devig_multibook(books) - 0.504330) < 1e-4

    def test_multibook_all_invalid_returns_none(self):
        # Zero-hold (+100/-100), invalid magnitude (50), and missing fields
        # are all unsanitary — nothing survives the gate.
        assert devig_multibook([
            {"bookmaker": "pinnacle", "odds_for": +100, "odds_against": -100},
            {"bookmaker": "fanduel", "odds_for": 50, "odds_against": -110},
            {"bookmaker": "draftkings"},
        ]) is None

    def test_invalid_boosted_odds_rejected(self):
        """Fractional American odds like 100.9 can never reach a SLAM."""
        from tools.boost_evaluator import evaluate_fixed_boost
        with pytest.raises(ValueError):
            evaluate_fixed_boost(boosted_odds=100.9, fair_probability=0.55)
        with pytest.raises(ValueError):
            evaluate_fixed_boost(boosted_odds=-110, fair_probability=1.5)
        result = evaluate_fixed_boost(boosted_odds=150, fair_probability=0.5)
        assert result["rating"] in {"MARGINAL", "GOOD", "STRONG", "EXCEPTIONAL", "NO_EDGE"}


class TestFixedBoost:
    def test_clear_plus_ev(self):
        """Boosted odds well above fair value → STRONG/EXCEPTIONAL."""
        result = evaluate_fixed_boost(
            boosted_odds=200,         # +200 boosted
            fair_probability=0.45,    # 45% true probability
            max_stake=50,
            description="Celtics ML boosted to +200",
            book="DraftKings",
        )
        assert result["ev_dollar"] > 0
        assert result["ev_pct"] > 0
        assert result["rating"] in ("STRONG", "EXCEPTIONAL", "GOOD")
        assert result["type"] == "FIXED_BOOST"

    def test_no_edge(self):
        """Boosted odds still below fair → NO_EDGE."""
        result = evaluate_fixed_boost(
            boosted_odds=-200,        # -200 even "boosted" is terrible
            fair_probability=0.45,    # True 45% but need 66.7% implied to break even
        )
        assert result["rating"] == "NO_EDGE"
        assert result["ev_dollar"] < 0

    def test_marginal_edge(self):
        """Small edge → MARGINAL."""
        result = evaluate_fixed_boost(
            boosted_odds=-105,
            fair_probability=0.52,
        )
        # With 52% prob and -105 odds (implied ~51.2%), edge is ~0.8% → MARGINAL
        assert result["edge"] > 0

    def test_includes_kelly(self):
        result = evaluate_fixed_boost(boosted_odds=150, fair_probability=0.5)
        assert "kelly" in result
        assert "kelly_fraction" in result["kelly"]


class TestPercentageBoost:
    def test_boost_increases_ev(self):
        """Percentage boost should add EV above the unboosted bet."""
        result = evaluate_percentage_boost(
            boost_pct=30,
            base_odds=200,
            fair_probability=0.40,
            max_stake=100,
        )
        assert result["boost_added_ev"] > 0
        assert result["boosted_profit"] > result["base_profit"]
        assert result["type"] == "PERCENTAGE_BOOST"

    def test_30pct_on_plus_200(self):
        """30% boost on +200: profit goes from $200 to $260."""
        result = evaluate_percentage_boost(
            boost_pct=30,
            base_odds=200,
            fair_probability=0.40,
            max_stake=100,
        )
        assert result["base_profit"] == 200.0
        assert result["boosted_profit"] == 260.0

    def test_100pct_boost_doubles_profit(self):
        result = evaluate_percentage_boost(
            boost_pct=100,
            base_odds=150,
            fair_probability=0.40,
            max_stake=100,
        )
        assert result["boosted_profit"] == result["base_profit"] * 2

    def test_effective_odds_calculated(self):
        result = evaluate_percentage_boost(
            boost_pct=50,
            base_odds=200,
            fair_probability=0.40,
        )
        assert result["effective_odds"] > 200  # Effective odds should be better


class TestFreeBet:
    def test_free_bet_positive_ev(self):
        """Free bet on underdog → always positive EV since no stake at risk."""
        result = evaluate_free_bet(
            free_bet_amount=100,
            bet_odds=300,
            fair_probability=0.30,
            stake_returned=False,
        )
        assert result["ev_dollar"] > 0  # Free bets are always +EV
        assert result["type"] == "FREE_BET"
        assert result["profit_if_win"] == 300.0  # $100 × (3.0 - 1.0) decimal

    def test_no_sweat_bet(self):
        """No-sweat: get credit back if lost → higher EV than free bet."""
        result_free = evaluate_free_bet(
            free_bet_amount=100, bet_odds=200, fair_probability=0.40,
            stake_returned=False,
        )
        result_nosweat = evaluate_free_bet(
            free_bet_amount=100, bet_odds=200, fair_probability=0.40,
            stake_returned=True,
        )
        assert result_nosweat["type"] == "NO_SWEAT"
        # No-sweat should show positive expected value accounting for credit
        assert result_nosweat["expected_value"] > 0

    def test_conversion_rate(self):
        result = evaluate_free_bet(
            free_bet_amount=100, bet_odds=300, fair_probability=0.30,
        )
        assert result["conversion_rate"] > 0


class TestHedge:
    def test_guaranteed_profit(self):
        """Hedge should produce guaranteed profit on a boosted bet."""
        result = calculate_hedge(
            boost_stake=100,
            boosted_odds=200,       # +200 boosted
            hedge_odds=-110,        # Other side at another book
            fair_probability=0.45,
        )
        assert result["guaranteed_profit"] > 0
        assert result["hedge_stake"] > 0
        assert result["total_outlay"] > 100

    def test_hedge_vs_ride_recommendation(self):
        result = calculate_hedge(
            boost_stake=50,
            boosted_odds=300,
            hedge_odds=-120,
            fair_probability=0.40,
        )
        assert result["recommendation"] in (
            "LET IT RIDE — long-run EV of not hedging exceeds guaranteed profit",
            "HEDGE — lock in guaranteed profit, reduce variance",
        )

    def test_equal_payouts(self):
        """Both outcomes should yield similar profit when properly hedged."""
        result = calculate_hedge(
            boost_stake=100,
            boosted_odds=150,
            hedge_odds=-130,
            fair_probability=0.50,
        )
        # Profits should be relatively close (not exact due to rounding)
        diff = abs(result["profit_if_boost_wins"] - result["profit_if_hedge_wins"])
        assert diff < 5  # Within $5


class TestOptimalBoostTarget:
    def test_ranks_by_boost_ev(self):
        bets = [
            {"odds": -110, "fair_probability": 0.52, "description": "Low odds"},
            {"odds": 200, "fair_probability": 0.40, "description": "Mid odds"},
            {"odds": 500, "fair_probability": 0.18, "description": "Long odds"},
        ]
        ranked = find_optimal_boost_target(boost_pct=30, available_bets=bets)
        assert len(ranked) == 3
        # Should be sorted by boost_added_ev descending
        assert ranked[0]["boost_added_ev"] >= ranked[1]["boost_added_ev"]
        assert ranked[1]["boost_added_ev"] >= ranked[2]["boost_added_ev"]

    def test_longer_odds_benefit_more(self):
        """Longer odds get more dollar value from percentage boosts."""
        bets = [
            {"odds": -110, "fair_probability": 0.55, "description": "Short"},
            {"odds": 300, "fair_probability": 0.25, "description": "Long"},
        ]
        ranked = find_optimal_boost_target(boost_pct=50, available_bets=bets)
        # Long odds should have higher boost_added_ev
        long = next(r for r in ranked if r["description"] == "Long")
        short = next(r for r in ranked if r["description"] == "Short")
        assert long["boost_added_ev"] > short["boost_added_ev"]


class TestHelpers:
    def test_american_to_decimal_positive(self):
        assert _american_to_decimal(200) == 3.0

    def test_american_to_decimal_negative(self):
        assert _american_to_decimal(-200) == 1.5

    def test_american_to_decimal_even(self):
        assert _american_to_decimal(100) == 2.0

    def test_prob_to_american_favorite(self):
        odds = _prob_to_american(0.6)
        assert odds == -150

    def test_prob_to_american_underdog(self):
        odds = _prob_to_american(0.4)
        assert odds == 150

    def test_prob_to_american_edges(self):
        assert _prob_to_american(0.0) == 0
        assert _prob_to_american(1.0) == 0
