"""Calibration diagnostics — measure WHERE downward shading enters.

The underconfidence finding (data/retro_batch/report_smoke5.json): predicted
0.33 where reality realised 0.60. This package instruments the subtraction
chain without changing any of it:

  mechanisms.py        — ordered replay of every downward adjustment, with
                         per-step attribution (points removed, by which rule).
  estimate_vs_ceiling.py — the ESTIMATE / CEILING separation: carry both
                         numbers, rescore calibration on the estimate.
  ab_axes.py           — A/B arms with one mechanism disabled at a time,
                         offline, from recorded runs.
  stacking.py          — compounding arithmetic for multiplicative caps.

INVARIANT (unchanged): nothing here may RAISE a stored confidence score.
The seal path still only subtracts. These tools measure; they do not fix.
"""
