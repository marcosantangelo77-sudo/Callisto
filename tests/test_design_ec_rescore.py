"""Rescore of the smoke5 retrodiction batch under estimate/ceiling
separation, using the reconstruction protocol from
tools/calibration/rescore_smoke5.py. Asserts what the data actually shows —
including the negative result (separation does NOT improve Brier on this
batch) so the finding cannot silently rot.
"""
import json
import math
from pathlib import Path

from agp.estimate import rescore
from tools.calibration.rescore_smoke5 import load_rows, records_for


def _batch():
    p = Path("data/retro_batch/results_smoke5.jsonl")
    if not p.exists():
        import pytest
        pytest.skip("smoke5 batch artifact not present")
    return load_rows(str(p))


def test_collapse_column_reproduces_reported_brier():
    rows = _batch()
    out = rescore(records_for(rows, 0.80))
    # The report says mean_brier 0.3129; our collapsed column must agree.
    assert math.isclose(out["collapsed"]["brier"], 0.3129, abs_tol=1e-4)


def test_separation_does_not_improve_brier_on_smoke5():
    """THE HONEST NEGATIVE RESULT. All five outcomes were True while every
    prediction sat at 0.33: the model was underconfident AND often wrong.
    Raising the estimate toward its reconstructed value moves predictions
    AWAY from outcomes for every admissible raw estimate in [0.55, 1.0].
    Separation is a measurement fix; it is not an accuracy fix."""
    rows = _batch()
    for est in (0.55, 0.65, 0.75, 0.80, 0.90, 1.00):
        out = rescore(records_for(rows, est))
        assert out["brier_improvement"] <= 0.0, (
            f"est={est}: separation improved Brier — revisit the finding")


def test_bias_column_confirms_underconfidence_direction():
    """What separation DOES show on this batch: the collapsed column's mean
    bias (+0.27) matches the morning report's 27-point gap exactly."""
    rows = _batch()
    out = rescore(records_for(rows, 0.80))
    assert math.isclose(out["collapsed"]["mean_underconfidence_bias"], 0.27,
                        abs_tol=1e-4)
