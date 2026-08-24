<<<<<<< HEAD
"""tools/calibration — where do the 27 confidence points go?

This package MEASURES the confidence pipeline; it changes nothing. Every
ceiling, penalty and clamp in agp/ and tools/pipeline/ is left exactly as
shipped (the run that produced mean_brier 0.3129 measured a system that is
badly underconfident; this package finds the mechanism, it does not relax
the guard).

Three layers:

  instrument  run the REAL pipeline with a spy on the model seam and replay
              the documented adjustment chain step by step, verifying the
              replay reproduces the observed final score exactly.
  ablate      rerun the same questions with ONE mechanism disabled at a
              time (runtime-patched inside this process, restored after;
              no repo value is edited) and attribute the gap per mechanism.
  bridge      prototype carrying ESTIMATE and CEILING as two separate
              numbers: p_reported = 0.5 + sign(p_hat-0.5)*min(2|p_hat-0.5|,
              ceiling)/2. Inflation stays structurally impossible: the
              reported number never crosses 0.5 against the model's own
              side, never exceeds the model's own magnitude, and never
              exceeds the ceiling.

Nothing here may raise a ceiling, weaken a gate, or let an automated actor
inflate confidence. The resolution to underconfidence explored here is
SEPARATING TWO NUMBERS, not loosening any of them.
"""
from tools.calibration.instrument import (  # noqa: F401
    AttributionStep,
    ConfidenceTrace,
    InstrumentedRun,
    ModelSpy,
    MECHANISMS,
    instrumented_run,
    replay_chain,
)
from tools.calibration.bridge import (  # noqa: F401
    separated_report,
    certainty_of,
    rescore_separated,
)
=======
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
>>>>>>> origin/build/dd-decomposition-diversity
