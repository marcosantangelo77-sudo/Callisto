"""Tests for the tools.psych split of tools/market_psychology.py.

Verifies that the facade re-exports everything and each submodule behaves
identically to the original monolithic implementation.
"""

import math


def test_facade_reexports_public_api():
    import tools.market_psychology as mp

    for name in (
        "detect_number_shading",
        "detect_trap_line",
        "futures_efficiency",
        "optimal_hedge_time",
        "half_market_adjustment",
        "attention_arbitrage",
        "predict_closing_line",
        "full_market_psychology",
        "NFL_MARGIN_FREQ",
        "NFL_SPREAD_SHADE",
        "NBA_TOTAL_SHADE",
        "NBA_SPREAD_SHADE",
        "SCORING_DISTRIBUTION",
        "HALF_QUARTER_EDGES",
        "LINE_MOVEMENT_VELOCITY",
        "ATTENTION_WEIGHTS",
    ):
        assert hasattr(mp, name), f"facade missing {name}"


def test_submodules_importable():
    import tools.psych
    from tools.psych import (  # noqa: F401
        attention, closing_line, constants, futures,
        half_markets, shading, trap_lines,
    )

    assert tools.psych.detect_number_shading is not None


def test_constants_preserved():
    from tools.market_psychology import NFL_MARGIN_FREQ, NFL_SPREAD_SHADE

    assert abs(sum(NFL_MARGIN_FREQ.values()) - 1.0) < 0.05
    assert NFL_MARGIN_FREQ[3] > NFL_MARGIN_FREQ[4]
    assert NFL_SPREAD_SHADE[3.0] == 12


def test_detect_number_shading_on_key_number():
    from tools.market_psychology import detect_number_shading

    res = detect_number_shading(-3.0, "americanfootball_nfl", market="spreads")
    assert res["is_shaded"] is True
    assert res["shaded_toward"] == "this_number"
    assert res["shade_magnitude_cents"] == 12
    assert res["value_side"] == "opposite"


def test_detect_number_shading_off_number():
    from tools.market_psychology import detect_number_shading

    res = detect_number_shading(8.5, "americanfootball_nfl", market="spreads")
    assert res["is_shaded"] is False
    assert res["shaded_toward"] is None
    assert res["value_side"] == "neutral"


def test_detect_trap_line_no_movement():
    from tools.market_psychology import detect_trap_line

    res = detect_trap_line(-3.0, -3.0, public_pct=80)
    assert res["is_trap"] is True
    assert "NO_MOVEMENT" in res["trap_signals"]
    assert res["actionable_side"] == "opposite_public"


def test_futures_efficiency_underpriced():
    from tools.market_psychology import futures_efficiency

    res = futures_efficiency(
        opening_odds=2000, current_odds=500,
        games_played=8, total_games=17,
        current_wins=7, current_losses=1,
    )
    assert res["mispricing_direction"] in ("underpriced", "overpriced", "efficient")
    assert 0 <= res["season_progress"] <= 1
    assert res["estimated_vig"] > 0


def test_optimal_hedge_time_basic():
    from tools.market_psychology import optimal_hedge_time

    res = optimal_hedge_time(
        original_odds=2000, current_odds=300,
        original_stake=50.0, remaining_uncertainty=0.2,
    )
    assert res["recommendation"] in ("HEDGE_NOW", "PARTIAL_HEDGE", "SMALL_HEDGE", "LET_IT_RIDE")
    assert 0 <= res["optimal_hedge_fraction"] <= 1


def test_half_market_adjustment_nfl():
    from tools.market_psychology import half_market_adjustment

    res = half_market_adjustment(44.0, "americanfootball_nfl", half="first")
    assert res["scoring_fraction"] == 0.48
    assert res["projected_half_line"] == round(44.0 * 0.48 * 2) / 2.0


def test_attention_arbitrage():
    from tools.market_psychology import attention_arbitrage

    events = [
        {"sport": "americanfootball_nfl", "event_name": "Chiefs vs Ravens",
         "tag": "Monday Night Football", "is_live": True},
        {"sport": "basketball_nba", "event_name": "Spurs vs Pistons",
         "tag": "", "is_live": False},
    ]
    res = attention_arbitrage(events)
    assert res["marquee_count"] >= 1
    assert res["total_attention"] >= 8
    assert any(t["sport"] == "basketball_nba" for t in res["thin_markets"])


def test_predict_closing_line_direction():
    from tools.market_psychology import predict_closing_line

    res = predict_closing_line(
        current_line=-2.5, hours_to_game=12,
        sport="americanfootball_nfl",
        sharp_money_direction="favorite", public_pct=70,
    )
    assert isinstance(res["predicted_close"], float)
    assert res["confidence_interval_68"][0] <= res["predicted_close"] <= res["confidence_interval_68"][1]
    assert 0 < res["prediction_confidence"] <= 0.95


def test_prob_to_american():
    from tools.market_psychology import _prob_to_american

    assert _prob_to_american(0.5) == -100
    assert _prob_to_american(0.25) == 300
    assert _prob_to_american(0) == 0


def test_full_market_psychology_orchestration():
    from tools.market_psychology import full_market_psychology

    games = [{
        "away_team": "Away", "home_team": "Home",
        "bookmakers": [{
            "title": "Book",
            "markets": [{
                "key": "spreads",
                "outcomes": [{"name": "Home", "point": -3.0, "price": -110}],
            }],
        }],
    }]
    res = full_market_psychology(games, "americanfootball_nfl")
    assert res["games_analyzed"] == 1
    assert len(res["number_shading"]) == 1
    assert res["attention_arbitrage"]["recommendation"] == "PROVIDE_EVENTS"
