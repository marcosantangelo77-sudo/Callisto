# Retrodiction smoke — 2 questions (2026-08-23)

Cheap end-to-end proof that the retrodiction path runs after three recent,
untested-together fixes (f1f68e1 event-loop seam · ee549f8 `_implied_outcome`
truth-only · fa2bea9 declared stance). **This is NOT a measurement** — 2 rows
say nothing about accuracy, and any brier read off them is noise.

## Run

- `CALLISTO_CUTOFF_KEY` set from `~/callisto-wt/.harness_key`.
- `python3 scripts/run_retro_batch.py --questions data/retro_batch/questions.json
  --limit 2 --label smoke2 --checkpoints /tmp/retro_smoke2_cp
  --results data/retro_batch/results_smoke2.jsonl --report data/retro_batch/report_smoke2.json`
- Elapsed: 74.0s (Apple) + 72.4s (Nvidia) ≈ 2.4 min total. Far inside the 90-min box.
- Zero exceptions / zero tracebacks in the log.

## Result: the path RUNS END TO END. No break.

| check | outcome |
|---|---|
| reached sealed or refused without exception | yes — both `status=scored`, refused to seal fail-closed (`refusal_reason: confidence 0.0 below DB floor 0.3 after adversary penalties`) |
| real answer_binary on rows | yes — both carry ground truth (`true`) from questions.json |
| calibration table emits n_no_truth | yes — all bins present, `n_no_truth: 0` |
| stance AFFIRMS/DENIES/UNDETERMINED (not keyword-scan) | yes structurally — but see caveat: both landed UNDETERMINED via the refusal path |

## Caveats observed (not fixed, per task scope)

1. **Both questions took the REFUSAL path**, so the AFFIRMS/DENIES branch of the
   stance code never executed this run. All sub-question retrievals failed
   (n_fetches=0; semanticscholar/wikidata 404s; terminator at max_iterations),
   adversary penalties drove confidence to 0.0, below the DB floor → refuse.
   Fail-closed behaviour is correct, but note `engine.py` assigns
   `result.stance = parent_stance` only AFTER the seal gate (~line 720) — a
   refused run keeps the dataclass default UNDETERMINED. That is honest here,
   but it means the smoke exercised p=0.5/UNDETERMINED, not a directional
   forecast. The AFFIRMS→p>0.5 / DENIES→p<0.5 mapping remains unexercised by
   any live run.
2. **Resume layer footgun**: the first launch used the DEFAULT checkpoint root
   and silently skipped both questions ("22 already complete") because stale
   checkpoints from an earlier batch live in `~/.local/state/callisto/checkpoints`,
   and `load_completed()` also treats any existing results JSONL as resume
   state — my fresh results file got repopulated with the OLD 22 rows. A smoke
   must pass BOTH a fresh `--checkpoints` dir AND a fresh results filename.
3. Report `failures.refusals` lists only rows with `status == "refused"`; these
   rows are `status == "scored"` with a populated `refusal_reason`, so they do
   not appear in the failures section. Cosmetic disclosure gap only.

## Verdict

The three fixes compose: no event-loop seam error, no truth imputation
(n_no_truth emitted, 0), stance plumbing intact end to end. Green light to
build on this harness. Accuracy claims remain forbidden until the full 22 runs.
