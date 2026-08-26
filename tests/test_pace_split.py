"""Tests for the tools/pace split of the former monolithic pace_model module."""

import math

import pytest

import tools.pace_model as facade
from tools.pace import (
    LEAGUE_DEFAULTS,
    Sport,
    TotalProjection,
    analyze_game_total,
    detect_total_edge,
    matchup_efficiency,
    player_pace_adjustment,
    poisson_pmf,
    poisson_total_distribution,
    project_game_total,
    project_pace,
    simulate_total_distribution,
)


def test_facade_reexports_public_names():
    """The legacy facade exposes the same public surface as the package."""
    for name in [
        "Sport",
        "LEAGUE_DEFAULTS",
        "PACE_INTERACTION_COEFF",
        "PaceProjection",
        "TotalProjection",
        "PlayerPaceImpact",
        "TotalEdge",
        "project_pace",
        "matchup_efficiency",
        "project_game_total",
        "player_pace_adjustment",
        "poisson_pmf",
        "poisson_total_distribution",
        "detect_total_edge",
        "analyze_game_total",
        "simulate_total_distribution",
    ]:
        assert hasattr(facade, name), f"facade missing {name}"
        assert getattr(facade, name) is not None


def test_sport_enum_and_defaults():
    assert Sport.NBA == "nba"
    assert set(LEAGUE_DEFAULTS) == {s for s in Sport}
    assert LEAGUE_DEFAULTS[Sport.NBA]["pace"] == 100.0


def test_project_pace_fast_fast_amplifies():
    fast = project_pace(105.0, 105.0, 100.0, "nba")
    slow = project_pace(95.0, 95.0, 100.0, "nba")
    # interaction is positive when both deltas share sign
    assert fast.interaction_term > 0
    assert slow.interaction_term > 0
    assert fast.pace_factor > 1.0
    assert slow.pace_factor < 1.0
    lo, hi = fast.confidence_interval
    assert lo < fast.projected_possessions < hi


def test_matchup_efficiency_by_sport():
    nba = matchup_efficiency(115.0, 120.0, 112.0, "nba")
    assert nba == pytest.approx(115 * (120 / 112), abs=0.05)
    mlb = matchup_efficiency(4.5, 4.0, 4.5, "mlb")
    assert mlb == pytest.approx(math.sqrt(4.5 * 4.0), abs=0.01)
    # zero league average falls back to raw offense
    assert matchup_efficiency(115.0, 120.0, 0.0, "nba") == 115.0


@pytest.mark.parametrize(
    "sport,expected_type",
    [
        ("nba", TotalProjection),
        ("nfl", TotalProjection),
        ("mlb", TotalProjection),
        ("nhl", TotalProjection),
        ("soccer", TotalProjection),
    ],
)
def test_project_game_total_all_sports(sport, expected_type):
    result = project_game_total(
        home_pace=100.0,
        away_pace=98.0,
        home_off_eff=112.0,
        away_off_eff=110.0,
        home_def_eff=110.0,
        away_def_eff=113.0,
        league_avg_pace=100.0,
        sport=sport,
    )
    assert isinstance(result, expected_type)
    assert result.projected_total > 0
    assert result.home_projected > 0 and result.away_projected > 0
    lo, hi = result.confidence_interval
    assert lo < result.projected_total < hi
    assert result.methodology


def test_project_game_total_home_advantage():
    proj = project_game_total(
        100.0, 100.0, 112.0, 112.0, 112.0, 112.0, 100.0, "nba"
    )
    assert proj.home_projected > proj.away_projected


def test_player_pace_adjustment():
    impact = player_pace_adjustment(
        player_pace_on=104.0,
        player_pace_off=98.0,
        projected_minutes=36.0,
        team_total_minutes=240.0,
        sport="nba",
    )
    assert impact.minutes_fraction == pytest.approx(0.15, abs=1e-3)
    expected_delta = 6.0 * 36.0 / 240.0
    assert impact.pace_delta == pytest.approx(expected_delta, abs=0.01)
    assert impact.projected_total_delta > 0


def test_poisson_helpers():
    assert poisson_pmf(0, 0.0) == 1.0
    assert poisson_pmf(1, 0.0) == 0.0
    assert poisson_pmf(2, 2.0) == pytest.approx(math.exp(-2.0) * 2.0, rel=1e-9)
    dist = poisson_total_distribution(2.0, 2.0, max_score=8)
    total_prob = sum(p["over_probs"].get(3.5, 0) for p in [dist])
    assert dist["total_mean"] == 4.0
    assert 0.0 <= dist["over_probs"][3.5] <= 1.0
    assert dist["under_probs"][3.5] == pytest.approx(1.0 - dist["over_probs"][3.5], abs=1e-4)


def test_detect_total_edge_gaussian():
    edge = detect_total_edge(
        projected_total=228.0,
        book_total=224.5,
        book_over_odds=-110,
        book_under_odds=-110,
        sport="nba",
        projection_std=11.0,
    )
    assert edge.edge_direction in ("over", "under")
    # projection well above line: over should be favored
    assert edge.over_probability > edge.under_probability
    assert edge.recommended_side == edge.edge_direction
    assert 0.0 <= edge.kelly_fraction <= 0.25


def test_detect_total_edge_poisson():
    edge = detect_total_edge(
        projected_total=7.0,
        book_total=6.5,
        book_over_odds=-110,
        book_under_odds=-110,
        sport="nhl",
        home_expected=3.6,
        away_expected=3.4,
    )
    assert edge.over_probability > 0.5


def test_analyze_game_total_with_injury_and_edge():
    result = analyze_game_total(
        home_pace=102.0,
        away_pace=100.0,
        home_off_eff=114.0,
        away_off_eff=111.0,
        home_def_eff=110.0,
        away_def_eff=112.0,
        league_avg_pace=100.0,
        sport="nba",
        book_total=226.5,
        book_over_odds=-110,
        book_under_odds=-110,
        player_adjustments=[
            {
                "player_pace_on": 106.0,
                "player_pace_off": 99.0,
                "projected_minutes": 0,
                "usual_minutes": 32,
                "is_playing": False,
                "team": "home",
            }
        ],
    )
    assert result["sport"] == "nba"
    assert result["adjusted_total"] < result["projection"].projected_total
    assert len(result["player_impacts"]) == 1
    assert result["edge"] is not None


def test_simulate_total_distribution_nba():
    out = simulate_total_distribution(
        101.0, 99.5, 113.0, 111.0, 110.0, 112.0, 100.0, "nba", iterations=2000
    )
    assert out["iterations"] == 2000
    assert 150.0 < out["mean_total"] < 300.0
    assert out["std_total"] > 0
    assert set(out["percentiles"]) == {"p5", "p10", "p25", "p50", "p75", "p90", "p95"}
    assert out["percentiles"]["p50"] <= out["percentiles"]["p95"]


def test_simulate_total_distribution_poisson_sport():
    out = simulate_total_distribution(
        12.0, 12.0, 1.40, 1.30, 1.30, 1.45, 12.0, "soccer", iterations=2000
    )
    assert 0.5 < out["mean_total"] < 10.0
