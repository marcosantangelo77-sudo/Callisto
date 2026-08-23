"""tests/test_fix_calib_attribution.py — the attribution table.

Verifies the replay of every downward adjustment reproduces the observed
smoke5 fixed point and attributes points per mechanism. Invariant under
test: NO step in any trace ever raises the running value.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tools.calibration.mechanisms import Attribution, MECHANISMS


def test_reproduces_observed_fixed_point():
    """raw 0.80, INFERRED, unmet reqs, 1 MAJOR objection → 0.34 → p=0.33."""
    a = Attribution(raw_estimate=0.80, best_source_class="INFERRED",
                    requirements_met=False, n_resolved_descendants=0,
                    objections=["MAJOR", "MINOR"], self_review_mode=True,
                    n_independent_sources=1)
    a.run()
    # 0.80 → prov 0.55 → req 0.54 → inherit no-op → adv −0.15−0.05 = 0.34
    # → self-review no-op → synthesis min(0.34, 0.55*0.7=0.385) = 0.34
    assert a.final == pytest.approx(0.34)
    p = 0.5 - a.final / 2  # leans-no mapping used by PipelineResearcher
    assert round(p, 2) == 0.33


@pytest.mark.parametrize("mech", MECHANISMS)
def test_no_step_ever_raises(mech):
    """The invariant: every mechanism only subtracts or caps. Replay with a
    high raw estimate and assert monotone non-increase across ALL steps."""
    a = Attribution(raw_estimate=0.99, objections=["MINOR", "MINOR", "MAJOR"],
                    n_independent_sources=1, requirements_met=False,
                    self_review_mode=True)
    steps = a.run()
    for prev, nxt in zip(steps, steps[1:]):
        assert nxt.after <= prev.after + 1e-12, (prev, nxt)


@pytest.mark.parametrize("raw", [0.05, 0.2, 0.31, 0.5, 0.7, 0.9, 1.0])
def test_final_never_exceeds_raw_and_never_below_zero(raw):
    a = Attribution(raw_estimate=raw, objections=["MAJOR", "MINOR"])
    steps = a.run()
    assert steps[-1].after <= raw + 1e-12
    assert steps[-1].after >= 0.0


def test_low_estimate_is_not_pulled_down():
    """The core asymmetry: an estimate of 0.3 passes through nearly intact;
    an estimate of 0.8 collapses to ~the same floor as 0.55. Estimates are
    only ever pulled DOWN — that is the drift mechanism."""
    lo = Attribution(raw_estimate=0.30); hi = Attribution(raw_estimate=0.80)
    assert lo.run()[-1].after >= 0.29          # barely touched
    assert hi.run()[-1].after <= 0.40          # collapsed
    assert hi.total_removed() > 4 * max(lo.total_removed(), 0.01)


def test_by_mechanism_sums_to_total_removed():
    a = Attribution(raw_estimate=0.85, objections=["MINOR", "MAJOR"])
    a.run()
    assert sum(a.by_mechanism().values()) == pytest.approx(a.total_removed(),
                                                           abs=1e-6)


def test_blocking_objection_floors_to_veto():
    a = Attribution(raw_estimate=0.8, objections=["BLOCKING", "MINOR"])
    steps = a.run()
    veto_step = next(s for s in steps if s.mechanism == "adversary_penalty")
    assert veto_step.after == 0.0


def test_high_raw_estimates_collapse_to_same_floor():
    """Stacking means ANY confident estimate lands at one fixed point —
    the reported number carries almost no information about the estimate."""
    finals = {r: Attribution(raw_estimate=r).run()[-1].after
              for r in (0.60, 0.75, 0.90, 1.00)}
    assert max(finals.values()) - min(finals.values()) < 0.01
