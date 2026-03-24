"""
Tests for hypothesis lifecycle manager and statistical functions.
"""

import math
import pytest
from tools.hypothesis import (
    binomial_pvalue,
    ttest_one_sample,
    z_score,
    sharpe_ratio,
    max_drawdown,
    calibration_bins,
)


class TestBinomialPvalue:
    """Test the binomial significance test."""

    def test_coin_flip_no_edge(self):
        """50 wins out of 100 at 50% expected = not significant."""
        p = binomial_pvalue(50, 100, 0.50)
        assert p > 0.40  # should be ~0.54

    def test_clear_edge(self):
        """60 wins out of 100 at 50% expected = significant."""
        p = binomial_pvalue(60, 100, 0.50)
        assert p < 0.05

    def test_large_sample_small_edge(self):
        """530 wins out of 1000 at 50% = borderline significant."""
        p = binomial_pvalue(530, 1000, 0.50)
        assert p < 0.10

    def test_large_sample_real_edge(self):
        """550 wins out of 1000 at 50% = very significant."""
        p = binomial_pvalue(550, 1000, 0.50)
        assert p < 0.01

    def test_no_data(self):
        """Edge cases."""
        assert binomial_pvalue(0, 0, 0.50) == 1.0
        assert binomial_pvalue(5, 10, 0.0) == 1.0


class TestTtest:
    """Test the one-sample t-test."""

    def test_positive_returns(self):
        """Returns averaging +5% with some variance should be significant."""
        import random
        random.seed(123)
        returns = [random.gauss(0.05, 0.02) for _ in range(100)]
        t, p = ttest_one_sample(returns)
        assert p < 0.001
        assert t > 0

    def test_zero_returns(self):
        """Returns averaging 0 should not be significant."""
        returns = [0.05, -0.05] * 50
        t, p = ttest_one_sample(returns)
        assert p > 0.40

    def test_negative_returns(self):
        """Negative returns should have p > 0.5."""
        import random
        random.seed(456)
        returns = [random.gauss(-0.05, 0.02) for _ in range(100)]
        t, p = ttest_one_sample(returns)
        assert p > 0.99

    def test_small_sample(self):
        t, p = ttest_one_sample([0.1])
        assert p == 1.0  # single value, can't compute


class TestZScore:
    """Test z-score computation."""

    def test_no_edge(self):
        z = z_score(50, 100, 0.50)
        assert abs(z) < 0.5

    def test_strong_edge(self):
        z = z_score(60, 100, 0.50)
        assert z > 1.5

    def test_negative(self):
        z = z_score(40, 100, 0.50)
        assert z < -1.5


class TestSharpe:
    """Test Sharpe ratio."""

    def test_consistent_positive(self):
        """Consistent positive returns with tiny variance should give high Sharpe."""
        import random
        random.seed(789)
        returns = [random.gauss(0.05, 0.001) for _ in range(100)]
        sr = sharpe_ratio(returns)
        assert sr > 10

    def test_mixed_returns(self):
        """Normal-looking returns."""
        import random
        random.seed(42)
        returns = [random.gauss(0.02, 0.10) for _ in range(200)]
        sr = sharpe_ratio(returns)
        # With mean 0.02 and std 0.10, Sharpe ≈ 0.2
        assert 0.0 < sr < 1.0

    def test_empty(self):
        assert sharpe_ratio([]) == 0.0


class TestMaxDrawdown:
    """Test maximum drawdown calculation."""

    def test_no_drawdown(self):
        returns = [0.01] * 10
        assert max_drawdown(returns) == 0.0

    def test_single_loss(self):
        returns = [0.10, 0.10, -0.50, 0.10]
        mdd = max_drawdown(returns)
        assert mdd > 0

    def test_empty(self):
        assert max_drawdown([]) == 0.0


class TestCalibrationBins:
    """Test probability calibration binning."""

    def test_well_calibrated(self):
        """If predicted=0.7 and 70% actually win, calibration is good."""
        preds = [(0.7, True)] * 70 + [(0.7, False)] * 30
        bins = calibration_bins(preds, n_bins=1)
        assert len(bins) == 1
        assert abs(bins[0]["observed_rate"] - 0.70) < 0.01
        assert abs(bins[0]["predicted_avg"] - 0.70) < 0.01

    def test_empty(self):
        assert calibration_bins([]) == []

    def test_multiple_bins(self):
        preds = [(0.3, False)] * 50 + [(0.7, True)] * 50
        bins = calibration_bins(preds, n_bins=2)
        assert len(bins) == 2
        # First bin should be low probability predictions
        assert bins[0]["predicted_avg"] < 0.5
        # Second bin should be high probability predictions
        assert bins[1]["predicted_avg"] > 0.5
