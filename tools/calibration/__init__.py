"""tools/calibration — where do the 27 confidence points go?

This package MEASURES the confidence pipeline; it changes nothing. Every
ceiling, penalty and clamp in agp/ and tools/pipeline/ is left exactly as
shipped (the run that produced mean_brier 0.3129 measured a system that is
badly underconfident; this package finds the mechanism, it does not relax
the guard).

Layers:

  instrument  run the REAL pipeline with a spy on the model seam and replay
              the documented adjustment chain step by step, verifying the
              replay reproduces the observed final score exactly.
              wrap_model additionally sniffs the RAW pre-clamp estimate out
              of every model response into a caller-supplied log, so future
              batches no longer have to RECONSTRUCT the number that seal
              time discarded (finding #1 of the underconfidence batch).
  ablate      rerun the same questions with ONE mechanism disabled at a
              time (runtime-patched inside this process, restored after;
              no repo value is edited) and attribute the gap per mechanism.

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
    wrap_model,
)
