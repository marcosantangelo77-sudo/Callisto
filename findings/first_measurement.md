# First Honest Calibration Measurement — retro_batch full22

**Date:** 2026-08-24 · **Branch:** improve/money-path-landing (worktree callisto-wt/research)
**Harness fixes in effect:** f1f68e1 (sync/async seam), ee549f8 / K1 (no self-imputed truth), fa2bea9 (declared AFFIRMS/DENIES/UNDETERMINED stance), plus two defects found and fixed **during** this run (below).

## What ran

All 22 questions from `data/retro_batch/questions.json`, each through the real
pipeline (Hermes CLI model, live source registry, adversary, seal gate),
scored against the stored `answer_binary`. Resumable batch runner
(`scripts/run_retro_batch.py`). A 5-question validation pass preceded the full run.

**Every row carried a real `answer_binary` (22/22). Nothing was imputed.**
The K1 fix holds: `_implied_outcome` returns None without a stored truth, and
the report's `n_no_truth` column would have shown it.

## Two new harness defects found by this run (fixed before trusting numbers)

1. **Stale-checkpoint false resume.** My first launch "completed" 22 questions
   in 0 seconds. The shared default checkpoint store (`~/.local/state/callisto/checkpoints`)
   held 22 `retro_batch` checkpoints keyed on (question text, domain, claim_date)
   — produced 2026-08-22→24 by earlier worktrees running the PRE-fa2bea9
   keyword-scan code (19 of 22 pre-date the declared-stance commit). The runner's
   resume path is content-keyed and cannot know which code produced a payload.
   Fix for measurement: isolated state dir (`CALLISTO_STATE_DIR`), fresh run.
   The old rows are retained as history but are NOT this measurement.

2. **Loop-bound process semaphore poisoned every question after the first.**
   `hermes_cli.proc_semaphore()` cached one `asyncio.Semaphore`; each question
   opens its own event loop, so question 2+ raised "Semaphore is bound to a
   different event loop" → all-error rows (exactly the shape of the original
   smoke5 disaster, second cause). Fixed: semaphore re-created when the running
   loop changes; per-process concurrency cap preserved.

Also raised `HermesCliModel` timeout 300s→900s (free tier).

## THE NUMBERS (fresh 22-question run, no tuning)

```
n_total = 22        n_scored = 22       null_rate = 0.0
rows with real answer_binary = 22/22 (100%)

mean_brier = 0.250000   (95% CI 0.25–0.25)
brier_decomposition: reliability 0.0517 · resolution 0.0000 · uncertainty 0.1983
sealed_rate = 0.2273 (5/22)
mean_elapsed_s = 169.3 (range 65–359 s/question; ~62 min wall clock for the 17 fresh)

calibration_overall (with n_no_truth):
  bin [0.4,0.6): n=22  mean_p=0.5000  realised=0.7273  n_no_truth=0
  all other bins: n=0

beat-market (19 rows with market_implied):
  beat-market rate = 3/19 = 15.8%
  mean directional edge = −0.1911  (negative = worse than market)

honest-null / retrieval-failure split:
  status 'error':            0
  unsealed (refusal_reason): 17/22
    - "every leaf came back unanswered": 11   ← retrieval failure, honest null shape
    - "confidence X below DB floor 0.3 after adversary penalties": 2
    - "adversary veto: adversary backend failed": 2
    - (2 sealed-but-uninformative rows carry p=0.5 anyway)
```

## Verdict — read this part

**This is not a calibration measurement; it is a degenerate one, and that is
the finding.** All 22 predictions came out at exactly p=0.5:

- UNDETERMINED stance → p=0.5 by construction (fa2bea9's honest mapping).
- 17/22 runs never sealed: retrieval returned nothing answerable
  ("every leaf came back unanswered") or confidence fell below the floor.
- The 5 sealed rows also landed at p=0.5 (conf≈0 ⇒ 0.5±conf/2 ≈ 0.5).

So mean_brier = 0.25 with zero variance, resolution = 0.0, and a single
occupied calibration bin. The bin says realised YES rate is 0.727 against a
constant 0.5 forecast: a coin that always says 0.5 scores 0.25 while reality
ran 73% YES. Brier decomposition confirms: miscalibration is small
(reliability 0.05) only because there is nothing to calibrate; signal is
exactly zero.

Beat-market rate 15.8% vs the ~50% an honest coin achieves against devigged
markets: the pipeline carries no information about these questions.

## Root cause (not tuned, reported)

The live source registry cannot answer these questions' leaves. The evidence
chain bottoms out in keyword-matched academic corpora (OpenAlex etc.) plus
hard-failing sources (federalregister 500s, semanticscholar 403, courtlistener
429) — the same failure mode the adversary objected to in every legacy row:
"zero items contain any Apple figure." The gate then honestly refuses to seal,
and the declared-stance bridge honestly maps refusal to p=0.5. Every number
above is what an honest harness produces when the retrieval layer is empty.

## Artifacts

- data/retro_batch/results_full22.jsonl (+ report_full22.json) — THIS measurement
- data/retro_batch/results_fresh5.jsonl (+ report_fresh5.json) — validation 5
- data/retro_batch/results_smoke5_live.jsonl — DO NOT USE: resumed stale
  pre-fix checkpoints (kept as the false-resume repro)
- Fixes: routes=None transport restore (5601cda), semaphore-per-loop,
  timeout 900s.

No threshold, floor, or scoring parameter was adjusted to improve any number.
