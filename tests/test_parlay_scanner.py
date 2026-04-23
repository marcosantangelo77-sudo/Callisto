"""Tests for parlay scanner — parlay math, correlation edges, overreaction detection."""

import pytest
from tools.parlay_scanner import (
    parlay_odds_from_legs,
    find_correlated_parlay_edges,
    analyze_prop_mispricing,
    analyze_live_overreaction,
)


class TestParlayOdds:
    def test_two_leg_parlay(self):
        legs = [
            {"american_odds": -110, "description": "Team A spread"},
            {"american_odds": -110, "description": "Team B spread"},
        ]
        result = parlay_odds_from_legs(legs)
        assert result["legs"] == 2
        # Two -110 legs: each implied ~52.4%, parlay ~27.4%
        assert 0.25 < result["book_implied_probability"] < 0.30

    def test_three_leg_parlay(self):
        legs = [
            {"american_odds": -110},
            {"american_odds": -110},
            {"american_odds": -110},
        ]
        result = parlay_odds_from_legs(legs)
        assert result["legs"] == 3
        # Three -110 legs: ~14.4% implied
        assert 0.12 < result["book_implied_probability"] < 0.16

    def test_parlay_with_true_probs(self):
        legs = [
            {"american_odds": -110, "true_probability": 0.55},
            {"american_odds": -110, "true_probability": 0.55},
        ]
        result = parlay_odds_from_legs(legs)
        # True prob (0.55 * 0.55 = 0.3025) > implied (0.524 * 0.524 = 0.274)
        assert result["edge"] > 0
        assert result["edge_pct"] > 0

    def test_negative_ev_parlay(self):
        legs = [
            {"american_odds": -110, "true_probability": 0.50},
            {"american_odds": -110, "true_probability": 0.50},
        ]
        result = parlay_odds_from_legs(legs)
        # True prob (0.25) < implied (0.274) = -EV
        assert result["edge"] < 0

    def test_empty_legs(self):
        result = parlay_odds_from_legs([])
        assert "error" in result

    def test_mixed_favorites_underdogs(self):
        legs = [
            {"american_odds": -200, "description": "Heavy favorite"},
            {"american_odds": +150, "description": "Underdog"},
        ]
        result = parlay_odds_from_legs(legs)
        # -200 implied ~66.7%, +150 implied ~40%
        # Parlay: ~26.7%
        assert 0.24 < result["book_implied_probability"] < 0.30
        assert result["parlay_american_odds"] > 0  # Should be plus odds


class TestCorrelatedParlayEdges:
    SAMPLE_GAME = {
        "id": "g1",
        "home_team": "Alabama",
        "away_team": "Texas Tech",
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Alabama", "price": -300},
                        {"name": "Texas Tech", "price": 250},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Alabama", "price": -110, "point": -7.5},
                        {"name": "Texas Tech", "price": -110, "point": 7.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -110, "point": 145.5},
                        {"name": "Under", "price": -110, "point": 145.5},
                    ]},
                ],
            },
        ],
    }

    SAMPLE_ALTERNATES = {
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {"key": "alternate_spreads", "outcomes": [
                        {"name": "Alabama", "price": -250, "point": -10.5},
                        {"name": "Alabama", "price": -180, "point": -9.5},
                        {"name": "Alabama", "price": 150, "point": -3.5},
                    ]},
                    {"key": "alternate_totals", "outcomes": [
                        {"name": "Over", "price": -200, "point": 140.5},
                        {"name": "Under", "price": 170, "point": 140.5},
                    ]},
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {"key": "alternate_spreads", "outcomes": [
                        {"name": "Alabama", "price": -220, "point": -10.5},
                        {"name": "Alabama", "price": 180, "point": -3.5},
                    ]},
                ],
            },
        ],
    }

    def test_finds_spread_over_correlation(self):
        edges = find_correlated_parlay_edges(self.SAMPLE_GAME, self.SAMPLE_ALTERNATES)
        spread_over = [e for e in edges if e["type"] == "SPREAD_OVER_CORRELATION"]
        # Alabama -7.5 spread + Over should be correlated
        assert len(spread_over) >= 1

    def test_finds_alt_spread_cross_book(self):
        edges = find_correlated_parlay_edges(self.SAMPLE_GAME, self.SAMPLE_ALTERNATES)
        cross_book = [e for e in edges if e["type"] == "ALT_SPREAD_CROSS_BOOK"]
        # Alabama -10.5 differs by 30+ between FanDuel (-250) and DraftKings (-220)
        assert len(cross_book) >= 1


class TestLiveOverreaction:
    def test_detects_large_movement(self):
        pre = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": -110}]}
        ]}]}]}
        live = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": 148}]}
        ]}]}]}
        result = analyze_live_overreaction(pre, live)
        assert len(result) == 1
        assert result[0]["price_movement"] == 258
        assert any(s in result[0]["assessment"] for s in ("EXTREME", "LARGE"))

    def test_no_overreaction_small_movement(self):
        pre = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": -110}]}
        ]}]}]}
        live = {"games": [{"id": "g1", "bookmakers": [{"key": "dk", "title": "DK", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": -120}]}
        ]}]}]}
        result = analyze_live_overreaction(pre, live)
        assert len(result) == 0


class TestPropMispricing:
    def test_cross_book_prop_edge(self):
        props = {
            "players": {
                "John Smith": [
                    {"bookmaker": "FanDuel", "market": "player_points", "name": "Over", "price": -110, "point": 16.5},
                    {"bookmaker": "DraftKings", "market": "player_points", "name": "Over", "price": -130, "point": 16.5},
                ],
            },
        }
        edges = analyze_prop_mispricing(props)
        assert len(edges) == 1
        assert edges[0]["player"] == "John Smith"
        assert edges[0]["price_spread"] == 20

    def test_role_change_context(self):
        props = {
            "players": {
                "Bench Guy": [
                    {"bookmaker": "FanDuel", "market": "player_points", "name": "Over", "price": -110, "point": 12.5},
                    {"bookmaker": "DraftKings", "market": "player_points", "name": "Over", "price": -125, "point": 12.5},
                ],
            },
        }
        context = {
            "starter_out": "Star Player",
            "replacement_player": "Bench Guy",
            "replacement_avg_stats": {"points": 8},
            "replacement_starter_stats": {"points": 16},
        }
        edges = analyze_prop_mispricing(props, context=context)
        assert len(edges) == 1
        assert "contextual_edge" in edges[0]
        assert edges[0]["contextual_edge"]["usage_bump"] == 8.0
        assert "underpriced" in edges[0]["contextual_edge"]["assessment"]
