# CALIBRATION & RETRODICTION HARNESS — improvement pass (build/cli-front-door)

**Area chosen: the calibration and retrodiction harness** (tools/retrodiction/
+ the pipeline bridge tools/pipeline/retro.py).

Why this one: NEXT.md §1 ranks it highest value ("the only honest way to
compare synthesis strategies") and no improve run has covered it — CLI was
done twice (improve_cli, improve_cli_run_persistence); retrieval (w1/P1),
synthesis (i3), checkpointing (w3), provider routing (oxalpha), schema seam
(p2) are all taken. This harness is what turns runs into a scored track
record; everything else in the system is judged by numbers only it can
produce.

## What was wrong — measured

**The headline defect: the batch runner has never worked against the real
researcher.** The shipped smoke run (data/retro_batch/results_smoke5.jsonl)
is 5/5 rows of:

    RuntimeError: This event loop is already running

Root cause: `RetrodictionBatch._run_one` executes inside `asyncio.run()`,
but `PipelineResearcher.answer()` — the only real researcher — calls
`run_until_complete` on that same running loop. Every live batch question
was an error row. The unit tests never caught it because every test uses a
plainly-sync stub researcher; the one researcher that matters has a shape
no test exercises. Same class of failure as MORNING_REPORT's "no unit test
caught SourceRegistry": component-local vocabulary in tests, reality
different.

A second latent bug under the first: even off-loop, `answer()` used
`asyncio.get_event_loop()`, which raises in a fresh worker thread.

**Structural gap: nothing in the harness says whether a difference between
two configs is real.** `run_ab` reported two mean Briers; a 0.24 vs 0.26 gap
on 20 questions is indistinguishable from noise, and the verdict string
("better than chance") carries no uncertainty either. An accuracy harness
whose own conclusions lack confidence statements fails the property it
exists to enforce.

## What changed (2 commits)

**f1f68e1 — the sync/async seam.**
- `batch._call_researcher`: async-native researchers awaited directly;
  sync researchers executed via `asyncio.to_thread` so they may freely own
  their own event loop.
- `retro.PipelineResearcher.answer`: creates/closes its own loop instead of
  `get_event_loop()` — correct on the main thread and in any worker thread.
- Verified end-to-end with the REAL bridge (`PipelineResearcher` over a
  fake model): 3/3 questions scored where the identical shape previously
  produced 5/5 errors.

**e358579 — significance testing, pure Python (repo has no numpy/scipy).**
- `paired_significance`: exact-ish paired permutation test (10k sign-flips,
  deterministic seed) on mean Brier difference between two configs.
  Attached automatically by `run_ab` when exactly two arms run; surfaced in
  `RunResult.summary()` and rendered reports.
- `brier_decomposition`: Murphy decomposition reliability − resolution +
  uncertainty. Distinguishes honest-but-uninformative (reliability≈0,
  resolution≈0) from miscalibration-dominant — invisible to the raw score.
- `bootstrap_brier_ci`: percentile bootstrap CI on mean Brier.
- Batch report now prints `brier_ci95`, the decomposition line, and plain-
  language diagnosis ("honest but uninformative" / "miscalibration
  dominates").
- tests/test_build_retro_hardening.py: 13 tests, including a regression
  test that reproduces the loop-owning researcher inside the async batch.

## Before/after numbers

| measure | before | after |
|---|---|---|
| live-shape batch (sync PipelineResearcher in async batch) | 5/5 error rows | 3/3 scored (verified end-to-end) |
| A/B comparison | raw means only | + p-value, better arm, significance flag |
| headline Brier | point estimate | + 95% bootstrap CI |
| failure-mode visibility | none | Murphy decomposition w/ diagnosis |
| retrodiction-area tests | existing suites | +13 |
| full suite | 2047 passed / 17 failed* | 2047 passed / 17 failed (same set) |

\* backtest_e2e ×11, adaptive_timeout ×4, claude_findings, prop_scanner —
byte-identical to the documented pre-existing set (improve_cli.md).
4 collection errors are environment gaps on this Mac (fastapi, joblib not
installed) and were excluded, not counted. Sports stays green.

## What I deliberately did not do

- Did not add numpy/scipy for stats — three small functions did not justify
  a new dependency.
- Did not touch magnitude scoring (already matches NEXT.md's CLV-generalised
  design), the cutoff enforcer (adversarially verified in wave R1), or
  question generation.
- No new CLI surface for batches; `scripts/run_retro_batch.py` picks up the
  fixes without modification.

## Honest caveats

- The permutation test is paired and two-sided; with <~10 shared questions
  it will almost never reach p<0.05 — that is the honest answer at that N,
  and the test says so rather than inventing a winner (asserted in tests).
- `_implied_outcome`'s brier-inversion fallback in batch reporting remains
  fragile for resumed rows lacking answer_binary; I added answer_binary to
  reconstructed questions where available but did not restructure it.
- The smoke-run errors in data/retro_batch/ were left in place as historical
  record (append-only results policy); rerunning the script will supersede
  them per the resume logic.
