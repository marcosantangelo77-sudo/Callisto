"""
Tests for the tools.regimes split of the original tools/regime.py.

The monolith was extracted into ``tools/regimes/`` (changepoint, recency,
power, bayes, reversion, composite) with ``tools/regime.py`` kept as a
facade. These tests verify:

1. Facade compatibility — every public (and historically used private)
   symbol is still importable from ``tools.regime`` and behaves identically.
2. Behavioral correctness of each sub-module.
3. The composite pipeline end-to-end.
"""

import math

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_series(n=30, base=100.0, noise=1.0, seed=7):
    """Series drawn from one stationary regime."""
    rng = np.random.default_rng(seed)
    return [float(v) for v in rng.normal(base, noise, n)]


def _shifted_series(n_before=20, n_after=20, before=95.0, after=105.0,
                    noise=1.0, seed=11):
    """Series with a genuine mean shift halfway through."""
    rng = np.random.default_rng(seed)
    first = rng.normal(before, noise, n_before)
    second = rng.normal(after, noise, n_after)
    return [float(v) for v in np.concatenate([first, second])]


def _team_data(history=None, **extra):
    data = {"name": "Test Team", "performance_history": history or []}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 1. Facade / package structure
# ---------------------------------------------------------------------------

class TestFacade:
    def test_all_public_names_importable_from_facade(self):
        import tools.regime as facade

        expected = [
            "ChangePointResult", "RecencyBiasResult", "PowerRating",
            "BayesianResult", "MeanReversionSignal",
            "detect_regime_change", "analyze_regimes",
            "recency_bias_score", "calculate_power_rating",
            "prior_weight_schedule", "bayesian_update",
            "seasonal_bayesian_rating", "mean_reversion_signal",
            "full_regime_analysis",
        ]
        for name in expected:
            assert hasattr(facade, name), f"facade missing {name}"
            assert name in facade.__all__

    def test_private_helpers_preserved_on_facade(self):
        # phases_impl and other callers historically reached into these
        import tools.regime as facade

        for name in ("_cost_normal", "_pelt_search", "_cusum_search",
                     "_classify_regime"):
            assert callable(getattr(facade, name))

    def test_facade_reexports_same_objects_as_submodules(self):
        from tools import regime as facade
        from tools.regimes import (
            bayes, changepoint, composite, power, recency, reversion,
        )

        assert facade.analyze_regimes is changepoint.analyze_regimes
        assert facade.detect_regime_change is changepoint.detect_regime_change
        assert facade.recency_bias_score is recency.recency_bias_score
        assert facade.calculate_power_rating is power.calculate_power_rating
        assert facade.bayesian_update is bayes.bayesian_update
        assert facade.mean_reversion_signal is reversion.mean_reversion_signal
        assert facade.full_regime_analysis is composite.full_regime_analysis

    def test_package_init_exports_match_facade(self):
        import tools.regime as facade
        import tools.regimes as package

        # Public API must match exactly; facade additionally re-exports
        # private helpers for backward compatibility.
        public = lambda names: sorted(n for n in names if not n.startswith("_"))
        assert public(package.__all__) == public(facade.__all__)

    def test_dataclasses_are_distinct_types(self):
        import tools.regime as facade

        assert facade.ChangePointResult is not facade.RecencyBiasResult
        assert facade.PowerRating is not facade.BayesianResult
        assert facade.MeanReversionSignal is not facade.ChangePointResult


# ---------------------------------------------------------------------------
# 2. Changepoint detection
# ---------------------------------------------------------------------------

class TestChangepoint:
    def test_no_changepoint_in_stable_series(self):
        from tools.regime import detect_regime_change

        # A truly constant series must produce zero change points.
        assert detect_regime_change([100.0] * 30) == []
        # A stationary series with an explicit strong penalty should not
        # fragment into a cascade of micro-segments.
        cps = detect_regime_change(_stable_series(30), penalty=5.0)
        assert len(cps) <= 1

    def test_detects_shift_in_shifted_series(self):
        from tools.regime import detect_regime_change

        series = _shifted_series(before=90.0, after=110.0, noise=0.5)
        cps = detect_regime_change(series, penalty=0.5)
        assert len(cps) >= 1
        # The detected point should be near the true shift (index 20)
        assert any(15 <= cp <= 26 for cp in cps)

    def test_cusum_method(self):
        from tools.regime import detect_regime_change

        series = _shifted_series(before=90.0, after=110.0, noise=0.5)
        cps = detect_regime_change(series, method="cusum")
        assert len(cps) >= 1

    def test_unknown_method_raises(self):
        from tools.regime import detect_regime_change

        with pytest.raises(ValueError, match="pelt"):
            detect_regime_change(_stable_series(10), method="wavelet")

    def test_too_short_returns_empty(self):
        from tools.regime import detect_regime_change

        assert detect_regime_change([1.0, 2.0], min_segment=3) == []
        assert detect_regime_change([], min_segment=3) == []

    def test_analyze_regimes_segments(self):
        from tools.regime import analyze_regimes

        result = analyze_regimes(
            _shifted_series(before=90.0, after=110.0, noise=0.5)
        )
        assert isinstance(result.n_segments, int)
        assert len(result.segment_means) == result.n_segments
        assert len(result.segment_variances) == result.n_segments
        assert 0.0 <= result.confidence <= 1.0
        if result.indices:
            # Means of segments around a big shift should differ substantially
            assert max(result.segment_means) - min(result.segment_means) > 10

    def test_analyze_regimes_stable_low_confidence(self):
        from tools.regime import analyze_regimes

        result = analyze_regimes(_stable_series(40, noise=0.001, seed=3))
        if result.n_segments == 1:
            assert result.confidence == 0.0

    def test_cost_normal(self):
        from tools.regime import _cost_normal

        assert _cost_normal(np.array([])) == 0.0
        assert _cost_normal(np.array([5.0])) == 0.0
        # Constant segment → zero variance → zero cost
        assert _cost_normal(np.array([5.0, 5.0, 5.0])) == 0.0
        spread = _cost_normal(np.array([1.0, 9.0]))
        assert spread == pytest.approx((2 / 2) * math.log(np.var(np.array([1.0, 9.0]))))

    def test_pelt_short_input(self):
        from tools.regime import _pelt_search

        assert _pelt_search(np.array([1.0, 2.0]), penalty=1.0) == []

    def test_pelt_matches_exact_search(self):
        # Regression test: young candidates (t - s < min_segment) must be
        # carried forward in the candidate set. The original implementation
        # dropped them, so PELT missed change points that exact search finds.
        from tools.regime import _pelt_search
        from tools.regimes.changepoint import _cost_normal

        def _exact(data, penalty, min_segment=3):
            data = np.asarray(data, dtype=float)
            n = len(data)
            if n < 2 * min_segment:
                return []
            INF = float("inf")
            F = np.full(n + 1, INF)
            F[0] = -penalty
            last = np.zeros(n + 1, int)
            for t in range(min_segment, n + 1):
                best = INF
                bs = 0
                for st in range(0, t - min_segment + 1):
                    c = F[st] + _cost_normal(data[st:t]) + penalty
                    if c < best:
                        best, bs = c, st
                F[t] = best
                last[t] = bs
            idx = n
            cps = []
            while idx > 0:
                cp = last[idx]
                if cp > 0:
                    cps.append(int(cp))
                idx = cp
            return sorted(cps)

        rng = np.random.default_rng(0)
        shifted = np.concatenate(
            [rng.normal(0.0, 0.1, 10), rng.normal(5.0, 0.1, 10)])
        penalty = 0.01
        assert list(_pelt_search(shifted, penalty)) == \
            _exact(shifted, penalty)
        # And the found change point is the true shift location.
        assert 10 in [int(c) for c in _pelt_search(shifted, penalty)]

        rng2 = np.random.default_rng(3)
        multi = np.concatenate([rng2.normal(0, 0.3, 15),
                                rng2.normal(2, 0.3, 15)])
        assert list(_pelt_search(multi, 0.05)) == _exact(multi, 0.05)

    def test_cusum_flat_input(self):
        from tools.regime import _cusum_search

        assert _cusum_search(np.array([5.0] * 20)) == []
        assert _cusum_search(np.array([1.0, 2.0])) == []


# ---------------------------------------------------------------------------
# 3. Recency bias
# ---------------------------------------------------------------------------

class TestRecencyBias:
    def test_overvalued_direction(self):
        from tools.regime import recency_bias_score

        season = _stable_series(30, base=50.0, noise=2.0)
        recent = list(season[-5:]) + [60.0, 61.0, 62.0]
        result = recency_bias_score(recent, season)

        assert result["bias_direction"] == "overvalued"
        assert 0.0 <= result["bias_magnitude"] <= 1.0
        assert result["perception_gap"] > 0
        assert result["recent_performance"] > result["underlying_performance"]

    def test_neutral_when_recent_matches_season(self):
        from tools.regime import recency_bias_score

        season = _stable_series(30, base=50.0, noise=1.0, seed=42)
        result = recency_bias_score(season[-4:], season)
        # z-score should be small → neutral direction expected
        assert abs(result["perception_gap"]) < 3 * 1.0

    def test_zero_variance_season(self):
        from tools.regime import recency_bias_score

        result = recency_bias_score([55.0] * 4, [50.0] * 20)
        assert result["bias_direction"] == "neutral"
        assert result["bias_magnitude"] == 0.0
        assert result["mean_reversion_probability"] == 0.5

    def test_dict_input_with_metric_key(self):
        from tools.regime import recency_bias_score

        season = [{"ppp": v} for v in _stable_series(20, base=1.0, noise=0.05)]
        recent = [{"ppp": 1.5}, {"ppp": 1.6}, {"ppp": 1.55}]
        result = recency_bias_score(recent, season, metric_key="ppp")
        assert result["perception_gap"] > 0

    def test_result_keys_complete(self):
        from tools.regime import recency_bias_score

        result = recency_bias_score([10.0, 10.5, 11.0],
                                    _stable_series(25, base=10.0))
        assert set(result) == {
            "bias_direction", "bias_magnitude", "mean_reversion_probability",
            "recent_performance", "underlying_performance", "perception_gap",
        }


# ---------------------------------------------------------------------------
# 4. Power ratings
# ---------------------------------------------------------------------------

class TestPowerRating:
    def test_insufficient_data(self):
        from tools.regime import calculate_power_rating

        result = calculate_power_rating(_team_data([100.0, 101.0]))
        assert result["regime"] == "insufficient_data"
        assert result["confidence"] == 0.0
        assert result["regime_games"] == 2

        empty = calculate_power_rating(_team_data([]))
        assert empty["rating"] == 0.0

    def test_stable_team_gets_stable_label_and_reasonable_rating(self):
        from tools.regime import calculate_power_rating

        history = _stable_series(30, base=100.0, noise=1.0)
        result = calculate_power_rating(_team_data(history))
        assert result["season_rating"] == pytest.approx(float(np.mean(history)), abs=0.01)
        assert result["rating"] == pytest.approx(100.0, abs=2.0)
        assert result["regime"] in ("stable", "improving", "declining", "volatile")

    def test_improving_team_detected(self):
        from tools.regime import calculate_power_rating

        history = _shifted_series(before=90.0, after=105.0, noise=0.5)
        result = calculate_power_rating(_team_data(history))
        # Whether a formal change point is found or a recency tilt is
        # applied, the rating must lean toward the stronger recent form.
        assert result["rating"] > float(np.mean(history))

    def test_league_avg_normalization(self):
        from tools.regime import calculate_power_rating

        history = _stable_series(20, base=100.0, noise=0.5)
        no_norm = calculate_power_rating(_team_data(history))
        norm = calculate_power_rating(_team_data(history, league_avg=95.0))
        assert norm["rating"] < no_norm["rating"]

    def test_classify_regime_unit(self):
        from tools.regime import _classify_regime

        assert _classify_regime([100.0], 0.0, 1.0) == "stable"
        assert _classify_regime([100.0, 105.0], 0.0, 1.0) == "improving"
        assert _classify_regime([100.0, 95.0], 0.0, 1.0) == "declining"
        assert _classify_regime([100.0, 100.5], 0.0, 1.0) == "stable"
        # Current variance much larger than season std → volatile
        assert _classify_regime([100.0, 101.0], current_variance=25.0,
                                season_std=1.0) == "volatile"


# ---------------------------------------------------------------------------
# 5. Bayesian prior management
# ---------------------------------------------------------------------------

class TestBayes:
    def test_prior_weight_schedule_monotone_decay(self):
        from tools.regime import prior_weight_schedule

        weights = [prior_weight_schedule(g, "nba") for g in range(0, 82)]
        assert all(weights[i] >= weights[i + 1] for i in range(len(weights) - 1))
        assert weights[0] > 0.95
        assert min(weights) >= 0.05
        # Late season weight is well below early season weight
        assert weights[-1] < weights[0] * 0.2

    def test_schedule_sport_differences(self):
        from tools.regime import prior_weight_schedule

        # NFL midpoint (6 games) comes much sooner than MLB (50 games)
        early = prior_weight_schedule(6, "nfl")
        mlb_early = prior_weight_schedule(6, "mlb")
        assert mlb_early > early

    def test_unknown_sport_falls_back_to_nba(self):
        from tools.regime import prior_weight_schedule

        assert (prior_weight_schedule(10, "quidditch")
                == prior_weight_schedule(10, "nba"))

    def test_bayesian_update_empty_evidence(self):
        from tools.regime import bayesian_update

        result = bayesian_update(75.0, [])
        assert result["posterior"] == 75.0
        assert result["prior_contribution"] == 1.0
        assert result["evidence_contribution"] == 0.0
        assert result["credible_interval"] == (73.0, 77.0)

    def test_posterior_between_prior_and_evidence_mean(self):
        from tools.regime import bayesian_update

        evidence = [80.0, 82.0, 81.0, 79.0]
        result = bayesian_update(70.0, evidence, prior_weight=0.5)
        ev_mean = float(np.mean(evidence))
        assert min(70.0, ev_mean) <= result["posterior"] <= max(70.0, ev_mean)

    def test_contributions_sum_to_one(self):
        from tools.regime import bayesian_update

        result = bayesian_update(70.0, [80.0] * 10, prior_weight=0.4)
        assert result["prior_contribution"] + result["evidence_contribution"] == \
            pytest.approx(1.0, abs=1e-6)
        assert result["prior_decay_applied"] == pytest.approx(0.6, abs=1e-6)

    def test_credible_interval_brackets_posterior(self):
        from tools.regime import bayesian_update

        result = bayesian_update(70.0, [72.0, 74.0, 76.0])
        lo, hi = result["credible_interval"]
        assert lo < result["posterior"] < hi

    def test_more_evidence_pulls_toward_evidence(self):
        from tools.regime import bayesian_update

        few = bayesian_update(50.0, [80.0, 81.0], prior_weight=0.5,
                              prior_variance=10.0)
        many = bayesian_update(50.0, [79.8, 80.2] * 20, prior_weight=0.5,
                               prior_variance=10.0)
        assert abs(many["posterior"] - 80.0) < abs(few["posterior"] - 80.0)

    def test_seasonal_convenience_function(self):
        from tools.regime import seasonal_bayesian_rating

        # Early season: posterior leans on prior; late season: on evidence.
        early = seasonal_bayesian_rating(
            100.0, [120.0, 121.0], sport="mlb")
        late = seasonal_bayesian_rating(
            100.0, [119.5, 120.5] * 75, sport="mlb", prior_variance=50.0)
        assert early["posterior"] < late["posterior"]
        assert late["prior_contribution"] < early["prior_contribution"]


# ---------------------------------------------------------------------------
# 6. Mean reversion
# ---------------------------------------------------------------------------

class TestMeanReversion:
    def test_extreme_hot_streak_flags_reversion_downward(self):
        from tools.regime import mean_reversion_signal

        history = [100.0] * 25 + [130.0] * 5
        result = mean_reversion_signal(history, league_avg=100.0)
        assert result["reversion_expected"] is True
        assert result["current_zscore"] > 0
        assert result["magnitude"] > 0
        assert result["confidence"] > 0

    def test_cold_streak_reverts_upward(self):
        from tools.regime import mean_reversion_signal

        history = [100.0] * 25 + [70.0] * 5
        result = mean_reversion_signal(history, league_avg=100.0)
        assert result["reversion_expected"] is True
        assert result["current_zscore"] < 0

    def test_no_reversion_for_typical_team(self):
        from tools.regime import mean_reversion_signal

        history = _stable_series(30, base=100.0, noise=1.0)
        result = mean_reversion_signal(history, league_avg=100.0)
        # Recent window near the mean → likely no strong reversion signal
        assert abs(result["current_zscore"]) < 3.0

    def test_too_few_games(self):
        from tools.regime import mean_reversion_signal

        result = mean_reversion_signal([100.0, 200.0], league_avg=110.0)
        assert result["reversion_expected"] is False
        assert result["confidence"] == 0.0

        empty = mean_reversion_signal([], league_avg=100.0)
        assert empty["current_value"] == 0.0

    def test_half_life_formula(self):
        from tools.regime import mean_reversion_signal

        history = _stable_series(20, base=100.0, noise=1.0)
        r05 = mean_reversion_signal(history, league_avg=100.0, regression_rate=0.5)
        expected_hl = -1.0 / math.log2(0.5)
        assert r05["half_life_games"] == pytest.approx(expected_hl, abs=0.02)

        r_none = mean_reversion_signal(history, league_avg=100.0,
                                       regression_rate=1.0)
        assert r_none["half_life_games"] == float("inf")

    def test_constant_history_edge_case(self):
        from tools.regime import mean_reversion_signal

        result = mean_reversion_signal([100.0] * 12, league_avg=100.0)
        assert result["reversion_expected"] is False
        assert result["magnitude"] == 0.0


# ---------------------------------------------------------------------------
# 7. Composite pipeline
# ---------------------------------------------------------------------------

class TestComposite:
    def test_full_analysis_structure(self):
        from tools.regime import full_regime_analysis

        team = _team_data(
            _shifted_series(before=90.0, after=104.0, noise=1.0),
            prior_rating=92.0, league_avg=95.0,
        )
        results = full_regime_analysis(team, sport="nba")

        assert results["team"] == "Test Team"
        assert results["games_analyzed"] == 40
        for key in ("regime_changes", "recency_bias", "power_rating",
                    "bayesian_rating", "mean_reversion"):
            assert key in results
        assert results["power_rating"]["regime"] != "insufficient_data"
        assert isinstance(results["actionable_signals"], list)
        assert results["has_edge_signal"] == (len(results["actionable_signals"]) > 0)

    def test_small_history_nulls_optional_sections(self):
        from tools.regime import full_regime_analysis

        results = full_regime_analysis(_team_data([100.0, 101.0, 99.0]))
        assert results["regime_changes"] is None
        assert results["recency_bias"] is None
        assert results["bayesian_rating"] is None
        assert results["mean_reversion"] is None
        assert results["power_rating"]["regime"] == "insufficient_data"
        assert results["actionable_signals"] == []
        assert results["has_edge_signal"] is False

    def test_bayesian_section_only_with_prior(self):
        from tools.regime import full_regime_analysis

        with_prior = full_regime_analysis(
            _team_data(_stable_series(20), prior_rating=90.0))
        without_prior = full_regime_analysis(_team_data(_stable_series(20)))
        assert with_prior["bayesian_rating"] is not None
        assert without_prior["bayesian_rating"] is None

    def test_actionable_signal_emitted_for_clear_regime_break(self):
        from tools.regime import full_regime_analysis

        history = [85.0] * 22 + [115.0] * 8
        results = full_regime_analysis(
            _team_data(history, league_avg=100.0))
        assert results["has_edge_signal"] is True
        joined = " | ".join(results["actionable_signals"])
        assert ("recency_bias" in joined) or ("mean_reversion" in joined)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
