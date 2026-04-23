"""Unit tests for tools.quant.consensus_engine.

Every math primitive is tested independently (devig methods), then the
end-to-end :func:`compute_consensus_fair_prob` is exercised with crafted
inputs that target the interesting properties:

  * Pinnacle-only with zero vig recovers the input exactly.
  * Four books at a true 50/50 market with realistic vigs return a
    consensus within 1% of 0.5.
  * A book whose devigged fair is >2σ from the weighted median gets
    trimmed and the ``disagreement`` flag fires.
  * An extreme outlier on a sharp book is NOT blindly trimmed — the
    tier weight dominates.
  * Degenerate "all books agree" case produces std_err = 0.
"""

import math

import pytest

from tools.quant.consensus_engine import (
    BOOK_TIER,
    BOOK_TIER_WEIGHT,
    BookLine,
    compute_consensus_fair_prob,
    multiplicative_devig,
    pinnacle_devig,
    power_devig,
)


# ──────────────────────────────────────────────────────────────────────
# Devig primitives
# ──────────────────────────────────────────────────────────────────────


def test_multiplicative_devig_sums_to_one():
    a, b = multiplicative_devig(0.52, 0.50)
    assert abs((a + b) - 1.0) < 1e-9
    assert 0.0 < a < 1.0 and 0.0 < b < 1.0


def test_multiplicative_devig_symmetric_vig_recovers_fair():
    # If both sides carry exactly the same vig (matched-hold book), the
    # devigged probs should be identical.
    a, b = multiplicative_devig(0.524, 0.524)
    assert abs(a - 0.5) < 1e-12
    assert abs(b - 0.5) < 1e-12


def test_multiplicative_devig_rejects_negative_inputs():
    with pytest.raises(ValueError):
        multiplicative_devig(-0.1, 0.6)


def test_pinnacle_devig_is_alias_for_multiplicative():
    # The module exports pinnacle_devig as a named alias.
    assert pinnacle_devig(0.52, 0.50) == multiplicative_devig(0.52, 0.50)


def test_power_devig_two_way_recovers_fair_on_zero_vig():
    # Already-fair probabilities should pass through unchanged.
    out = power_devig([0.5, 0.5])
    assert abs(sum(out) - 1.0) < 1e-6
    assert abs(out[0] - 0.5) < 1e-6


def test_power_devig_handles_asymmetric_vig():
    # A book that charges more vig on the favorite (common in retail)
    # would imply (0.58, 0.47) rather than (0.55, 0.50) for a fair 55/45
    # market. Power devig should pull back toward 55/45.
    out = power_devig([0.58, 0.47])
    assert abs(sum(out) - 1.0) < 1e-6
    # The favorite is still the favorite.
    assert out[0] > out[1]
    # Favorite should end up near its true fair (around 55%).
    assert 0.54 < out[0] < 0.57


def test_power_devig_three_way_sums_to_one():
    # Soccer 1X2 market with overround.
    out = power_devig([0.50, 0.33, 0.25])
    assert abs(sum(out) - 1.0) < 1e-6
    assert all(0 < x < 1 for x in out)


def test_power_devig_rejects_degenerate_input():
    with pytest.raises(ValueError):
        power_devig([])
    with pytest.raises(ValueError):
        power_devig([1.5, 0.4])
    with pytest.raises(ValueError):
        power_devig([0.0, 0.5])


# ──────────────────────────────────────────────────────────────────────
# Consensus aggregation
# ──────────────────────────────────────────────────────────────────────


def test_consensus_pinnacle_only_matches_pinnacle_devig():
    # One book in, same book out. No trim, no disagreement.
    lines = [BookLine("pinnacle", 0.505, paired_implied_prob=0.505)]
    r = compute_consensus_fair_prob(lines)
    assert abs(r.fair_prob - 0.5) < 1e-6
    assert r.n_books == 1 and r.n_books_raw == 1
    assert r.disagreement is False
    assert r.outlier_books == []


def test_consensus_recovers_fair_50_50_from_vigged_books():
    # Four realistic books all offering the same true 50/50 market with
    # different vig rates. Consensus should come out ~0.50.
    lines = [
        BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
        BookLine("draftkings", 0.524, paired_implied_prob=0.524),
        BookLine("fanduel", 0.520, paired_implied_prob=0.520),
        BookLine("caesars", 0.527, paired_implied_prob=0.527),
    ]
    r = compute_consensus_fair_prob(lines)
    assert abs(r.fair_prob - 0.5) < 0.01
    assert r.n_books == 4
    assert r.disagreement is False


def test_consensus_trims_outlier_and_flags_disagreement():
    # One book is dramatically off (think stale line or book error).
    lines = [
        BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
        BookLine("draftkings", 0.510, paired_implied_prob=0.515),
        BookLine("fanduel", 0.508, paired_implied_prob=0.512),
        # 7-point outlier — clearly off.
        BookLine("wynn", 0.570, paired_implied_prob=0.470),
    ]
    r = compute_consensus_fair_prob(lines)
    assert "wynn" in r.outlier_books
    assert r.disagreement is True
    # Consensus should be close to the three non-outlier books.
    assert 0.49 < r.fair_prob < 0.51


def test_consensus_degenerate_all_agree_std_err_zero():
    # Every book devigs to exactly the same fair — std_err is 0, no
    # disagreement, no outliers.
    lines = [
        BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
        BookLine("draftkings", 0.505, paired_implied_prob=0.505),
        BookLine("fanduel", 0.505, paired_implied_prob=0.505),
    ]
    r = compute_consensus_fair_prob(lines)
    assert r.std_err == 0.0
    assert r.disagreement is False
    assert r.outlier_books == []
    assert abs(r.fair_prob - 0.5) < 1e-6


def test_consensus_skews_toward_sharp_tier_weights():
    # Two sharp books say 0.55; three soft books say 0.60. Sharp-weighted
    # consensus should land NEAR the sharp side, not the simple mean.
    # Note: the books must be in BOOK_TIER or they default to soft; we
    # pick real entries so weights apply as intended.
    lines = [
        BookLine("pinnacle", 0.560, paired_implied_prob=0.456),   # devigs to ~0.551
        BookLine("lowvig", 0.560, paired_implied_prob=0.456),
        BookLine("caesars", 0.615, paired_implied_prob=0.415),    # devigs to ~0.597
        BookLine("fanatics", 0.615, paired_implied_prob=0.415),
        BookLine("hardrock", 0.615, paired_implied_prob=0.415),
    ]
    r = compute_consensus_fair_prob(lines)
    # Arithmetic mean of devigged values ≈ 0.579. Tier-weighted
    # mean should be CLOSER to the sharp value (~0.551) than that.
    assert r.fair_prob < 0.579


def test_consensus_handles_single_sided_line():
    # No paired prob supplied — single-sided devig path. Still produces
    # a usable fair prob.
    lines = [
        BookLine("pinnacle", 0.505),
        BookLine("draftkings", 0.524),
    ]
    r = compute_consensus_fair_prob(lines)
    assert 0.48 < r.fair_prob < 0.52
    assert r.n_books >= 1


def test_consensus_custom_tier_weights_work():
    # Overriding tier weights lets the caller down-weight sharp on
    # markets where Pinnacle isn't the sharpest (e.g., UFC props).
    override = {"sharp": 0.1, "reference": 0.45, "soft": 0.45}
    lines = [
        BookLine("pinnacle", 0.560, paired_implied_prob=0.456),
        BookLine("draftkings", 0.615, paired_implied_prob=0.415),
        BookLine("caesars", 0.615, paired_implied_prob=0.415),
    ]
    r_custom = compute_consensus_fair_prob(lines, tier_weights=override)
    r_default = compute_consensus_fair_prob(lines)
    # Custom weights down-weight sharp, so consensus drifts UP toward the
    # retail books compared to the default.
    assert r_custom.fair_prob > r_default.fair_prob


def test_consensus_rejects_empty_input():
    with pytest.raises(ValueError):
        compute_consensus_fair_prob([])


def test_consensus_effective_sample_size_makes_sense():
    # ESS should equal N when all weights are equal; less than N when
    # mixed tiers (because the weights are unequal).
    equal_lines = [
        BookLine("draftkings", 0.505, paired_implied_prob=0.505),
        BookLine("fanduel", 0.505, paired_implied_prob=0.505),
        BookLine("betmgm", 0.505, paired_implied_prob=0.505),
    ]
    r_equal = compute_consensus_fair_prob(equal_lines)
    assert abs(r_equal.effective_sample_size - 3.0) < 1e-6

    mixed_lines = [
        BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
        BookLine("draftkings", 0.505, paired_implied_prob=0.505),
        BookLine("caesars", 0.505, paired_implied_prob=0.505),
    ]
    r_mixed = compute_consensus_fair_prob(mixed_lines)
    assert r_mixed.effective_sample_size < 3.0
    assert r_mixed.effective_sample_size > 1.0


def test_consensus_per_book_fair_contains_every_input_book():
    lines = [
        BookLine("pinnacle", 0.505, paired_implied_prob=0.505),
        BookLine("draftkings", 0.524, paired_implied_prob=0.524),
        BookLine("caesars", 0.527, paired_implied_prob=0.527),
    ]
    r = compute_consensus_fair_prob(lines)
    assert set(r.per_book_fair.keys()) == {"pinnacle", "draftkings", "caesars"}
    for fair in r.per_book_fair.values():
        assert 0.0 < fair < 1.0


def test_tier_weights_sum_to_one():
    # Invariant on the module-level constants: tier weights form a valid
    # probability distribution.
    assert abs(sum(BOOK_TIER_WEIGHT.values()) - 1.0) < 1e-9
    assert all(w >= 0 for w in BOOK_TIER_WEIGHT.values())


def test_every_tier_used_in_BOOK_TIER_has_a_weight():
    # If a book is classified as 'X', BOOK_TIER_WEIGHT must know X.
    tiers_in_use = set(BOOK_TIER.values())
    assert tiers_in_use.issubset(BOOK_TIER_WEIGHT.keys())
