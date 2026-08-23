"""Property test: separating estimate from ceiling creates NO path where an
automated actor raises a reported confidence.

The architecture's central commitment (BUILD_MANDATE rule 4, MORNING_REPORT
"the strongest case AGAINST the central design commitment"). This is the
property-based analogue of the R3 lesson in MORNING_REPORT: an example-based
test asserting the right invariant can pass while 663/2000 random inputs
violate it. Here the invariant is checked over random trajectories of
mechanism applications.

The property: for any EstimateCeiling and ANY sequence of downward-mechanism
applications (provenance clamp, requirement cap, adversary penalty,
inheritance clamp, self-review cap), the sealable number after is <= the
sealable number before, and never exceeds the entry ceiling. The only
estimate-moving call (with_estimate) still reports min(estimate, ceiling).
"""
import random

import pytest

from agp.estimate import EstimateCeiling


def _random_ec(rng: random.Random) -> EstimateCeiling:
    return EstimateCeiling(
        estimate=rng.random(),
        ceiling=rng.choice([0.34, 0.54, 0.55, 0.75, 1.0, rng.random()]))


MECHANISMS = [
    # (fn(ec, rng) -> EstimateCeiling) — every automated mechanism, applied
    # RELATIVELY (a clamp below the current ceiling / an additive penalty),
    # which is what real call sites do.
    lambda ec, rng: ec.with_ceiling(
        rng.uniform(0.0, ec.ceiling)),
    lambda ec, rng: ec.apply_adversary_penalty(rng.uniform(0, ec.ceiling + 0.2)),
    lambda ec, rng: ec.apply_adversary_penalty(0.05 * rng.randint(0, 4)),
]

N_TRIALS = 5000
STEPS = range(6)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_mechanism_sequence_raises_sealable(seed):
    rng = random.Random(seed)
    for _ in range(N_TRIALS // len([1, 2, 3])):
        ec = _random_ec(rng)
        entry_sealable = ec.sealable()
        entry_ceiling = ec.ceiling
        for _ in STEPS:
            m = rng.choice(MECHANISMS)
            # A mechanism that would raise the ceiling must be REJECTED by
            # the type itself — attempt it explicitly too.
            if rng.random() < 0.25:
                bump = min(1.0, ec.ceiling + rng.uniform(1e-6, 0.5))
                if bump > ec.ceiling:   # at ceiling==1.0 there is nothing above
                    with pytest.raises(ValueError):
                        ec.with_ceiling(bump)
            ec = m(ec, rng)
            assert ec.sealable() <= entry_sealable + 1e-12
            assert ec.sealable() <= ec.ceiling + 1e-12
            assert ec.sealable() <= entry_ceiling + 1e-12


def test_estimate_revision_never_exceeds_ceiling_in_report(seed=7):
    """with_estimate is the one upward-capable path — verify over random
    inputs that the REPORTED number stays bounded by entitlement."""
    rng = random.Random(seed)
    for _ in range(N_TRIALS):
        ec = _random_ec(rng)
        bumped = ec.with_estimate(rng.random())
        assert bumped.sealable() <= min(bumped.estimate, ec.ceiling) + 1e-12
        assert bumped.sealable() <= ec.ceiling + 1e-12


def test_quantisation_creates_no_upward_drift():
    """The round() bug class: repeated quantise/clamp round-trips must not
    creep upward. floor-only quantisation guarantees it."""
    rng = random.Random(11)
    for _ in range(2000):
        ec = _random_ec(rng)
        x = ec.sealable()
        for _ in range(10):
            ec = ec.with_ceiling(max(0.0, ec.ceiling - rng.uniform(0, 0.05)))
            y = ec.sealable()
            assert y <= x + 1e-12
            x = y
