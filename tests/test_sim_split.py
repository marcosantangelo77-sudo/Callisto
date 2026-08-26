"""Tests for the tools.simulation -> tools.sim split.

Verifies that:
  1. The public API is still importable from tools.simulation.
  2. The implementations actually live in the tools.sim package.
  3. Behavior is unchanged (functional smoke tests).
"""

import importlib
import inspect

import numpy as np

import tools.simulation as sim_mod


class TestFacadeReExports:
    def test_public_api_importable_from_tools_simulation(self):
        names = [
            "DEFAULT_ITERATIONS",
            "HIGH_SCORING_SPORTS",
            "LOW_SCORING_SPORTS",
            "SPORT_DEFAULTS",
            "_classify_sport",
            "classify_sport",
            "TeamProfile",
            "SimulationResult",
            "PropSimResult",
            "EdgeResult",
            "simulate_game",
            "simulate_spread",
            "simulate_total",
            "simulate_prop",
            "compare_to_book",
            "compare_to_market",
            "compare_poisson_to_market",
            "_make_edge_result",
            "_poisson_pmf",
            "simulate_game_with_pace_env",
            "simulate_basketball",
            "simulate_poisson",
        ]
        for name in names:
            assert hasattr(sim_mod, name), f"missing re-export: {name}"

    def test_implementations_live_in_tools_sim_package(self):
        # simulate_game should be defined in a tools.sim submodule, not in
        # tools/simulation.py itself.
        assert sim_mod.simulate_game.__module__.startswith("tools.sim.")
        assert sim_mod.simulate_basketball.__module__.startswith("tools.sim.")
        assert sim_mod.compare_to_market.__module__.startswith("tools.sim.")
        assert not inspect.getsourcefile(sim_mod.simulate_game).endswith(
            "tools/simulation.py"
        )

    def test_submodules_exist(self):
        for sub in ("constants", "models", "game", "markets", "props", "edge",
                    "pace_env", "legacy"):
            mod = importlib.import_module(f"tools.sim.{sub}")
            assert mod is not None

    def test_private_aliases(self):
        from tools.sim.game import _poisson_pmf, _std_dev
        from tools.sim.edge import _make_edge_result
        assert callable(_make_edge_result)
        assert _poisson_pmf(0, 0.0) == 1.0
        assert abs(_poisson_pmf(1, 2.0) - 2.0 * np.exp(-2.0)) < 1e-12
        assert _std_dev([1, 1, 1]) == 0.0
        assert abs(_std_dev([1.0, 3.0]) - 1.4142135623730951) < 1e-9


class TestBehaviorUnchanged:
    def test_classify_sport(self):
        f = sim_mod._classify_sport
        assert f("basketball_nba") == "high_scoring"
        assert f("americanfootball_nfl") == "high_scoring"
        assert f("soccer_epl") == "low_scoring"
        assert f("icehockey_nhl") == "low_scoring"
        assert f("baseball_mlb") == "low_scoring"
        assert f("soccer_exotic_league") == "low_scoring"  # prefix match
        assert f("BASKETBALL_NBA") == "high_scoring"

    def test_simulate_game_high_scoring(self):
        res = sim_mod.simulate_game(
            home_power=115.0, away_power=110.0,
            sport="basketball_nba", n_sims=2000, home_advantage=3.0,
        )
        assert isinstance(res, sim_mod.SimulationResult)
        assert res.iterations == 2000
        assert len(res.home_scores) == 2000
        assert res.home_avg_score > res.away_avg_score
        assert res.fair_spread > 0
        assert res.spread_cover_probs and res.over_probs
        probs = res.home_win_pct + res.away_win_pct + res.draw_pct
        assert 0.98 <= probs <= 1.02

    def test_simulate_game_low_scoring_exact_scores(self):
        res = sim_mod.simulate_game(
            home_power=1.5, away_power=1.1,
            sport="soccer_epl", n_sims=2000,
        )
        assert res.exact_score_probs, "low-scoring sports must produce exact scores"
        top = max(res.exact_score_probs.items(), key=lambda kv: kv[1])[0]
        assert isinstance(top, str) and "-" in top

    def test_simulate_basketball_legacy(self):
        home = sim_mod.TeamProfile(name="A", offensive_efficiency=112,
                                   defensive_efficiency=96, pace=100)
        away = sim_mod.TeamProfile(name="B", offensive_efficiency=106,
                                   defensive_efficiency=102, pace=98)
        res = sim_mod.simulate_basketball(home, away, iterations=1500)
        assert res.home_team == "A"
        assert res.home_win_pct > res.away_win_pct
        assert len(res.home_scores) == 1500

    def test_simulate_poisson(self):
        out = sim_mod.simulate_poisson(1.5, 1.1, max_goals=6)
        total = out["home_win"] + out["draw"] + out["away_win"]
        assert abs(total - 1.0) < 0.01
        assert out["fair_total"] == 2.6

    def test_simulate_prop(self):
        res = sim_mod.simulate_prop(player_avg=25.0, n_sims=2000,
                                    player_name="Star", stat="points")
        assert isinstance(res, sim_mod.PropSimResult)
        assert 20 < res.mean < 30
        assert res.percentiles[50] <= res.percentiles[90]

    def test_compare_to_book_no_edge_on_fair_line(self):
        rng = np.random.default_rng(42)
        values = rng.normal(224.0, 12.0, size=5000)
        result = sim_mod.compare_to_book(values, book_line=230.5,
                                         book_odds=-110)
        assert isinstance(result, sim_mod.EdgeResult)
        assert result.rating in {"STRONG", "MODERATE", "THIN", "NO_EDGE"}
        lo, hi = result.confidence_interval
        assert 0.0 <= lo <= hi <= 1.0

    def test_compare_to_market_and_spread(self):
        home = sim_mod.TeamProfile(name="LAL", offensive_efficiency=115,
                                   defensive_efficiency=95, pace=100)
        away = sim_mod.TeamProfile(name="BOS", offensive_efficiency=105,
                                   defensive_efficiency=100, pace=100)
        sim_res = sim_mod.simulate_basketball(home, away, iterations=2000)
        market = {
            "bookmakers": [{
                "title": "TestBook",
                "markets": [
                    {"key": "spreads", "outcomes": [
                        {"name": "LAL", "price": -105, "point": -10.0},
                        {"name": "BOS", "price": -115, "point": 10.0},
                    ]},
                    {"key": "h2h", "outcomes": [
                        {"name": "LAL", "price": -300},
                        {"name": "BOS", "price": 250},
                    ]},
                ],
            }],
        }
        edges = sim_mod.compare_to_market(sim_res, market)
        assert isinstance(edges, list)
        for e in edges:
            assert e["bookmaker"] == "TestBook"
            assert "edge" in e and "assessment" in e

        spread_out = sim_mod.simulate_spread(market, sport="basketball",
                                             n_sims=1000,
                                             home_power=115.0,
                                             away_power=108.0)
        assert spread_out["sport"] == "basketball"
        assert len(spread_out["edges"]) == 2

    def test_compare_poisson_to_market(self):
        poisson = sim_mod.simulate_poisson(1.8, 1.0, max_goals=6)
        market = {
            "bookmakers": [{
                "title": "TB",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Home", "price": -150},
                        {"name": "Away", "price": 400},
                        {"name": "Draw", "price": 260},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": 100, "point": 4},
                        {"name": "Under", "price": -120, "point": 4},
                    ]},
                ],
            }],
        }
        edges = sim_mod.compare_poisson_to_market(poisson, market, "Home", "Away")
        assert isinstance(edges, list)

    def test_simulate_game_with_pace_env_falls_back(self):
        res = sim_mod.simulate_game_with_pace_env(
            home_power=114.0, away_power=109.0,
            sport="basketball_nba", n_sims=500, home_advantage=3.0,
        )
        assert isinstance(res, sim_mod.SimulationResult)
        meta = getattr(res, "_pace_env_meta", None)
        assert meta is not None
        assert meta["environment_adjustment"] == 0.0
        assert meta["pace_projection"] is None
