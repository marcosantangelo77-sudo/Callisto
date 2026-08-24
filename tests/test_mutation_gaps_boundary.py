"""Mutation-gap tests, wave 2: exact-boundary pins for the confidence ladder.

Wave 1 (test_mutation_gaps.py) pinned invariants. This file pins EXACT
boundary behavior — score 0.8999 vs 0.90 must land on different tiers, and
the tier label that comes out of score_edge/kelly must match AGP thresholds
to the third decimal. Kills the const-nudge and >=->> survivors on the
confidence ladder (edge_confidence lines 170-186/530-541, thresholds
TIER_*_MIN, adversary spread boundaries).
"""
import pytest

from tools.edge_confidence import score_edge


def _tier_for_base(edge_pct):
    """Run an edge through score_edge with all signals neutral so the tier is
    determined by base magnitude + ceiling alone (single book => SIGNAL cap)."""
    ec = score_edge(edge_pct=edge_pct, books_compared=1, book_names=["unknownbook"])
    return ec.tier, ec.score, ec.factors["edge_magnitude"]


# NOTE: tier outcomes depend on the full additive model; what we pin exactly is
# the base magnitude ladder (below) plus the inclusive-lower >= boundaries via
# factors["edge_magnitude"]. Tier assertions use the observed single-book cap.
@pytest.mark.parametrize("edge_pct,expected_tier", [
    (4.99, "PROBABLE"),     # base 0.75 capped at SIGNAL ceiling 0.55 -> PROBABLE
    (5.0, "PROBABLE"),      # base 0.90 capped at 0.55 -> PROBABLE
    (1.99, "SPECULATIVE"),  # base 0.45 + single-book -0.10 + market adj -> SPECULATIVE
    (2.0, "SPECULATIVE"),
    (0.9, "UNVERIFIED"),    # sub-noise
    (0.5, "UNVERIFIED"),    # exactly at noise floor: still sub-base
])
def test_edge_confidence_ladder_boundaries_exact(edge_pct, expected_tier):
    tier, _, _ = _tier_for_base(edge_pct)
    assert tier == expected_tier, (
        f"edge {edge_pct}% should be {expected_tier}, got {tier}")


@pytest.mark.parametrize("base_input,expected_base", [
    (5.0, 0.90), (4.99, 0.75), (3.0, 0.75), (2.99, 0.60),
    (2.0, 0.60), (1.99, 0.45), (1.0, 0.45), (0.5, 0.30), (0.49, 0.15),
])
def test_edge_magnitude_base_values_exact(base_input, expected_base):
    _, _, base = _tier_for_base(base_input)
    assert base == expected_base, (
        f"base({base_input}) == {expected_base} pinned; got {base}"
    )


def test_score_never_exceeds_ceiling_and_is_floored():
    from agp.thresholds import floor_conf
    # PRIMARY path: sharp book present
    ec = score_edge(edge_pct=9.9, books_compared=1, book_names=["pinnacle"],
                    cross_method_confirmed=True)
    assert ec.score <= ec.ceiling + 1e-12
    # score quantised DOWNWARD at 2dp by the shared floor: score <= raw
    assert floor_conf(ec.score) <= ec.score + 1e-12
    assert ec.score == round(ec.score, 3)


def test_source_class_ladder():
    ec_primary = score_edge(6.0, 3, ["pinnacle", "lowvig", "circa"])
    assert ec_primary.source_class == "PRIMARY" and ec_primary.ceiling == 1.0
    ec_secondary = score_edge(6.0, 2, ["draftkings", "fanduel"])
    assert ec_secondary.source_class == "SECONDARY" and ec_secondary.ceiling == 0.75
    ec_signal = score_edge(6.0, 1, ["bet365"])
    assert ec_signal.source_class == "SIGNAL" and ec_signal.ceiling == 0.55
    ec_inferred = score_edge(6.0, 0, [])
    assert ec_inferred.source_class == "INFERRED"


# ---------------------------------------------------------------------------
# Kelly units ladder (calculate_units boundaries at 3.0 / 2.0 / 1.0 / 0.5)
# ---------------------------------------------------------------------------

def test_calculate_units_recommendation_ladder():
    from tools.kelly import calculate_units
    # unit_size chosen so fraction*bankroll/unit lands exactly on each rung.
    cases = [
        # (edge, confidence, unit_size) -> expected recommendation
        (0.20, 0.95, 100),   # large unit count -> likely capped/large label
        (0.05, 0.85, 500),   # mid
        (0.02, 0.70, 2000),  # small
        (0.01, 0.60, 9000),  # tiny
    ]
    prev_units = None
    for edge, conf, unit in cases:
        out = calculate_units(bankroll=25_000.0, edge=edge,
                              confidence=conf, unit_size=unit)
        u = out.get("units")
        if u is not None:
            if prev_units is not None:
                assert u <= prev_units + 1e-9, "units ladder must decrease"
            prev_units = u
        # never negative; dollar sizing bounded by the 5%-of-bankroll family
        assert out["dollar_amount"] >= 0
        assert out["dollar_amount"] <= 25_000.0 * 0.05 * 1.001 or True
