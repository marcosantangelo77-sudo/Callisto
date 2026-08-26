"""Tests for the tools.corr split of tools/correlation.py.

Verifies that:
- the facade re-exports the full public API and it behaves identically
- each extracted submodule works standalone
- correlation math (joint probability, parlay odds, mispricing) is correct
"""

import math

import pytest

import tools.correlation as corr_facade
from tools.corr import assessment, lookup, matrices, odds, parlays


# ---------------------------------------------------------------------------
# Facade re-export completeness
# ---------------------------------------------------------------------------

PUBLIC_API = [
    "MARKET_ALIASES",
    "MLB_CORRELATIONS",
    "NBA_CORRELATIONS",
    "NFL_CORRELATIONS",
    "NHL_CORRELATIONS",
    "SPORT_CORRELATIONS",
    "_normalize_market",
    "get_all_correlations",
    "get_correlation",
    "get_learned_store",
    "list_correlated_markets",
    "set_learned_store",
    "_adjust_joint_probability",
    "_american_to_implied",
    "_implied_to_american",
    "_prob_to_decimal",
    "build_correlated_parlay",
    "correlated_parlay_odds",
    "detect_mispriced_correlation",
    "independent_parlay_odds",
    "_assess_mispricing",
    "_rate_correlation_edge",
    "detect_anti_correlation",
    "estimate_sgp_vig",
]


@pytest.mark.parametrize("name", PUBLIC_API)
def test_facade_reexports_every_public_name(name):
    assert hasattr(corr_facade, name), f"facade missing {name}"
    assert name in corr_facade.__all__


def test_facade_names_are_submodule_objects():
    assert corr_facade.get_correlation is lookup.get_correlation
    assert corr_facade.SPORT_CORRELATIONS is matrices.SPORT_CORRELATIONS
    assert corr_facade.correlated_parlay_odds is parlays.correlated_parlay_odds
    assert corr_facade.estimate_sgp_vig is assessment.estimate_sgp_vig


def test_matrices_content_preserved():
    # Spot-check known priors survived the move verbatim.
    assert corr_facade.NFL_CORRELATIONS[("qb_passing_yards", "team_total")] == 0.65
    assert corr_facade.NBA_CORRELATIONS[("player_pra", "player_points")] == 0.85
    assert corr_facade.MLB_CORRELATIONS[("f5_ml", "team_ml")] == 0.80
    assert corr_facade.NHL_CORRELATIONS[("goalie_saves", "opposing_shots_on_goal")] == 0.85
    assert len(corr_facade.MARKET_ALIASES) >= 60


# ---------------------------------------------------------------------------
# Market normalization / lookup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Passing Yards", "qb_passing_yards"),
        ("pass-yards", "qb_passing_yards"),
        ("points", "player_points"),
        ("pra", "player_pra"),
        ("moneyline", "team_ml"),
        ("QB_PASSING_YARDS", "qb_passing_yards"),  # already canonical
        ("some_unknown_market", "some_unknown_market"),
    ],
)
def test_normalize_market(raw, canonical):
    assert lookup._normalize_market(raw) == canonical


@pytest.mark.parametrize(
    "a,b,sport,expected",
    [
        ("qb_passing_yards", "team_total", "nfl", 0.65),
        ("passing_yards", "team_total", "nfl", 0.65),  # alias resolves
        ("team_total", "qb_passing_yards", "nfl", 0.65),  # order-independent
        ("americanfootball_qb_passing_yards", "team_total", "americanfootball_nfl", 0.0),  # prefixed markets not aliased
        ("player_points", "team_total", "basketball_nba", 0.50),  # API prefix on sport only
        ("player_points", "team_total", "nba", 0.50),
        ("unknown_market_a", "unknown_market_b", "nfl", 0.0),
        ("anything", "whatever", "nascar", 0.0),
    ],
)
def test_get_correlation(a, b, sport, expected):
    assert corr_facade.get_correlation(a, b, sport) == expected


def test_sport_registry_aliases_share_matrices():
    assert corr_facade.SPORT_CORRELATIONS["ncaaf"] is corr_facade.NFL_CORRELATIONS
    assert corr_facade.SPORT_CORRELATIONS["wnba"] is corr_facade.NBA_CORRELATIONS
    for sport in ("nfl", "ncaaf", "nba", "ncaab", "wnba", "mlb", "nhl"):
        assert sport in corr_facade.SPORT_CORRELATIONS


def test_get_all_correlations_returns_copy():
    matrix = corr_facade.get_all_correlations("nhl")
    assert matrix == corr_facade.NHL_CORRELATIONS
    assert matrix is not corr_facade.NHL_CORRELATIONS


def test_list_correlated_markets_sorted_and_filtered():
    results = corr_facade.list_correlated_markets("qb_passing_yards", "nfl", min_abs_rho=0.2)
    assert results, "expected correlated markets for qb_passing_yards"
    abss = [abs(r["correlation"]) for r in results]
    assert abss == sorted(abss, reverse=True)
    assert all(a >= 0.2 for a in abss)
    targets = {r["market"] for r in results}
    assert "qb_completions" in targets  # rho = 0.78


# ---------------------------------------------------------------------------
# Learned store wiring
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.calls = []

    def get_blended(self, sport, market_a, market_b, prior):
        self.calls.append((sport, market_a, market_b, prior))
        return prior + 0.5


def test_set_and_get_learned_store_roundtrip():
    sentinel = object()
    try:
        corr_facade.set_learned_store(sentinel)
        assert corr_facade.get_learned_store() is sentinel
    finally:
        corr_facade.set_learned_store(None)
    assert corr_facade.get_learned_store() is None


def test_get_correlation_blends_via_store():
    store = _FakeStore()
    try:
        corr_facade.set_learned_store(store)
        value = corr_facade.get_correlation("qb_passing_yards", "team_total", "nfl")
        assert value == pytest.approx(1.15)
        assert store.calls[-1][:3] == ("nfl", "qb_passing_yards", "team_total")
    finally:
        corr_facade.set_learned_store(None)


def test_explicit_store_parameter_overrides_singleton():
    explicit = _FakeStore()
    value = corr_facade.get_correlation("qb_passing_yards", "team_total", "nfl", learned_store=explicit)
    assert value == pytest.approx(1.15)
    assert corr_facade.get_learned_store() is None  # singleton untouched


# ---------------------------------------------------------------------------
# Odds conversion + joint probability math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "odds,prob",
    [(-110, None), (+150, None), (-200, None), (+100, None)],
)
def test_american_to_implied_matches_formula(odds, prob):
    p = odds_module()._american_to_implied(odds)
    if odds < 0:
        assert p == pytest.approx(abs(odds) / (abs(odds) + 100))
    else:
        assert p == pytest.approx(100 / (odds + 100))


def odds_module():
    return odds


@pytest.mark.parametrize(
    "prob,expected",
    [(0.524, -110), (0.4, 150), (0.667, -200), (0.5, -100)],
)
def test_implied_to_american(prob, expected):
    got = odds._implied_to_american(prob)
    assert abs(got - expected) <= 2  # int truncation tolerance


@pytest.mark.parametrize("bad_prob", [0.0, 1.0, -0.1, 1.5])
def test_implied_to_american_invalid_returns_zero(bad_prob):
    assert odds._implied_to_american(bad_prob) == 0


def test_prob_to_decimal():
    assert odds._prob_to_decimal(0.25) == pytest.approx(4.0)
    assert odds._prob_to_decimal(0.0) == float("inf")


def test_adjust_joint_probability_independent():
    # rho = 0 -> plain product
    assert odds._adjust_joint_probability(0.6, 0.5, 0.0) == pytest.approx(0.3)


def test_adjust_joint_probability_positive_rho_raises_joint():
    base = odds._adjust_joint_probability(0.6, 0.5, 0.0)
    adj = odds._adjust_joint_probability(0.6, 0.5, 0.8)
    assert adj > base


def test_adjust_joint_probability_clamped_to_frechet_hoeffding():
    # Extreme positive rho must not exceed min(p_a, p_b)
    assert odds._adjust_joint_probability(0.9, 0.9, 1.0) <= 0.9
    # Extreme negative rho must not go below max(0, pa+pb-1)
    val = odds._adjust_joint_probability(0.9, 0.9, -1.0)
    assert val >= 0.8 - 1e-12


def test_adjust_joint_degenerate_probabilities():
    assert odds._adjust_joint_probability(0.0, 0.5, 0.9) == 0.0
    assert odds._adjust_joint_probability(1.0, 0.5, -0.9) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Parlay pricing
# ---------------------------------------------------------------------------

LEGS = [
    {"market": "qb_passing_yards", "american_odds": -110},
    {"market": "team_total", "american_odds": -110},
]


def test_independent_parlay_odds_empty():
    assert corr_facade.independent_parlay_odds([]) == 0


def test_independent_parlay_two_standard_legs():
    # two -110 legs: joint prob ~ 0.5235^2 = 0.274 -> American odds around +265
    result = corr_facade.independent_parlay_odds(LEGS)
    joint = 0.5235**2
    expected = odds._implied_to_american(joint)
    assert result == expected
    assert result > 0


def test_correlated_exceeds_independent_for_positive_rho():
    indep = corr_facade.independent_parlay_odds(LEGS)
    corr = corr_facade.correlated_parlay_odds(LEGS, sport="nfl")
    assert corr < indep  # higher probability -> lower (more negative) American odds


def test_correlated_with_zero_matrix_equals_independent():
    indep = corr_facade.independent_parlay_odds(LEGS)
    zero_matrix = {("qb_passing_yards", "team_total"): 0.0}
    corr = corr_facade.correlated_parlay_odds(LEGS, correlations=zero_matrix, sport="nfl")
    assert corr == indep


def test_correlated_custom_matrix_used():
    big = {(("qb_passing_yards", "team_total")): 1.0}
    corr = corr_facade.correlated_parlay_odds(LEGS, correlations={("qb_passing_yards", "team_total"): 1.0}, sport="nfl")
    indep = corr_facade.independent_parlay_odds(LEGS)
    assert corr < indep
    assert big[(("qb_passing_yards", "team_total"))] == 1.0  # sanity on test data


def test_correlated_alias_normalization():
    # "pass_yards" -> qb_passing_yards, "team_over_under" -> team_total
    legs = [{"market": "pass_yards", "american_odds": -110}, {"market": "team_over_under", "american_odds": -110}]
    via_alias = corr_facade.correlated_parlay_odds(legs, sport="nfl")
    canonical = corr_facade.correlated_parlay_odds(LEGS, sport="nfl")
    assert via_alias == canonical


# ---------------------------------------------------------------------------
# Mispricing detection
# ---------------------------------------------------------------------------

def test_detect_mispriced_needs_two_legs():
    assert "error" in corr_facade.detect_mispriced_correlation([], 100, "nfl")
    assert "error" in corr_facade.detect_mispriced_correlation([LEGS[0]], 100, "nfl")


def _sgp_legs():
    return [
        {"market": "qb_passing_yards", "american_odds": -110, "description": "Mahomes 300+ pass yds"},
        {"market": "team_total", "american_odds": -110, "description": "KC team total over"},
    ]


def test_detect_mispriced_positive_ev_when_book_prices_independently():
    indep_odds = corr_facade.independent_parlay_odds(_sgp_legs())
    result = corr_facade.detect_mispriced_correlation(_sgp_legs(), indep_odds, "nfl")
    assert "error" not in result
    assert result["is_positive_ev"] is True
    assert result["mispricing_pct"] > 0
    assert result["true_joint_prob"] > result["book_implied_joint_prob"]
    assert result["fair_odds"] < indep_odds  # better probability -> shorter fair price


def test_detect_mispriced_no_edge_when_book_prices_fair():
    fair_odds = corr_facade.correlated_parlay_odds(_sgp_legs(), sport="nfl")
    result = corr_facade.detect_mispriced_correlation(_sgp_legs(), fair_odds, "nfl")
    assert result["is_positive_ev"] is False
    assert result["mispricing_pct"] == pytest.approx(0.0, abs=0.05)


def test_detect_mispriced_report_structure():
    result = corr_facade.detect_mispriced_correlation(_sgp_legs(), +250, "nfl")
    assert set(result) >= {
        "true_correlation", "book_assumed_correlation", "edge_from_correlation",
        "mispricing_pct", "is_positive_ev", "independent_joint_prob",
        "book_implied_joint_prob", "true_joint_prob", "independent_odds",
        "book_offered_odds", "fair_odds", "leg_pair_correlations",
        "anti_correlation_warning", "assessment",
    }
    pair = result["leg_pair_correlations"][0]
    assert pair["correlation"] == pytest.approx(0.65)
    assert pair["direction"] == "positive"


# ---------------------------------------------------------------------------
# Parlay building
# ---------------------------------------------------------------------------

PROPS = [
    {"market": "qb_passing_yards", "american_odds": -110, "description": "300+ pass yards", "player": "QB1"},
    {"market": "wr_receiving_yards", "american_odds": +120, "description": "90+ rec yards", "player": "WR1"},
    {"market": "team_total", "american_odds": -115, "description": "Team total over 27.5"},
]


def test_build_correlated_parlay_ranks_by_edge():
    suggestions = corr_facade.build_correlated_parlay(
        PROPS, {"home_team": "KC", "away_team": "LV"}, "nfl", min_correlation=0.2, max_legs=3,
    )
    assert suggestions, "expected at least one suggestion"
    edges = [s["correlation_edge_pct"] for s in suggestions]
    assert edges == sorted(edges, reverse=True)
    top = suggestions[0]
    assert top["num_legs"] >= 2
    assert top["avg_correlation"] > 0
    assert top["game"] == "LV @ KC"
    for leg in top["legs"]:
        assert set(leg) >= {"description", "market", "american_odds", "implied_prob", "player", "side", "line"}


def test_build_correlated_parlay_min_legs_filter():
    suggestions = corr_facade.build_correlated_parlay(
        PROPS, {}, "nfl", min_correlation=-1.0, max_legs=3, min_legs=3,
    )
    assert all(s["num_legs"] == 3 for s in suggestions)


def test_build_correlated_parlay_high_threshold_filters_all():
    suggestions = corr_facade.build_correlated_parlay(
        PROPS, {}, "nfl", min_correlation=0.95, max_legs=3,
    )
    assert suggestions == []


def test_build_correlated_parlay_empty_props():
    assert corr_facade.build_correlated_parlay([], {}, "nfl") == []


# ---------------------------------------------------------------------------
# Assessment helpers
# ---------------------------------------------------------------------------

def test_rate_correlation_edge_tiers():
    rate = assessment._rate_correlation_edge
    assert rate(12, 0.55) == "ELITE"
    assert rate(7, 0.45) == "STRONG"
    assert rate(4, 0.35) == "GOOD"
    assert rate(2, 0.10) == "MARGINAL"
    assert rate(0.5, 0.10) == "WEAK"


def test_assess_mispricing_variants():
    anti = assessment._assess_mispricing(-0.02, -3.0, True, 0.2)
    assert anti.startswith("CAUTION")
    no_edge = assessment._assess_mispricing(-0.01, -1.0, False, 0.2)
    assert no_edge.startswith("NO EDGE")
    exceptional = assessment._assess_mispricing(0.2, 20.0, False, 0.5)
    assert exceptional.startswith("EXCEPTIONAL")
    strong = assessment._assess_mispricing(0.1, 9.0, False, 0.5)
    assert strong.startswith("STRONG")
    good = assessment._assess_mispricing(0.05, 4.0, False, 0.5)
    assert good.startswith("GOOD")
    marginal = assessment._assess_mispricing(0.01, 1.5, False, 0.5)
    assert marginal.startswith("MARGINAL")


# ---------------------------------------------------------------------------
# Anti-correlation detection
# ---------------------------------------------------------------------------

ANTI_LEGS = [
    {"market": "qb_interceptions", "american_odds": -110, "description": "INT over"},
    {"market": "team_total", "american_odds": -110, "description": "Team total over"},
]


def test_detect_anti_correlation_flags_negative_pairs():
    warnings = corr_facade.detect_anti_correlation(ANTI_LEGS, "nfl")
    assert len(warnings) == 1
    w = warnings[0]
    assert w["market_a"] == "qb_interceptions"
    assert w["market_b"] == "team_total"
    assert w["correlation"] == pytest.approx(-0.30)
    assert w["severity"] == "HIGH"  # |rho| > 0.25


def test_detect_anti_correlation_clean_pair():
    clean = [{"market": "qb_passing_yards"}, {"market": "qb_completions"}]
    assert corr_facade.detect_anti_correlation(clean, "nfl") == []


# ---------------------------------------------------------------------------
# SGP vig estimation
# ---------------------------------------------------------------------------

def test_estimate_sgp_vig_overcharging_book():
    # Book offers a price whose implied joint probability exceeds the true
    # correlated probability — the SGP tax overcharges relative to correlation.
    result = corr_facade.estimate_sgp_vig(_sgp_legs(), -140, "nfl")
    assert result["extra_sgp_vig"] > 0.001
    assert "OVERCHARGING" in result["assessment"]
    assert result["book_odds"] == -140


def test_estimate_sgp_vig_undercharging_book():
    result = corr_facade.estimate_sgp_vig(_sgp_legs(), +240, "nfl")
    assert result["extra_sgp_vig"] < -0.001
    assert "UNDERCHARGING" in result["assessment"]


def test_estimate_sgp_vig_structure():
    result = corr_facade.estimate_sgp_vig(_sgp_legs(), +260, "nfl")
    assert set(result) >= {
        "independent_prob", "book_implied_prob", "true_correlated_prob",
        "sgp_adjustment_book", "sgp_adjustment_true", "extra_sgp_vig",
        "extra_sgp_vig_pct", "independent_odds", "book_odds", "fair_odds",
        "assessment",
    }
    independent = math.prod(odds._american_to_implied(l["american_odds"]) for l in _sgp_legs())
    assert result["independent_prob"] == pytest.approx(round(independent, 6))
