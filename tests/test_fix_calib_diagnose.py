"""tests/test_fix_calib_diagnose.py — end-to-end diagnosis on the real data.

Runs tools.calibration.diagnose against the ACTUAL smoke5 results file and
checks the headline claims: the fixed point reproduces, the attribution
table is non-empty and sums correctly, and the output JSON carries the
sample-size caveat. Also tests the instrument wrapper against ScriptedModel.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tools.calibration.diagnose import diagnose

SMOKE5 = Path("data/retro_batch/results_smoke5.jsonl")


@pytest.mark.skipif(not SMOKE5.exists(), reason="smoke5 data not present")
def test_diagnose_on_real_smoke5_data():
    report = diagnose(SMOKE5)
    # fixed point reproduced: mean_final ≈ 0.34 (the observed confidence)
    assert abs(report["attribution"]["mean_final"] - 0.34) < 0.01
    mech = report["attribution"]["mean_removed_by_mechanism"]
    assert sum(mech.values()) == pytest.approx(
        report["attribution"]["raw_estimate_assumed"]
        - report["attribution"]["mean_final"], abs=0.02)
    # A/B verdict exists and names a real mechanism
    assert report["ab_arms"]["verdict_largest_single"] in mech
    # stacking funnel documented
    assert "floor" in report["stacking"]["compounding_note"]
    # sample-size honesty
    assert "n=5" in report["sample_size_caveat"]
    # invariant statement present
    assert "no mechanism was relaxed" in report["invariant"]


def test_instrument_wrapper_logs_raw_estimate():
    """The instrument must capture proposed_confidence without changing what
    the pipeline sees."""
    from tools.pipeline.model import ScriptedModel
    from tools.calibration.instrument import wrap_model

    raw_log: list = []
    scripted = ScriptedModel(default={"parsed_json": {
        "answer": "yes", "proposed_confidence": 0.82}})
    model = wrap_model(scripted, raw_log)

    import asyncio
    resp = asyncio.run(model.complete("Manager", [{"role": "user",
                                                   "content": "q"}]))
    assert resp["parsed_json"]["proposed_confidence"] == 0.82  # unchanged
    assert raw_log and raw_log[-1]["raw_estimate"] == pytest.approx(0.82)


def test_instrument_never_breaks_on_garbage():
    from tools.pipeline.model import ScriptedModel
    from tools.calibration.instrument import wrap_model

    raw_log: list = []
    scripted = ScriptedModel(default="not json at all")
    model = wrap_model(scripted, raw_log)
    import asyncio
    asyncio.run(model.complete("Manager", []))
    assert raw_log == []          # soft-fail: nothing logged, nothing raised
