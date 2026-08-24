"""Unit tests for agp/estimate.py — the estimate/ceiling split prototype."""
import math

import pytest

from agp.estimate import EstimateCeiling, brier, rescore


class TestConstruction:
    def test_rejects_estimate_out_of_range(self):
        with pytest.raises(ValueError):
            EstimateCeiling(estimate=1.2, ceiling=0.5)
        with pytest.raises(ValueError):
            EstimateCeiling(estimate=-0.1, ceiling=0.5)

    def test_rejects_ceiling_out_of_range(self):
        with pytest.raises(ValueError):
            EstimateCeiling(estimate=0.5, ceiling=1.4)
        with pytest.raises(ValueError):
            EstimateCeiling(estimate=0.5, ceiling=-0.01)

    def test_roundtrip_dict(self):
        ec = EstimateCeiling(estimate=0.8, ceiling=0.55)
        assert EstimateCeiling.from_dict(ec.to_dict()) == ec


class TestSealable:
    """I2: sealing behaviour is byte-identical to today's min() chain."""

    def test_ceiling_bounds_estimate(self):
        assert EstimateCeiling(0.8, 0.55).sealable() == 0.55

    def test_estimate_below_ceiling_passes_through(self):
        assert EstimateCeiling(0.30, 0.75).sealable() == 0.30

    def test_quantisation_is_downward_only(self):
        # 0.269 -> floor to 0.26, never round up to 0.27.
        assert EstimateCeiling(0.269, 1.0).sealable() == 0.26

    def test_binary_mapping_matches_retro(self):
        # tools/pipeline/retro.py:99: P(True)=0.5±conf/2
        ec = EstimateCeiling(0.8, 0.34)
        assert math.isclose(ec.to_binary_probability(True), 0.67)
        assert math.isclose(ec.to_binary_probability(False), 0.33)


class TestDownwardOnly:
    """I3: no method can raise either field."""

    def test_ceiling_may_fall(self):
        assert EstimateCeiling(0.8, 0.55).with_ceiling(0.40).ceiling == 0.40

    def test_ceiling_never_rises(self):
        with pytest.raises(ValueError):
            EstimateCeiling(0.8, 0.55).with_ceiling(0.90)

    def test_adversary_penalty_lowers_ceiling_not_estimate(self):
        ec = EstimateCeiling(0.80, 0.55).apply_adversary_penalty(0.20)
        assert ec.ceiling == pytest.approx(0.35)
        assert ec.estimate == 0.80
        assert ec.sealable() == 0.35

    def test_no_bonus_path(self):
        with pytest.raises(ValueError):
            EstimateCeiling(0.80, 0.55).apply_adversary_penalty(-0.10)
        # A penalty at least as large as the ceiling floors it at zero —
        # legitimate, never a raise.
        assert EstimateCeiling(0.80, 0.55).apply_adversary_penalty(1.0).sealable() == 0.0

    def test_estimate_revision_still_clamped_at_seal(self):
        # Even an explicit estimate revision cannot lift the REPORTED number
        # above entitlement.
        ec = EstimateCeiling(0.30, 0.55).with_estimate(0.95)
        assert ec.estimate == 0.95
        assert ec.sealable() == 0.55

    def test_frozen_dataclass(self):
        import dataclasses
        with pytest.raises(dataclasses.FrozenInstanceError):
            EstimateCeiling(0.5, 0.5).estimate = 0.9   # type: ignore[misc]


class TestRescore:
    def test_reproduces_collapse_column(self):
        recs = [{"question_id": "q", "estimate": 0.8, "ceiling": 0.34,
                 "leans_yes": True, "outcome": True}]
        out = rescore(recs)
        assert math.isclose(out["collapsed"]["brier"], (0.67 - 1) ** 2)

    def test_separated_column_uses_raw_estimate(self):
        recs = [{"question_id": "q", "estimate": 0.8, "ceiling": 0.34,
                 "leans_yes": True, "outcome": True}]
        out = rescore(recs)
        assert math.isclose(out["separated"]["brier"], (0.9 - 1) ** 2)

    def test_empty_records_rejected(self):
        with pytest.raises(ValueError):
            rescore([])


def test_brier_basic():
    assert brier(0.0, True) == 1.0
    assert brier(1.0, True) == 0.0
    assert brier(0.5, False) == 0.25
