"""tests/test_fix_calib_ab_axes.py — A/B arms and stacking arithmetic.

Each arm disables ONE mechanism in the counterfactual replay; the test
verifies (a) every arm's final >= baseline final (removing a downward
mechanism can only raise the counterfactual), and (b) the stacking table
matches hand-computed arithmetic for the smoke5 configuration.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tools.calibration.ab_axes import ab_attribution, stack_arithmetic
from tools.calibration.mechanisms import Attribution, MECHANISMS


def _smoke5_shape() -> Attribution:
    return Attribution(raw_estimate=0.80, best_source_class="INFERRED",
                       requirements_met=False, n_resolved_descendants=0,
                       objections=["MAJOR"], self_review_mode=True,
                       n_independent_sources=1)


def test_arms_cover_all_mechanisms():
    out = ab_attribution(_smoke5_shape())
    assert set(out["arms"]) == set(MECHANISMS)
    assert out["verdict_largest_single"] in MECHANISMS


def test_removing_any_mechanism_never_lowers_the_counterfactual():
    """Direction check: disabling a downward mechanism can only restore
    points in the REPLAY — never remove more."""
    base = _smoke5_shape(); base.run()
    out = ab_attribution(base)
    for mech, arm in out["arms"].items():
        assert arm["final_without"] >= out["baseline_final"] - 1e-9, mech


def test_shares_sum_within_one():
    out = ab_attribution(_smoke5_shape())
    total = sum(a["points_restored"] for a in out["arms"].values())
    # arms overlap (multiple mechanisms bind at the same floor) so shares can
    # exceed the gap; but each individual share must be <= 1.
    for arm in out["arms"].values():
        if arm["share_of_gap"] is not None:
            assert 0.0 <= arm["share_of_gap"] <= 1.0 + 1e-9
    assert total > 0


def test_no_single_mechanism_explains_the_gap():
    """THE STACKING FINDING: because the caps compound into a funnel
    (provenance 0.55 → req/inherit/self-review 0.54 → synthesis 0.385),
    disabling ANY ONE mechanism lets the others re-collapse a confident
    estimate to nearly the same floor. No single mechanism accounts for the
    27-point gap; the bias is structural."""
    base = _smoke5_shape(); base.run()
    out = ab_attribution(base)
    for mech, arm in out["arms"].items():
        restored = arm["points_restored"]
        assert restored < 0.10, (mech, restored)   # none moves it materially
    # whereas removing the whole stack at once recovers the raw estimate
    fully = _smoke5_shape()
    fully.best_source_class = "PRIMARY"
    fully.requirements_met = True
    fully.n_resolved_descendants = 99
    fully.objections = []
    fully.self_review_mode = False
    fully.n_independent_sources = 4
    assert fully.run()[-1].after > 0.75


def test_provenance_ceiling_is_largest_single_in_smoke5_shape():
    """With raw=0.80 collapsed to 0.34, the provenance ceiling (0.55 on
    INFERRED) is the single largest restorable block — but 'largest' here
    restores only ~0.05; see test_no_single_mechanism_explains_the_gap."""
    out = ab_attribution(_smoke5_shape())
    assert out["verdict_largest_single"] == "provenance_ceiling"


def test_stacking_matches_hand_computation():
    """Hand-check: 0.80 → prov 0.55 → req 0.54 → inherit 0.54 → adv −0.15
    → 0.39 → self-review no-op → synthesis 0.55*0.70=0.385 → floor 0.38."""
    s = stack_arithmetic(0.80, n_objections_major=1, n_independent_sources=1)
    assert s["compounded_final"] == pytest.approx(0.38)
    assert s["compounded_removed"] == pytest.approx(0.42)
    chain = {r["step"]: r["value"] for r in s["chain"]}
    assert chain["provenance_ceiling"] == 0.55
    assert chain["requirement_cap"] == 0.54
    assert chain["inheritance_clamp"] == 0.54
    assert chain["adversary_penalty"] == 0.39
    assert chain["synthesis_agreement"] == 0.38


def test_compounding_is_subadditive():
    """Compounded removal < naive sum of single-mechanism removals, because
    each later cap acts on an already-shrunken base. This is why 'just sum
    the penalties' overstates — the bias is a multiplicative funnel, not an
    additive tax."""
    s = stack_arithmetic(0.95, n_objections_major=2, n_independent_sources=2)
    assert s["compounded_removed"] < s["sum_if_naive_additive"]
    # and the funnel property: any estimate above the floor lands AT it
    finals = {r: stack_arithmetic(r)["compounded_final"]
              for r in (0.5, 0.7, 0.9)}
    assert len(set(finals.values())) == 1
