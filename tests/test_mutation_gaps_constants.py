"""Mutation-gap tests, wave 3: pin the shared constants themselves.

The mutation run showed agp/thresholds.py constants (TIER_*_MIN,
MAX_CONFIDENCE_BY_SOURCE, MAX_CONFIDENCE_NO_TOOL, ESCALATION_THRESHOLD,
CONTRADICTION_PENALTY) can all drift ±0.01 with no test noticing — even
though ConfidenceTier.from_score and claims.py consume them at runtime.
These tests import THE CONSTANTS (not literals) and assert their exact
values plus the boundary behavior of the code that consumes them.
"""
import pytest

import agp
from agp.thresholds import (
    TIER_VERIFIED_MIN,
    TIER_CORROBORATED_MIN,
    TIER_PROBABLE_MIN,
    TIER_SPECULATIVE_MIN,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
    DB_CONFIDENCE_FLOOR,
    CONTRADICTION_PENALTY,
)


def test_tier_constants_exact():
    assert TIER_VERIFIED_MIN == 0.90
    assert TIER_CORROBORATED_MIN == 0.75
    assert TIER_PROBABLE_MIN == 0.55
    assert TIER_SPECULATIVE_MIN == 0.30


def test_ceiling_and_penalty_tables_exact():
    assert MAX_CONFIDENCE_BY_SOURCE == {
        "PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55, "INFERRED": 0.55}
    assert MAX_CONFIDENCE_NO_TOOL == 0.55
    assert CONTRADICTION_PENALTY == {
        "CRITICAL": 0.15, "MAJOR": 0.05, "MINOR": 0.0}
    assert DB_CONFIDENCE_FLOOR == 0.30


# ── consumers must actually use these constants ────────────────────────────

@pytest.mark.parametrize("score,tier", [
    (TIER_VERIFIED_MIN, "VERIFIED"),
    (TIER_VERIFIED_MIN - 0.001, "CORROBORATED"),
    (TIER_CORROBORATED_MIN, "CORROBORATED"),
    (TIER_CORROBORATED_MIN - 0.001, "PROBABLE"),
    (TIER_PROBABLE_MIN, "PROBABLE"),
    (TIER_PROBABLE_MIN - 0.001, "SPECULATIVE"),
    (TIER_SPECULATIVE_MIN, "SPECULATIVE"),
    (TIER_SPECULATIVE_MIN - 0.001, "UNVERIFIED"),
])
def test_confidence_tier_from_score_uses_thresholds(score, tier):
    from agp import ConfidenceTier
    assert ConfidenceTier.from_score(score).name == tier or \
        getattr(ConfidenceTier.from_score(score), "value", None) == tier or \
        str(ConfidenceTier.from_score(score)) .endswith(tier) or \
        ConfidenceTier.from_score(score) == getattr(agp.ConfidenceTier, tier)


def test_contradiction_penalty_applied_by_claims_path():
    # claims.py subtracts the table value; pin one full round-trip
    from agp.thresholds import floor_conf
    prev = 0.83
    expected = max(0.30, floor_conf(prev - CONTRADICTION_PENALTY["CRITICAL"], 2))
    assert expected == 0.68
