"""Tests for the tools.lanalysis split of tools/line_analysis.py.

Verifies that:
1. The facade (tools.line_analysis) re-exports the full public API.
2. The implementation modules in tools/lanalysis are the real code.
3. Behavior is preserved (decomposition, RLM, steam, timing, public side,
   contrarian value, EV-of-analysis, and the composite report).
4. Downstream consumers (tools.autonomous) can still import from the facade.
"""

import math

import pytest

import tools.line_analysis as facade


# ---------------------------------------------------------------------------
# Facade re-exports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "decompose_movement",
        "detect_rlm",
        "detect_steam",
        "optimal_bet_timing",
        "estimate_public_side",
        "contrarian_value",
        "ev_of_analysis",
        "full_line_analysis",
        "TEAM_BRAND_TIERS",
        "NFL_KEY_NUMBERS",
        "CONTRARIAN_ROI_TABLE",
    ],
)
def test_facade_exports(name):
    assert hasattr(facade, name)


def test_submodule_reexports_are_same_objects():
    from tools.lanalysis import (
        composite,
        decomposition,
        priority,
        public,
        rlm,
        steam,
        timing,
    )

    assert facade.decompose_movement is decomposition.decompose_movement
    assert facade.detect_rlm is rlm.detect_rlm
    assert facade.detect_steam is steam.detect_steam
    assert facade.optimal_bet_timing is timing.optimal_bet_timing
    assert facade.estimate_public_side is public.estimate_public_side
    assert facade.contrarian_value is public.contrarian_value
    assert facade.ev_of_analysis is priority.ev_of_analysis
    assert facade.full_line_analysis is composite.full_line_analysis
    assert facade.TEAM_BRAND_TIERS is public.TEAM_BRAND_TIERS


def test_package_modules_exist():
    import tools.lanalysis as pkg

    for mod in (
        "_util",
        "constants",
        "decomposition",
        "rlm",
        "steam",
        "timing",
        "public",
        "priority",
        "composite",
    ):
        assert hasattr(pkg, mod), mod


# ---------------------------------------------------------------------------
# Movement decomposition
# ---------------------------------------------------------------------------


def _linear_history(n=8, slope=0.2, books=("pinnacle", "fanduel")):
    return [
        {
            "timestamp": 1_000_000 + i * 60,
            "line": -3.0 + slope * i,
            "book": books[i % len(books)],
        }
        for i in range(n)
    ]


class TestDecomposeMovement:
    def test_too_few_points(self):
        result = facade.decompose_movement(
            [{"timestamp": 0, "line": -3}, {"timestamp": 60, "line": -2.5}]
        )
        assert result["error"]
        assert result["trend"] == []
        assert result["sharp_component"] == 0.0

    def test_linear_series_is_all_trend(self):
        history = _linear_history(n=10)
        result = facade.decompose_movement(history)
        assert result["data_points"] == 10
        # Perfectly linear series: trend captures nearly all movement
        assert result["trend_direction"] == "rising"
        assert result["sharp_component"] == pytest.approx(0.2 * 9, abs=0.01)
        assert result["signal_to_noise"] > 3
        assert result["trend_variance_pct"] > 99.0
        assert len(result["trend"]) == 10
        assert len(result["noise"]) == 10
        assert "interpretation" in result

    def test_flat_series(self):
        history = [{"timestamp": 1000 + i, "line": -3.0} for i in range(6)]
        result = facade.decompose_movement(history)
        assert result["trend_direction"] == "flat"
        assert result["sharp_component"] == pytest.approx(0.0)

    def test_sorts_by_timestamp(self):
        shuffled = list(reversed(_linear_history(n=8)))
        result = facade.decompose_movement(shuffled)
        assert result["trend_direction"] == "rising"

    def test_sport_lambda_changes_result(self):
        # Alternating jitter around a rising line — smoother (higher-lambda)
        # trend should hug the raw values less tightly on the noisy parts.
        noisy = [
            dict(e, line=e["line"] + (0.15 if i % 2 == 0 else -0.15))
            for i, e in enumerate(_linear_history(n=12))
        ]
        nfl = facade.decompose_movement(noisy, "americanfootball_nfl")
        nba = facade.decompose_movement(noisy, "basketball_nba")
        assert nfl["raw_values"] == nba["raw_values"]
        # Smoother NFL trend is flatter through the jitter than NBA's
        nfl_range = max(nfl["trend"]) - min(nfl["trend"])
        nba_range = max(nba["trend"]) - min(nba["trend"])
        assert abs(nfl_range - 0.2 * 11) <= abs(nba_range - 0.2 * 11)

    def test_iso_timestamp_parsing(self):
        history = [
            {"timestamp": f"2026-01-01T00:{i:02d}:00Z", "line": -3.0 - 0.1 * i}
            for i in range(5)
        ]
        result = facade.decompose_movement(history)
        assert result["trend_direction"] == "falling"


# ---------------------------------------------------------------------------
# RLM detection
# ---------------------------------------------------------------------------


class TestDetectRlm:
    def test_strong_rlm(self):
        result = facade.detect_rlm(line_movement_direction=-1.5, public_ticket_pct=75, public_money_pct=45)
        assert result["is_rlm"] is True
        assert result["strength"] == "STRONG"
        assert result["confidence"] > 0.6
        assert "opposite of public" in result["sharp_side"]

    def test_moderate_rlm(self):
        result = facade.detect_rlm(-1.0, 65, 42)
        assert result["is_rlm"] is True
        assert result["strength"] == "MODERATE"

    def test_weak_rlm(self):
        result = facade.detect_rlm(-0.2, 55, 52)
        assert result["is_rlm"] is True
        assert result["strength"] == "WEAK"
        assert result["confidence"] <= 0.35

    def test_no_rlm_when_line_moves_with_public(self):
        result = facade.detect_rlm(2.0, 75, 70)
        assert result["is_rlm"] is False
        assert result["strength"] == "NONE"
        assert result["confidence"] == 0.0
        assert "aligned with public" in result["sharp_side"]

    def test_public_on_b_side_rlm(self):
        # Public mostly on B (tickets < 50) but line moves toward A
        result = facade.detect_rlm(1.5, 30, 25)
        assert result["is_rlm"] is True
        assert result["ticket_money_divergence"] == 5.0

    def test_clamping(self):
        result = facade.detect_rlm(-10, 150, -20)
        assert result["ticket_pct_side_a"] == 100.0
        assert result["money_pct_side_a"] == 0.0


# ---------------------------------------------------------------------------
# Steam detection
# ---------------------------------------------------------------------------


def _steam_snapshots(base_ts=1_000_000):
    """Three books all dropping ~20 cents within a few minutes."""
    snaps = []
    for book in ("pinnacle", "fanduel", "draftkings"):
        snaps.append({"timestamp": base_ts, "line": -3.0, "book": book})
        snaps.append({"timestamp": base_ts + 120, "line": -3.2, "book": book})
    # One unrelated early snapshot pair to satisfy the >=4 snapshot minimum
    snaps.append({"timestamp": base_ts - 600, "line": -2.8, "book": "betmgm"})
    snaps.append({"timestamp": base_ts - 590, "line": -2.8, "book": "caesars"})
    return snaps


class TestDetectSteam:
    def test_no_steam_with_few_snapshots(self):
        assert facade.detect_steam([{"timestamp": 0, "line": -3, "book": "a"}] * 3) == []

    def test_detects_coordinated_move(self):
        moves = facade.detect_steam(_steam_snapshots(), threshold_cents=15, time_window_minutes=10)
        assert moves, "expected at least one steam move"
        top = moves[0]
        assert top["direction"] == "down"
        assert top["books_moved"] == 3
        assert top["avg_movement"] == pytest.approx(-0.2, abs=0.01)
        assert top["confidence"] > 0
        # Sorted by confidence descending
        confidences = [m["confidence"] for m in moves]
        assert confidences == sorted(confidences, reverse=True)

    def test_no_steam_when_books_disagree(self):
        base = 2_000_000
        snaps = [
            {"timestamp": base, "line": -3.0, "book": "pinnacle"},
            {"timestamp": base + 60, "line": -3.5, "book": "pinnacle"},
            {"timestamp": base, "line": -3.0, "book": "fanduel"},
            {"timestamp": base + 60, "line": -2.5, "book": "fanduel"},
        ]
        assert facade.detect_steam(snaps, threshold_cents=15, time_window_minutes=5) == []

    def test_slow_drift_below_window_not_flagged(self):
        base = 3_000_000
        snaps = []
        for book in ("pinnacle", "fanduel"):
            snaps.append({"timestamp": base, "line": -3.0, "book": book})
            snaps.append({"timestamp": base + 3600, "line": -3.2, "book": book})
        snaps.append({"timestamp": base, "line": -2.9, "book": "betmgm"})
        snaps.append({"timestamp": base + 3600, "line": -2.9, "book": "caesars"})
        moves = facade.detect_steam(snaps, threshold_cents=15, time_window_minutes=10)
        coordinated = [m for m in moves if m["books_moved"] >= 2]
        assert coordinated == []


# ---------------------------------------------------------------------------
# Timing optimization
# ---------------------------------------------------------------------------


class TestOptimalBetTiming:
    @pytest.mark.parametrize(
        "sport",
        [
            "americanfootball_nfl",
            "americanfootball_ncaaf",
            "basketball_nba",
            "basketball_ncaab",
            "baseball_mlb",
            "icehockey_nhl",
            "soccer_epl",  # generic profile
        ],
    )
    def test_returns_structured_profile_for_every_sport(self, sport):
        result = facade.optimal_bet_timing(sport, "spreads", "sunday", 48.0)
        assert result["sport"] == sport
        assert result["market"] == "spreads"
        assert result["optimal_window"]
        assert isinstance(result["historical_edge_pct"], float)
        assert result["all_windows"]
        assert result["reasoning"]
        assert result["general_principle"]

    def test_nfl_lookahead_window(self):
        result = facade._nfl_timing("spreads", "sunday", 160.0)
        labels = [w["window"] for w in result["all_windows"]]
        assert any("look-ahead" in l for l in labels)
        assert result["historical_edge_pct"] == 2.5

    def test_mlb_post_lineup_card_is_optimal(self):
        result = facade._mlb_timing("h2h", "tuesday", 2.0)
        assert result["optimal_window"].startswith("Post-lineup card")

    def test_generic_profile_early_line(self):
        result = facade._generic_timing("spreads", "friday", 48.0)
        assert result["optimal_window"].startswith("Early line")

    def test_timing_wraps_profile_fields(self):
        result = facade.optimal_bet_timing("basketball_nba", "totals", "monday", 12.0)
        assert result["day_of_week"] == "monday"
        assert result["hours_to_game"] == 12.0


# ---------------------------------------------------------------------------
# Public side estimation
# ---------------------------------------------------------------------------


class TestEstimatePublicSide:
    def test_big_brand_favorite_primetime(self):
        result = facade.estimate_public_side(
            line_open=-7.5,
            line_current=-8.5,
            sport="americanfootball_nfl",
            is_primetime=True,
            team_a="Dallas Cowboys",
            team_b="Chicago Bears",
        )
        assert result["estimated_public_pct_a"] > 65
        assert result["public_favorite"] == "A"
        assert result["fade_side"] == "B"
        assert result["fade_value"] > 0
        assert result["confidence"] in ("medium-high", "high")
        assert "Dallas Cowboys" in result["interpretation"]

    def test_even_matchup_is_split(self):
        result = facade.estimate_public_side(line_open=-1.0, line_current=-1.0)
        assert result["public_favorite"] in ("split", "A", "B")
        assert 15.0 <= result["estimated_public_pct_a"] <= 85.0

    def test_clamped_to_reasonable_range(self):
        result = facade.estimate_public_side(
            line_open=-14,
            line_current=-17,
            sport="americanfootball_nfl",
            is_primetime=True,
            is_rivalry=True,
            team_a="Dallas Cowboys",
            team_a_recent_wins=3,
        )
        assert result["estimated_public_pct_a"] <= 85.0
        assert result["estimated_public_pct_b"] >= 15.0

    def test_hot_team_recency_boost(self):
        cold = facade.estimate_public_side(-3.0, -3.0, team_a_recent_wins=0)
        hot = facade.estimate_public_side(-3.0, -3.0, team_a_recent_wins=3)
        assert hot["estimated_public_pct_a"] > cold["estimated_public_pct_a"]

    def test_contrarian_roi_lookup_matches_table(self):
        result = facade.estimate_public_side(-10, -12, sport="basketball_nba")
        pct = result["estimated_public_pct_a"]
        expected = None
        for (lo, hi), roi in facade.CONTRARIAN_ROI_TABLE["basketball_nba"].items():
            if lo <= max(pct, 100 - pct) < hi:
                expected = roi
                break
        if result["fade_side"] != "neither":
            assert result["fade_value"] == expected


# ---------------------------------------------------------------------------
# Contrarian value
# ---------------------------------------------------------------------------


class TestContrarianValue:
    def test_high_public_pct_football_off_key_number(self):
        result = facade.contrarian_value(80.0, "americanfootball_nfl", spread=5.5)
        assert result["on_key_number"] is False
        # Base 4.1 + off-key amplification 0.5
        assert result["adjusted_roi"] == pytest.approx(4.6)
        assert result["contrarian_edge"] == pytest.approx(result["adjusted_roi"] / 2.0)
        assert "amplified edge" in result["interpretation"]

    def test_key_number_penalty(self):
        on_key = facade.contrarian_value(80.0, "americanfootball_nfl", spread=3.0)
        assert on_key["on_key_number"] is True
        assert on_key["key_number_adjustment"] == -0.8
        # Base 4.1 - 0.8 + large-spread bump? spread=3 -> no bump
        assert on_key["adjusted_roi"] == pytest.approx(3.3)

    def test_large_spread_bonus(self):
        result = facade.contrarian_value(70.0, "americanfootball_nfl", spread=13.5)
        assert result["adjusted_roi"] == pytest.approx(2.4 + 0.5 + 0.5)

    def test_unmodeled_sport_uses_default_table(self):
        result = facade.contrarian_value(80.0, "soccer_epl", spread=1.0)
        assert result["base_historical_roi"] == 3.0
        # Not football -> no key number adjustment
        assert result["key_number_adjustment"] == 0.0
        assert result["on_key_number"] is False

    def test_low_pct_is_low_confidence(self):
        result = facade.contrarian_value(55.0, "americanfootball_nfl")
        assert result["confidence"] == "low"


# ---------------------------------------------------------------------------
# EV of analysis
# ---------------------------------------------------------------------------


class TestEvOfAnalysis:
    def test_boring_game_is_skip(self):
        result = facade.ev_of_analysis(
            {
                "line_movement": 0.1,
                "books_count": 10,
                "price_spread_across_books": 2,
                "hours_to_game": 24,
                "line_stable_hours": 48,
            }
        )
        assert result["priority"] in ("SKIP", "LOW")
        assert result["priority_score"] < 15
        assert result["negative_signals"], "expected penalties for a boring game"

    def test_actionable_game_is_high_priority(self):
        result = facade.ev_of_analysis(
            {
                "line_movement": 3.0,
                "has_injury_news": True,
                "has_weather_concern": True,
                "books_count": 2,
                "price_spread_across_books": 40,
                "estimated_public_pct": 82,
                "is_primetime": True,
                "hours_to_game": 3.0,
            }
        )
        assert result["priority_score"] == 100.0  # clamped
        assert result["priority"] == "HIGH"
        assert len(result["positive_signals"]) >= 6

    def test_score_clamped_at_zero_floor(self):
        result = facade.ev_of_analysis(
            {
                "line_movement": 0,
                "books_count": 12,
                "hours_to_game": 0.1,
                "line_stable_hours": 72,
            }
        )
        assert result["priority_score"] == 0.0
        assert result["priority"] == "SKIP"


# ---------------------------------------------------------------------------
# Timestamp utility
# ---------------------------------------------------------------------------


class TestParseTimestamp:
    def test_numeric_epoch_seconds(self):
        assert facade._parse_timestamp(1_700_000_000) == 1_700_000_000.0

    def test_milliseconds_converted(self):
        assert facade._parse_timestamp(1_700_000_000_000) == 1_700_000_000.0

    def test_iso_string(self):
        ts = facade._parse_timestamp("2026-01-01T00:00:00Z")
        assert ts == pytest.approx(1_767_225_600.0)

    def test_date_only_string(self):
        ts = facade._parse_timestamp("2026-01-01")
        assert ts == pytest.approx(1_767_225_600.0)

    def test_garbage_returns_zero(self):
        assert facade._parse_timestamp("not-a-date") == 0.0


# ---------------------------------------------------------------------------
# Composite analysis
# ---------------------------------------------------------------------------


class TestFullLineAnalysis:
    def _run(self, **overrides):
        kwargs = dict(
            line_history=_linear_history(),
            sport="americanfootball_nfl",
            line_open=-2.5,
            line_current=-4.0,
            hours_to_game=24.0,
            day_of_week="sunday",
        )
        kwargs.update(overrides)
        return facade.full_line_analysis(**kwargs)

    def test_report_structure(self):
        report = self._run(public_ticket_pct=75, public_money_pct=40)
        for key in (
            "decomposition",
            "rlm",
            "steam_moves",
            "timing",
            "public_estimate",
            "contrarian",
            "analysis_priority",
            "summary",
        ):
            assert key in report, key

    def test_real_ticket_data_skips_estimate_in_rlm(self):
        report = self._run(public_ticket_pct=75, public_money_pct=40)
        assert "note" not in report["rlm"]
        assert report["rlm"]["is_rlm"] is True

    def test_estimated_rlm_gets_note(self):
        report = self._run()
        assert report["rlm"]["note"] == "Based on estimated (not actual) public percentages"

    def test_single_book_history_notes_no_steam(self):
        history = [
            {"timestamp": 1000 + i * 60, "line": -3 + 0.1 * i, "book": "pinnacle"}
            for i in range(6)
        ]
        report = facade.full_line_analysis(
            history, "americanfootball_nfl", -3.0, -2.5
        )
        assert report["steam_moves"] == {"note": "Need multi-book snapshots for steam detection"}

    def test_short_history_notes_insufficient(self):
        report = facade.full_line_analysis([], "basketball_nba", -2.0, -2.5)
        assert "Insufficient line history" in report["decomposition"]["note"]

    def test_summary_signals_and_assessment(self):
        report = self._run(public_ticket_pct=78, public_money_pct=38)
        summary = report["summary"]
        assert summary["signal_count"] == len(summary["signals_detected"])
        assert summary["overall_assessment"] in (
            "STRONG — multiple confirming signals",
            "MODERATE — some signals present",
            "WEAK — no strong signals detected",
        )

    def test_game_data_passthrough_vs_synthetic(self):
        explicit = self._run(
            game_data={
                "line_movement": 3.0,
                "has_injury_news": True,
                "books_count": 2,
                "price_spread_across_books": 40,
                "estimated_public_pct": 82,
            }
        )
        assert explicit["analysis_priority"]["priority"] == "HIGH"
        synthetic = self._run()
        assert "analysis_priority" in synthetic

    def test_composite_components_agree_with_direct_calls(self):
        report = self._run(public_ticket_pct=75, public_money_pct=45)
        direct_decomp = facade.decompose_movement(_linear_history(), "americanfootball_nfl")
        assert report["decomposition"]["sharp_component"] == direct_decomp["sharp_component"]
        direct_rlm = facade.detect_rlm(-1.5, 75, 45)
        assert report["rlm"]["confidence"] == direct_rlm["confidence"]


# ---------------------------------------------------------------------------
# Constants integrity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_brand_tiers_values(self):
        assert facade.TEAM_BRAND_TIERS["Dallas Cowboys"] == 3
        assert facade.TEAM_BRAND_TIERS["Chicago Bears"] == 2
        assert facade.TEAM_BRAND_TIERS.get("Some Expansion Team", 1) == 1

    def test_nfl_key_numbers(self):
        assert 3 in facade.NFL_KEY_NUMBERS
        assert 7 in facade.NFL_KEY_NUMBERS
        assert 99 not in facade.NFL_KEY_NUMBERS

    def test_contrarian_table_monotonic_per_sport(self):
        for sport, table in facade.CONTRARIAN_ROI_TABLE.items():
            rois = sorted(table.items())
            prev = None
            for bucket, roi in rois:
                if prev is not None:
                    assert roi >= prev, (sport, bucket)
                prev = roi


# ---------------------------------------------------------------------------
# Downstream consumer compatibility
# ---------------------------------------------------------------------------


def test_autonomous_imports_from_facade():
    import inspect

    import tools.autonomous as autonomous

    src = inspect.getsource(autonomous)
    assert "from tools.line_analysis import" in src
    # The names autonomous.py imports must exist on the facade
    for name in ("detect_rlm", "detect_steam", "estimate_public_side", "contrarian_value", "optimal_bet_timing"):
        assert getattr(facade, name, None) is not None
