"""K1 — the calibration table can manufacture its own ground truth.

_implied_outcome recovers the "realised binary" for a row from the recorded
brier, and on any ambiguity — or when brier is absent — falls back to
`1.0 if p >= 0.5 else 0.0`: the PREDICTION'S OWN DIRECTION.

Rows without answer_binary are exactly the ones rehydrated from checkpoints or
read from JSONL written by older code. For those rows the calibration table
reports agreement with itself, and nothing marks the bin as derived rather
than observed.

The red team measured: implied outcome agreed with the prediction in 2000/2000
random cases, a forged row reported a wrong call as CORRECT, and ten truth-less
rows rendered a textbook-perfect table with the verdict "strongly better than
chance".

This is not a scoring nit. The project's headline empirical claim — bin 0.2-0.4
"predicted 0.33, realised 0.60" — was read off this table.

A row without ground truth must be EXCLUDED and the exclusion DISCLOSED, never
imputed.
"""
import pytest

from tools.retrodiction.batch import BatchResult, _implied_outcome


def _row(p, y=None, brier=None):
    return BatchResult(question_id="q", status="ok",
                       predicted_probability=p, answer_binary=y, brier=brier)


def test_row_without_truth_yields_no_outcome():
    """No answer_binary must mean 'unknown', not a fabricated binary."""
    assert _implied_outcome(_row(0.9)) is None, \
        "a truth-less row produced a realised outcome"
    assert _implied_outcome(_row(0.1)) is None


def test_truthless_outcome_never_just_echoes_the_prediction():
    """The exact forgery: y recovered as sign(p) agrees with p every time."""
    agree = 0
    probs = [i / 100.0 for i in range(0, 101)]
    for p in probs:
        y = _implied_outcome(_row(p, brier=0.0025))
        if y is None:
            continue
        if (y >= 0.5) == (p >= 0.5):
            agree += 1
    assert agree == 0, (
        f"{agree}/{len(probs)} truth-less rows produced an outcome agreeing "
        "with their own prediction")


def test_forged_flip_is_not_reported_correct():
    """truth y=1, model said p=0.05, row stores brier=0.0025 and no truth."""
    y = _implied_outcome(_row(0.05, brier=0.0025))
    assert y is None, "a forged brier resurrected a fabricated 'correct' call"


def test_real_ground_truth_is_still_used():
    """The fix must not discard rows that DO carry truth."""
    assert _implied_outcome(_row(0.9, y=True)) == 1.0
    assert _implied_outcome(_row(0.9, y=False)) == 0.0


def test_calibration_table_discloses_truthless_rows():
    """A bin built on fewer rows than it contains must say so."""
    from tools.retrodiction.batch import build_report
    rows = [  # status must be "scored" to enter the calibration table
        BatchResult(question_id="a", status="scored",
                    predicted_probability=0.30, answer_binary=True, brier=0.49),
        BatchResult(question_id="b", status="scored",
                    predicted_probability=0.32, answer_binary=None, brier=0.10),
        BatchResult(question_id="c", status="scored",
                    predicted_probability=0.34, answer_binary=None, brier=0.10),
    ]
    rep = build_report({r.question_id: r for r in rows})
    table = rep.get("calibration_overall") or []
    bin_of_interest = [b for b in table
                       if b.get("n_no_truth", 0) > 0]
    assert bin_of_interest, "excluded truth-less rows were not disclosed"
    b = bin_of_interest[0]
    assert b["n"] == 1 and b["n_no_truth"] == 2, (
        f"expected 1 scored / 2 excluded, got n={b['n']} "
        f"n_no_truth={b['n_no_truth']}")
