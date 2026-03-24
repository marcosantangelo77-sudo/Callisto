"""Tests for Monte Carlo simulation and Poisson model."""

import pytest
from tools.simulation import (
    simulate_basketball,
    simulate_poisson,
    compare_to_market,
    TeamProfile,
    _poisson_pmf,
)


class TestBasketballSimulation:
    def test_basic_simulation(self):
        home = TeamProfile(name="Team A", offensive_efficiency=110, defensive_efficiency=95, pace=70)
        away = TeamProfile(name="Team B", offensive_efficiency=105, defensive_efficiency=100, pace=68)
        result = simulate_basketball(home, away, iterations=5000)

        assert result.home_team == "Team A"
        assert result.away_team == "Team B"
        assert result.iterations == 5000
        assert 55 < result.home_avg_score < 100
        assert 55 < result.away_avg_score < 100
        # Ties are possible in simulation, so wins + ties ~ 1.0
        assert result.home_win_pct + result.away_win_pct > 0.85

    def test_better_team_wins_more(self):
        strong = TeamProfile(name="Strong", offensive_efficiency=115, defensive_efficiency=90, pace=72)
        weak = TeamProfile(name="Weak", offensive_efficiency=95, defensive_efficiency=110, pace=68)
        result = simulate_basketball(strong, weak, iterations=10000)

        assert result.home_win_pct > 0.55  # Strong team should win majority

    def test_home_advantage(self):
        team = TeamProfile(name="Neutral", offensive_efficiency=105, defensive_efficiency=100, pace=70)
        # Same team, home vs away
        result_home = simulate_basketball(team, team, is_home=True, iterations=10000)
        result_away = simulate_basketball(team, team, is_home=False, iterations=10000)

        # Home team should win more often with home advantage
        assert result_home.home_win_pct > result_away.home_win_pct

    def test_fair_spread_direction(self):
        favorite = TeamProfile(name="Fav", offensive_efficiency=112, defensive_efficiency=95, pace=70)
        underdog = TeamProfile(name="Dog", offensive_efficiency=100, defensive_efficiency=105, pace=68)
        result = simulate_basketball(favorite, underdog, iterations=10000)

        assert result.fair_spread > 0  # Home (favorite) should be favored

    def test_spread_cover_probs_decrease(self):
        home = TeamProfile(name="A", offensive_efficiency=110, defensive_efficiency=95, pace=70)
        away = TeamProfile(name="B", offensive_efficiency=105, defensive_efficiency=100, pace=68)
        result = simulate_basketball(home, away, iterations=5000)

        # Cover probability should decrease as spread gets HARDER (more positive)
        # Positive spread = home must win by more
        probs = [result.spread_cover_probs.get(s, 0) for s in [1.5, 3.5, 5.5]]
        assert probs[0] >= probs[1] >= probs[2]

    def test_injuries_reduce_scoring(self):
        healthy = TeamProfile(name="A", offensive_efficiency=110, defensive_efficiency=95, pace=70)
        injured = TeamProfile(name="A_inj", offensive_efficiency=110, defensive_efficiency=95, pace=70, injuries_impact=-5)
        opp = TeamProfile(name="B", offensive_efficiency=105, defensive_efficiency=100, pace=68)

        result_h = simulate_basketball(healthy, opp, iterations=10000)
        result_i = simulate_basketball(injured, opp, iterations=10000)

        assert result_i.home_avg_score < result_h.home_avg_score


class TestPoissonSimulation:
    def test_basic_poisson(self):
        result = simulate_poisson(1.5, 1.2)
        assert result["home_win"] > 0
        assert result["away_win"] > 0
        assert result["draw"] > 0
        assert abs(result["home_win"] + result["away_win"] + result["draw"] - 1.0) < 0.01

    def test_home_advantage(self):
        result = simulate_poisson(2.0, 0.8)
        assert result["home_win"] > result["away_win"]

    def test_fair_total(self):
        result = simulate_poisson(1.5, 1.3)
        assert abs(result["fair_total"] - 2.8) < 0.01

    def test_over_probs(self):
        result = simulate_poisson(1.5, 1.5)
        # Over 2.5 should have reasonable probability
        assert 0.3 < result["over_probs"].get(2.5, 0) < 0.7

    def test_top_scorelines(self):
        result = simulate_poisson(1.5, 1.2)
        assert len(result["top_scorelines"]) > 0
        # Most likely scores should sum to significant probability
        top_prob = sum(s["probability"] for s in result["top_scorelines"][:5])
        assert top_prob > 0.2


class TestPoissonPMF:
    def test_zero_goals(self):
        import math
        assert abs(_poisson_pmf(0, 1.5) - math.exp(-1.5)) < 0.001

    def test_probabilities_sum_to_one(self):
        total = sum(_poisson_pmf(k, 2.0) for k in range(20))
        assert abs(total - 1.0) < 0.001

    def test_zero_lambda(self):
        assert _poisson_pmf(0, 0) == 1.0
        assert _poisson_pmf(1, 0) == 0.0


class TestMarketComparison:
    def test_finds_edges(self):
        home = TeamProfile(name="Home", offensive_efficiency=115, defensive_efficiency=90, pace=72)
        away = TeamProfile(name="Away", offensive_efficiency=95, defensive_efficiency=110, pace=66)
        sim = simulate_basketball(home, away, iterations=10000)

        # Create market odds that disagree with simulation
        market = {
            "bookmakers": [{
                "key": "testbook",
                "title": "TestBook",
                "markets": [
                    {"key": "spreads", "outcomes": [
                        {"name": "Home", "price": -110, "point": -3.5},
                        {"name": "Away", "price": -110, "point": 3.5},
                    ]},
                    {"key": "h2h", "outcomes": [
                        {"name": "Home", "price": -150},
                        {"name": "Away", "price": 130},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -110, "point": 145.5},
                        {"name": "Under", "price": -110, "point": 145.5},
                    ]},
                ],
            }],
        }

        edges = compare_to_market(sim, market)
        # With a strong home team and -3.5 spread, there should be edges
        assert len(edges) > 0
        assert all("edge" in e for e in edges)

    def test_spread_probs_are_sane(self):
        """Verify spread cover probabilities map correctly to market conventions."""
        home = TeamProfile(name="Home", offensive_efficiency=110, defensive_efficiency=97, pace=70)
        away = TeamProfile(name="Away", offensive_efficiency=105, defensive_efficiency=100, pace=68)
        sim = simulate_basketball(home, away, iterations=20000)

        # Market: home -3.5
        market = {
            "bookmakers": [{
                "title": "TestBook",
                "markets": [{"key": "spreads", "outcomes": [
                    {"name": "Home", "price": -110, "point": -3.5},
                    {"name": "Away", "price": -110, "point": 3.5},
                ]}],
            }],
        }
        edges = compare_to_market(sim, market)
        spread_edges = [e for e in edges if e["market"] == "spreads"]
        # Model probabilities for home and away should sum to ~1.0
        if len(spread_edges) == 2:
            total = sum(e["model_probability"] for e in spread_edges)
            assert 0.95 < total < 1.05

        # Model probability for either side should be in reasonable range (20-80%)
        for e in spread_edges:
            assert 0.15 < e["model_probability"] < 0.85, (
                f"{e['team']} spread probability {e['model_probability']} is unreasonable"
            )

    def test_totals_half_point_lookup(self):
        """Verify half-point total lines resolve correctly."""
        home = TeamProfile(name="Home", offensive_efficiency=110, defensive_efficiency=95, pace=72)
        away = TeamProfile(name="Away", offensive_efficiency=105, defensive_efficiency=100, pace=68)
        sim = simulate_basketball(home, away, iterations=10000)

        fair_total = round(sim.fair_total)
        # Set market total well below fair — should find Over as +EV
        low_total = fair_total - 5.5
        market = {
            "bookmakers": [{
                "title": "TestBook",
                "markets": [{"key": "totals", "outcomes": [
                    {"name": "Over", "price": -110, "point": low_total},
                    {"name": "Under", "price": -110, "point": low_total},
                ]}],
            }],
        }
        edges = compare_to_market(sim, market)
        over_edges = [e for e in edges if e["market"] == "totals" and e["team"] == "Over"]
        # Should find Over as having positive edge since total is set well below fair
        assert len(over_edges) == 1
        assert over_edges[0]["edge"] > 0
