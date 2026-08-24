# DD instrument decision — two `tools/calibration/instrument.py`, one must go

## What each implementation is

### Master's version (fix/underconfidence, blob ab4c453) — the LIVE instrument
409 lines. It runs the REAL pipeline (`instrumented_run()`): wraps the model
seam in a `ModelSpy` (records every parsed proposal incl. raw pre-clamp
`proposed_confidence`), executes `ResearchPipeline.run()`, then REPLAYS the
engine's exact adjustment chain (provenance ceiling → requirement gate →
inheritance clamp → adversary penalties → floor → retro bridge half-scale +
sign) and verifies the replay reproduces the OBSERVED final score bit-exactly
(`verified=False` on any mismatch). It also carries `sign_of_prediction()`,
which keeps the retired prose keyword scan purely for attribution: it counts
forecasts whose sign the old scorer got backwards. Consumers on master:
`tools/calibration/ablate.py`, `tools/calibration/__init__.py`.

What only it can do: attribute points to mechanisms against an actual run and
PROVE the attribution (exact replay verification); measure the bridge's
certainty-halving and sign defects; feed ablation.

### Branch's version (build/dd-decomposition-diversity) — the offline replay
Two pieces:
- `mechanisms.py`: a pure-arithmetic replay (`Attribution`) of the same chain,
  parameterised by hand (raw estimate, source class, objection severities...).
  No pipeline execution, no verification against observed output. Plus
  `attribution_from_batch_row()`, which reconstructs the smoke5 batch's fixed
  point (0.80 → 0.34 → p=0.33) from stored rows whose raw estimates were
  discarded — with explicitly flagged assumptions.
- `instrument.py`: `wrap_model(model, raw_log)` — a thin proxy that sniffs
  `proposed_confidence` out of model responses into a caller-supplied log so
  FUTURE batches record the raw estimate. ~60 lines, no replay logic at all.

Consumers on branch: `ab_axes.py`, `diagnose.py`,
tests/test_fix_calib_attribution/ab_axes/diagnose/separation.

## Decision

**Keep master's instrument.py wholesale. Port the branch's one unique idea —
`wrap_model`'s raw-estimate logging — into it as a small addition, and retire
the rest of the branch's copy.**

Reasons:
1. The branch's instrument.py has no attribution logic; its substance is
   wrap_model. Deleting the file would delete a genuinely useful seam-capture
   tool, so port it rather than drop it.
2. mechanisms.py duplicates master's replay chain as hand-set arithmetic with
   hardcoded reconstructions (raw_estimate=0.80 assumption, severity guess
   "MAJOR+MINOR"). Every duplicated rule in this repo has drifted (membership
   rule x3, forecast-sign rule x2). Master's version verifies itself against
   reality; mechanisms.py cannot detect drift at all. This is exactly the
   duplication pattern that burned us before.
3. The smoke5 reconstruction work is done — its conclusions are recorded in
   data/retro_batch/diagnosis_underconfidence.json and findings/. The tool
   that produced them has no forward-looking job: future runs can use
   instrumented_run/wrap_model to get real numbers instead of reconstructed
   ones.

Concretely:
- `tools/calibration/instrument.py` = master's + `wrap_model()` /
  `wrap_model_raw_log` added (branch's `_sniff` logic, reusing the module's
  own `parse_model_json` import).
- Branch's `mechanisms.py` deleted; `ab_axes.py` and `diagnose.py` either
  move to master's verified replay or are retired if their tests are
  reconstruction-only (resolved during merge; see below).
- `__init__.py`: master's docstring/exports stand; add `wrap_model` export.

## Is the instrument still worth carrying at all?

Partly. Its original target was the underconfidence gap, whose dominant cause
turned out to be the forecast-SIGN keyword-scan bug (fixed fa2bea9). But not
all of it was aimed at the wrong target:
- The provenance ceiling / requirement gate / inheritance clamp genuinely do
  subtract real points, and the bridge still halves certainty
  (p = 0.5 ± conf/2). Attribution of those remains live diagnostic value.
- `sign_of_prediction()`'s keyword-comparison mode is now purely historical
  damage-sizing; once that sizing is recorded it can be retired, but it costs
  nothing and documents the defect honestly.
Verdict: keep the live-run instrument (master's), keep wrap_model. The
offline-reconstruction layer (mechanisms.py + batch-row guessing) is the part
whose honest answer is retirement.

## Merge plan

1. Merge origin/build/dd-decomposition-diversity into epistemics worktree
   (branch build/dd-instrument-decision), resolving tools/calibration/* by the
   decision above — real three-way content merges, no -X ours/theirs.
2. Wire wrap_model into master's instrument.py; update __init__ exports;
   reconcile ab_axes/diagnose imports or retire them with their tests noted
   in this file.
3. Run the 19 money red-team tests (exist only on the branch).
4. tests/test_lifecycle_claim.py: expect 2 master failures resolved.


## FINAL RESULTS (post-merge, this branch)

Merge landed. Full suite: 76 failed / 11,567 passed / 8 skipped
(xgboost/ml collection errors excluded — environmental; master baseline 30-34
failures on the same exclusions).

### The 19 money red-team test files: RUNNING for the first time
17 branch-only money/red-team files (197 tests): 163 pass, 34 fail.
Classification of the 34:
- ~27 fail identically on origin/build/dd-decomposition-diversity itself
  (pre-existing repros pinning still-open defects).
- 3 K1 calib-scoring failures are GOOD NEWS: they pin the ground-truth
  fabrication in _implied_outcome that MASTER already fixed (fail-closed,
  unknown = excluded). Repros now stale.
- 3 retr_selection_nulls failures: master's single membership rule classifies
  mixed error+rejection as honest_null WITH partial-coverage disclosure;
  branch pinned fail-closed RETRIEVAL_FAILURE. Genuine semantic collision,
  resolved toward master's disclosed-mixed rule. Needs owner sign-off.
- 1 forged-amendment failure is INTENTIONAL: score()'s default basis is now
  the sealed ORIGINALS (per preregistration.py's own contract); amendments
  apply only when explicitly passed. The forged-amendment attack is dead on
  the default path.

### Lifecycle claim tests: 2 failures -> 0 (verified)
- amendment default-basis fix in agp/preregistration.py
- content-bound journal chain in agp/claims.py (also converts two C2
  forged-chain repros to fix-pins)

### Other resolutions
- engine.py: concurrent leaves + crossrun memory + full-gate checkpoints +
  admissible-checkpoint replay combined; lost result.fetches.extend restored.
- synthesis.py: per-class corroboration accounting kept (F5/F4c).
- retro.answer(): handles running-loop and no-loop callers.
- instrument decision executed as specified above.
