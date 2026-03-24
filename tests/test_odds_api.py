"""Tests for odds API tools — EV calculations, line movement detection, implied probability."""

import pytest
from tools.odds_api import (
    calculate_ev,
    calculate_implied_probability,
    find_best_line,
    detect_line_movement,
)


class TestImpliedProbability:
    def test_negative_odds(self):
        # -110 → 110/210 ≈ 0.5238
        prob = calculate_implied_probability(-110)
        assert abs(prob - 0.5238) < 0.001

    def test_positive_odds(self):
        # +150 → 100/250 = 0.4
        prob = calculate_implied_probability(150)
        assert abs(prob - 0.4) < 0.001

    def test_heavy_favorite(self):
        # -300 → 300/400 = 0.75
        prob = calculate_implied_probability(-300)
        assert abs(prob - 0.75) < 0.001

    def test_big_underdog(self):
        # +500 → 100/600 ≈ 0.1667
        prob = calculate_implied_probability(500)
        assert abs(prob - 0.1667) < 0.001

    def test_even_odds(self):
        # +100 → 100/200 = 0.5
        prob = calculate_implied_probability(100)
        assert abs(prob - 0.5) < 0.001


class TestCalculateEV:
    def test_positive_ev(self):
        # If true prob is 0.55 and line is -110, that's +EV
        result = calculate_ev(probability=0.55, american_odds=-110)
        assert result["is_positive_ev"] is True
        assert result["expected_value"] > 0
        assert result["edge"] > 0
        assert result["kelly_fraction"] > 0

    def test_negative_ev(self):
        # If true prob is 0.45 and line is -110, that's -EV
        result = calculate_ev(probability=0.45, american_odds=-110)
        assert result["is_positive_ev"] is False
        assert result["expected_value"] < 0
        assert result["edge"] < 0
        assert result["kelly_fraction"] == 0  # Kelly says don't bet

    def test_kelly_never_negative(self):
        result = calculate_ev(probability=0.1, american_odds=-500)
        assert result["kelly_fraction"] >= 0

    def test_strong_edge(self):
        # True prob 0.7, line offers +100 (implied 0.5) — huge edge
        result = calculate_ev(probability=0.7, american_odds=100)
        assert result["edge"] > 0.15
        assert result["kelly_fraction"] > 0.3
        assert result["is_positive_ev"] is True

    def test_custom_stake(self):
        result = calculate_ev(probability=0.55, american_odds=-110, stake=200)
        assert result["expected_value"] != 0  # just verify it runs


class TestFindBestLine:
    SAMPLE_GAME = {
        "id": "game1",
        "home_team": "Duke",
        "away_team": "UNC",
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": "2026-03-22T00:00:00Z",
                "markets": [
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Duke", "price": -108, "point": -3.5},
                            {"name": "UNC", "price": -112, "point": 3.5},
                        ],
                    }
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-03-22T00:00:00Z",
                "markets": [
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Duke", "price": -105, "point": -3.5},
                            {"name": "UNC", "price": -115, "point": 3.5},
                        ],
                    }
                ],
            },
        ],
    }

    def test_find_best_spread(self):
        result = find_best_line(self.SAMPLE_GAME, market="spreads", team="Duke")
        assert result["best"]["bookmaker"] == "DraftKings"  # -105 > -108
        assert result["best"]["price"] == -105
        assert result["worst"]["price"] == -108
        assert result["spread_across_books"] == 3

    def test_find_best_no_filter(self):
        result = find_best_line(self.SAMPLE_GAME, market="spreads")
        assert len(result["all_lines"]) == 4  # 2 teams × 2 books
        assert result["best"]["price"] == -105

    def test_no_market_found(self):
        result = find_best_line(self.SAMPLE_GAME, market="totals")
        assert "error" in result

    def test_empty_bookmakers(self):
        result = find_best_line({"bookmakers": []}, market="spreads")
        assert "error" in result


class TestDetectLineMovement:
    def test_detects_price_movement(self):
        old = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "dk",
                    "title": "DraftKings",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [{"name": "Team A", "price": -150}],
                    }],
                }],
            }],
        }
        new = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "dk",
                    "title": "DraftKings",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [{"name": "Team A", "price": -180}],
                    }],
                }],
            }],
        }
        movements = detect_line_movement(old, new)
        assert len(movements) == 1
        assert movements[0]["price_movement"] == -30
        assert movements[0]["direction"] == "unfavorable"

    def test_detects_point_movement(self):
        old = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "fd",
                    "title": "FanDuel",
                    "markets": [{
                        "key": "spreads",
                        "outcomes": [{"name": "Team A", "price": -110, "point": -3.5}],
                    }],
                }],
            }],
        }
        new = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "fd",
                    "title": "FanDuel",
                    "markets": [{
                        "key": "spreads",
                        "outcomes": [{"name": "Team A", "price": -110, "point": -5.0}],
                    }],
                }],
            }],
        }
        movements = detect_line_movement(old, new)
        assert len(movements) == 1
        assert movements[0]["point_movement"] == -1.5

    def test_no_movement_below_threshold(self):
        old = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "dk",
                    "title": "DraftKings",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [{"name": "Team A", "price": -110}],
                    }],
                }],
            }],
        }
        new = {
            "games": [{
                "id": "g1",
                "bookmakers": [{
                    "key": "dk",
                    "title": "DraftKings",
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [{"name": "Team A", "price": -112}],
                    }],
                }],
            }],
        }
        movements = detect_line_movement(old, new)
        assert len(movements) == 0

    def test_empty_snapshots(self):
        movements = detect_line_movement({}, {})
        assert movements == []

    def test_favorable_movement(self):
        old = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -200}]}]}]}]}
        new = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": -150}]}]}]}]}
        movements = detect_line_movement(old, new)
        assert len(movements) == 1
        assert movements[0]["direction"] == "favorable"
        assert movements[0]["price_movement"] == 50
