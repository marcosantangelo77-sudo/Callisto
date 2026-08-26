"""Tests for the tools.injury split of tools/injury_model.py.

Verifies:
- the package modules exist and import cleanly
- every historical public name is still importable from tools.injury_model
- the split modules and the compat shim share identity for constants/classes
- core model behavior is preserved (impact, redistribution, matchup, market)
"""

import importlib

import pytest


# ---------------------------------------------------------------------------
# Module structure
# ---------------------------------------------------------------------------

def test_package_modules_exist():
    data = importlib.import_module("tools.injury.data")
    model = importlib.import_module("tools.injury.model")
    pkg = importlib.import_module("tools.injury")
    assert data is not None and model is not None and pkg is not None
    # Data module holds only constants — no impact functions leaked there.
    assert not hasattr(data, "player_impact")


def test_constants_live_in_data_module():
    from tools.injury import data
    assert hasattr(data, "NBA_POSITION_IMPACT")
    assert hasattr(data, "NFL_TARGET_REDISTRIBUTION")
    assert hasattr(data, "MARKET_ADJUSTMENT_CURVE")
    assert hasattr(data, "SIGNIFICANCE_TIERS")


# ---------------------------------------------------------------------------
# Public API stability via the compat shim
# ---------------------------------------------------------------------------

PUBLIC_NAMES = [
    # constants
    "NBA_POSITION_IMPACT",
    "NBA_TIER_THRESHOLDS",
    "NFL_POSITION_IMPACT",
    "NFL_TARGET_REDISTRIBUTION",
    "MLB_POSITION_IMPACT_CENTS",
    "MLB_PITCHER_TIERS",
    "MLB_POSITION_TIERS",
    "NBA_MATCHUP_MODIFIERS",
    "NFL_MATCHUP_MODIFIERS",
    "MARKET_ADJUSTMENT_CURVE",
    "SIGNIFICANCE_TIERS",
    # dataclasses
    "PlayerImpactResult",
    "UsageRedistribution",
    "MatchupAdjustedImpact",
    "MarketAdjustmentEstimate",
    # functions
    "player_impact",
    "redistribute_usage",
    "matchup_adjusted_impact",
    "estimate_market_adjustment",
    "full_injury_analysis",
    "lookup_position_impact",
]


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_public_names_stable(name):
    shim = importlib.import_module("tools.injury_model")
    assert hasattr(shim, name)


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_shim_reexports_package_identity(name):
    shim = importlib.import_module("tools.injury_model")
    pkg = importlib.import_module("tools.injury")
    assert getattr(shim, name) is getattr(pkg, name)


def test_internal_importers_still_work():
    mod = importlib.import_module("tools.autonomous")
    assert callable(mod.full_injury_analysis)
    phases = importlib.import_module("tools.loop.phases_impl")
    assert phases is not None


# ---------------------------------------------------------------------------
# Behavior preservation
# ---------------------------------------------------------------------------

class TestPlayerImpact:
    def test_nba_star_out_moves_spread_4_to_5(self):
        from tools.injury_model import player_impact
        r = player_impact("Star", "LAL", "NBA", position="PG", ppg=28, bpm=8)
        assert 4.0 <= r.spread_impact <= 5.5
        assert r.tier == "mvp_candidate"
        assert r.sport == "NBA"

    def test_nba_bench_player_small_impact(self):
        from tools.injury_model import player_impact
        r = player_impact("Bench Guy", "SAS", "NBA", position="SG", ppg=3, bpm=-4)
        assert r.spread_impact < 1.0

    def test_nfl_qb_out_is_largest(self):
        from tools.injury_model import player_impact
        qb = player_impact("QB1", "KC", "NFL", position="QB")
        rb = player_impact("RB1", "KC", "NFL", position="RB")
        assert qb.spread_impact >= 3.0
        assert rb.spread_impact < 2.0
        assert qb.tier == "franchise_qb"

    def test_mlb_ace_pitcher_cents(self):
        from tools.injury_model import player_impact
        r = player_impact("Ace", "NYY", "MLB", position="SP", era=2.50)
        assert r.marginal_value_over_replacement > 30
        assert r.sport == "MLB"

    def test_unsupported_sport_returns_low_confidence(self):
        from tools.injury_model import player_impact
        r = player_impact("Who", "X", "NHL")
        assert r.confidence <= 0.2
        assert r.spread_impact == 0.0

    def test_result_type(self):
        from tools.injury_model import PlayerImpactResult, player_impact
        r = player_impact("P", "T", "NBA")
        assert isinstance(r, PlayerImpactResult)

    def test_teammate_prop_redistribution(self):
        from tools.injury_model import player_impact
        teammates = [
            {"name": "A", "ppg": 20, "usage_rate": 28},
            {"name": "B", "ppg": 12, "usage_rate": 18},
            {"name": "C", "ppg": 8, "usage_rate": 14},
        ]
        r = player_impact(
            "Star", "LAL", "NBA", position="SF", ppg=26, bpm=5,
            teammates=teammates,
        )
        assert set(r.prop_redistribution) == {"A", "B", "C"}
        # Higher usage teammate absorbs more
        a_gain = r.prop_redistribution["A"]["projected_ppg_increase"]
        c_gain = r.prop_redistribution["C"]["projected_ppg_increase"]
        assert a_gain > c_gain


class TestRedistributeUsage:
    def test_nba_sorted_desc_and_skips_absent(self):
        from tools.injury_model import redistribute_usage
        roster = [
            {"name": "Out Guy", "ppg": 20, "usage_rate": 30},
            {"name": "Alpha", "ppg": 22, "usage_rate": 27},
            {"name": "Beta", "ppg": 10, "usage_rate": 15},
        ]
        results = redistribute_usage("Out Guy", roster, "NBA")
        names = [r.player for r in results]
        assert "Out Guy" not in names
        increases = [r.usage_increase for r in results]
        assert increases == sorted(increases, reverse=True)
        assert all(isinstance(r.usage_increase, float) for r in results)

    def test_nfl_wr1_pattern(self):
        from tools.injury_model import redistribute_usage
        roster = [
            {"name": "WR2 Guy", "role": "WR2", "targets_per_game": 6},
            {"name": "TE1 Guy", "role": "TE1", "targets_per_game": 5},
            {"name": "RB1 Guy", "role": "RB1", "targets_per_game": 3},
        ]
        stats = {"role": "WR1", "targets_per_game": 10}
        results = redistribute_usage("WR1 Guy", roster, "NFL", stats)
        assert results[0].player == "WR2 Guy"
        top = results[0].projected_stat_change
        assert top["target_increase"] == pytest.approx(10 * 0.35, abs=0.05)

    def test_unsupported_sport_empty(self):
        from tools.injury_model import redistribute_usage
        assert redistribute_usage("X", [{"name": "Y"}], "NHL") == []


class TestMatchupAdjustment:
    def test_rim_protector_vs_interior_amplified(self):
        from tools.injury_model import matchup_adjusted_impact
        r = matchup_adjusted_impact(
            "Big Man", "DEN", "NBA",
            player_archetype="rim_protector",
            opponent_style="interior_dominant",
            base_impact=4.0,
        )
        assert r.matchup_multiplier == pytest.approx(1.35)
        assert r.adjusted_spread_impact == pytest.approx(5.40)
        assert any("amplified" in s for s in r.reasoning)

    def test_balanced_default_multiplier(self):
        from tools.injury_model import matchup_adjusted_impact
        r = matchup_adjusted_impact(
            "Guy", "ANY", "NBA",
            player_archetype="scorer", opponent_style="balanced",
            base_impact=3.0,
        )
        assert r.matchup_multiplier == 1.0
        assert r.adjusted_spread_impact == pytest.approx(3.0)

    def test_mlb_weak_lineup_mitigated(self):
        from tools.injury_model import matchup_adjusted_impact
        r = matchup_adjusted_impact(
            "SP", "PIT", "MLB",
            opponent_style="vs_weak_lineup", base_impact=40.0,
        )
        assert r.matchup_multiplier < 1.0

    def test_result_type(self):
        from tools.injury_model import MatchupAdjustedImpact, matchup_adjusted_impact
        r = matchup_adjusted_impact("A", "B", "NBA", base_impact=2.0)
        assert isinstance(r, MatchupAdjustedImpact)


class TestMarketAdjustment:
    def test_star_fast_role_slow(self):
        from tools.injury_model import estimate_market_adjustment
        star = estimate_market_adjustment(10, "NBA", player_tier="star")
        role = estimate_market_adjustment(10, "NBA", player_tier="role_player")
        assert star.pct_adjusted > role.pct_adjusted

    def test_monotonic_in_time(self):
        from tools.injury_model import estimate_market_adjustment
        early = estimate_market_adjustment(2, "NFL", position="QB").pct_adjusted
        late = estimate_market_adjustment(60, "NFL", position="QB").pct_adjusted
        assert late > early

    def test_fully_adjusted_no_edge(self):
        from tools.injury_model import estimate_market_adjustment
        r = estimate_market_adjustment(600, "NBA", spread_impact=5.0)
        assert r.pct_adjusted == pytest.approx(1.0, abs=0.01)
        assert r.edge_remaining == pytest.approx(0.0, abs=0.05)

    def test_edge_remaining_scales_with_spread(self):
        from tools.injury_model import estimate_market_adjustment
        r = estimate_market_adjustment(0, "NBA", player_tier="star",
                                       spread_impact=4.0)
        assert 0 < r.edge_remaining <= 4.0

    def test_result_type_and_tier(self):
        from tools.injury_model import MarketAdjustmentEstimate, estimate_market_adjustment
        r = estimate_market_adjustment(5, "NFL", position="QB")
        assert isinstance(r, MarketAdjustmentEstimate)
        assert r.significance_tier == "star"


class TestFullAnalysis:
    def test_pipeline_shape(self):
        from tools.injury_model import full_injury_analysis
        summary = full_injury_analysis(
            "Star", "BOS", "NBA", "DEN", position="C", ppg=25, bpm=6,
            minutes_since_announced=5,
        )
        assert set(summary) >= {
            "player", "team", "sport", "opponent", "impact",
            "redistribution", "matchup_adjusted", "market_timing",
            "actionable", "edge_points",
        }
        assert summary["player"] == "Star"
        assert summary["edge_points"] == summary["market_timing"].edge_remaining

    def test_actionable_when_news_fresh(self):
        from tools.injury_model import full_injury_analysis
        fresh = full_injury_analysis("S", "T", "NBA", "O", position="PG",
                                     ppg=27, bpm=7, minutes_since_announced=1)
        stale = full_injury_analysis("S", "T", "NBA", "O", position="PG",
                                     ppg=27, bpm=7, minutes_since_announced=300)
        assert fresh["actionable"] is True
        assert stale["actionable"] is False


class TestLookupPositionImpact:
    def test_nba(self):
        from tools.injury_model import lookup_position_impact
        v = lookup_position_impact("NBA", "C")
        assert v["unit"] == "spread points"
        assert v["bench"] < v["mvp_candidate"]

    def test_nfl(self):
        from tools.injury_model import lookup_position_impact
        v = lookup_position_impact("NFL", "QB")
        assert v["low_quality_backup"] > v["high_quality_backup"]

    def test_mlb(self):
        from tools.injury_model import lookup_position_impact
        v = lookup_position_impact("MLB", "SP")
        assert v["unit"] == "moneyline cents"
        assert v["star"] > v["replacement"]

    def test_unknown_returns_error_dict(self):
        from tools.injury_model import lookup_position_impact
        assert "error" in lookup_position_impact("NBA", "XX")
        assert "error" in lookup_position_impact("NHL", "C")
