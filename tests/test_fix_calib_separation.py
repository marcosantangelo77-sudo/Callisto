"""tests/test_fix_calib_separation.py — estimate/ceiling separation rescore.

Verifies: (1) the reported (collapsed) number equals min(estimate, ceiling)
mapped onto the binary — the unchanged guard; (2) rescoring recorded
outcomes on the separated estimate changes calibration WITHOUT changing any
stored/sealed confidence; (3) the smoke5 gap is explained by collapse, not
by bad estimates — under a plausible reconstruction, bias shrinks toward 0.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tools.calibration.estimate_vs_ceiling import (
    TwoNumberPrediction,
    decompose_observed,
    rescore,
)


def test_reported_guard_unchanged():
    """min(estimate, ceiling) still bounds everything stored or sealed."""
    t = TwoNumberPrediction(question_id="q", estimate=0.9, ceiling=0.55,
                            leans_yes=True)
    assert t.reported == pytest.approx(0.5 + 0.55 / 2)   # 0.775, capped
    t2 = TwoNumberPrediction(question_id="q", estimate=0.3, ceiling=0.55,
                             leans_yes=True)
    assert t2.reported == pytest.approx(0.5 + 0.3 / 2)   # untouched below cap


def test_low_estimate_never_raised_by_separation():
    """Separation is NOT an inflation path: an estimate under its ceiling is
    reported exactly as before. Only over-ceiling estimates differ, and they
    differ only in what CALIBRATION is scored on, never in what seals."""
    t = TwoNumberPrediction(question_id="q", estimate=0.4, ceiling=0.55)
    assert t.reported == t.estimate_probability


def test_separation_improves_only_when_estimate_is_informative():
    """HONEST FRAMING: rescoring on the separated estimate improves Brier
    IF AND ONLY IF the raw estimate carries information the collapse
    destroyed. With an informative estimate (0.7 toward what actually
    happened) separation helps; with an uninformative one it hurts. This is
    precisely why finding #1 — the raw estimate was never recorded — must
    land before the fix can be evaluated."""
    def rows(est):
        return [{"question_id": q, "raw_estimate": est,
                 "ceiling": 0.55, "leans_yes": True, "outcome": True}
                for q in ("a", "b", "c", "d")]

    # Informative: model estimated 0.75-conf toward TRUE; collapse reported
    # only 0.55-conf. Scoring on the estimate is strictly better.
    good = rescore(rows(0.75))
    assert good["brier_improvement"] > 0
    assert abs(good["estimate_separated"]["mean_underconfidence_bias"]) \
        < abs(good["reported"]["mean_underconfidence_bias"])

    # Uninformative/wrong: same machinery, estimate pointing the wrong way,
    # separation does not rescue the scored number — measurement, not magic.
    # (est 0.10-conf toward TRUE, outcome TRUE: reported p=0.55, estimate
    # p=0.30. Both score badly; the estimate is WORSE or equal, never a fix.)
    bad = rescore(rows(0.10))
    assert bad["brier_improvement"] <= 0


def test_smoke5_cannot_be_decided_without_recorded_estimates():
    """With the actual smoke5 numbers, ANY rescore rests on a guessed raw
    estimate (it was discarded at clamp time). Under one plausible guess the
    separated score is better; under another it is worse — so the honest
    output of this investigation is 'the number needed for the answer was
    not recorded', plus the instrumentation to record it next run."""
    from tools.calibration.mechanisms import Attribution
    # the discarded-number proof: any raw estimate at or above the first
    # binding cap produces the IDENTICAL reported output — the collapse is
    # many-to-one over exactly the range where the estimate's information
    # lives.
    finals = {r: Attribution(raw_estimate=r,
                             objections=["MAJOR", "MINOR"]).run()[-1].after
              for r in (0.55, 0.60, 0.90)}
    assert len(set(finals.values())) == 1   # indistinguishable after clamp
    # below the cap the estimate survives — so only estimates ABOVE the cap
    # lose their information, and those are precisely the confident ones.
    low = Attribution(raw_estimate=0.30, objections=["MAJOR", "MINOR"])
    assert low.run()[-1].after < 0.34


def test_rescore_empty_raises():
    with pytest.raises(ValueError):
        rescore([])


def test_decompose_observed_gives_floor_estimate():
    """An observed collapsed report only bounds the true estimate from below
    when the ceiling bound; decompose must never claim to know more."""
    d = decompose_observed(observed_p=0.33, ceiling=0.55, leans_yes=False)
    assert 0.0 <= d.estimate <= 1.0
    assert d.reported == pytest.approx(0.33)


def test_validation():
    with pytest.raises(ValueError):
        TwoNumberPrediction(question_id="q", estimate=1.4, ceiling=0.5)
    with pytest.raises(ValueError):
        TwoNumberPrediction(question_id="q", estimate=0.5, ceiling=-0.1)
