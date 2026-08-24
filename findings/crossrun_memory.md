# Cross-run memory — GAP 2 closed (smallest version that compounds)

GAP 2 from `findings/prior_art_survey_salvaged.md`: Skywork's gated distiller
persists what a run learned and auto-loads it next session. Callisto had memory
modules but nothing carrying what a run LEARNED into the next one — every run
started cold, re-deriving which sources were useless for this kind of question,
re-erroring against broken endpoints, re-spending identical wasted fetches.
That redundancy is visible in the owner's token ratio.

## What was built

Three pieces, all building on existing infrastructure (no new persistence
engine — the JSONL append-only convention of `tools/routing/scores.py`, state
off OneDrive via `CALLISTO_STATE_DIR`):

1. **`tools/pipeline/crossrun.py`** — `CrossRunMemoryStore` (append-only JSONL,
   torn-line tolerant, thread-safe), `record_run()` (the structured end-of-run
   fact record) and `PlanningView` (the ONLY shape a live run can see).
2. **`tools/pipeline/retrieval.py`** — `IterativeRetriever(source_order=...)`
   seam: an order-only re-rank applied to each round's fan-out candidates
   before any fetch, defensively validated as a permutation.
3. **`tools/pipeline/engine.py`** — `ResearchPipeline(crossrun_store=...)`:
   `run()` now wraps `_run_inner()`; loads the question class's records at
   start, collects each leaf's `RetrievalTrace`, persists one record at end,
   appends the briefing line to `result.notes`.

### The record (facts, not prose)

Per run: `question_class`, per-source counts (`admitted` / `rejected_gate` /
`errored` / `skipped`), `gap_kinds` per leaf (honest_null / retrieval_failure
/ unprovable / ""), final `stance` and `tier`, `sealed`, `refusal_reason`,
`n_fetches`, and `root_query_sha256` (audit only). No evidence bodies, no
conclusions, no query text ever stored.

### Question class

Reuses `tools.task_classifier.classify_query` (the orchestrator/routing store's
existing bucket vocabulary). Coarse by construction; a different question of
the same kind shares the bucket.

## HARD CONSTRAINTS — enforced structurally, tested in `tests/test_build_crossrun_memory.py`

| Constraint | Enforcement | Test |
|---|---|---|
| Memory informs WHAT/ORDER only | `PlanningView.order_specs` is a stable partition moving chronic-null sources to the BACK; retriever validates permutation, degrades to registry order on any fault | `test_order_specs_is_stable_partition_never_drops`, `test_retriever_ignores_a_malformed_order_hint` |
| Never informs confidence | `PlanningView` physically has no stance/tier/conclusion/evidence attributes; nothing else crosses the seam | `test_planning_view_physically_lacks_confidence_material` |
| Remembered fact ≠ evidence (R5 escalator stays dead) | Poisoned records claiming `stance=AFFIRMS tier=VERIFIED` change nothing vs a memory-less run; evidence-set size identical | `test_poisoned_memory_cannot_move_the_conclusion` |
| Per CLASS, never per question | Store lookup filters on class string ONLY; question text stored only as truncated SHA | `test_question_class_is_coarse_not_per_question`, `test_memory_shared_across_questions_of_same_class`, leak assertions in `test_record_run_…` |
| Chronic null needs ≥3 runs; fragile is FLAGGED not reordered | `DEPRIORITISE_MIN_RUNS=3`, sliding window of last 10 records so sources can redeem | `test_chronic_null_threshold_three_runs_not_two`, `test_window_ages_out_old_evidence_and_redeemers`, `test_fragile_flagged_not_deprioritised` |

Deliberately NOT done: no memory in decomposition prompts (that path could
carry remembered stances into reasoning — an R5 rebuild); no exclusion of any
source; no confidence, prior, or threshold touches anywhere.

## JOB 3 — the two-run proof

Same question, same fixtures, deterministic scripted model, one leaf needing 2
independent sources. Fan-out order: openalex (returns junk → gate-rejected),
gdelt (endpoint down → error), federalregister, clinicaltrials (both admit).
Store pre-seeded with two prior chronic-null runs of this class.

```
[cross-run compounding] wasted fetches run1=3 run2=1
```

| Metric | Run 1 | Run 2 |
|---|---|---|
| Wasted fetches (gate-rejected + errored) | **3** | **1** |
| Fetch attempts | 5 | 3 |
| Admitted evidence | federalregister, clinicaltrials | federalregister, clinicaltrials |
| Sealed | yes | yes |
| Stance / tier / confidence / leaf answers | X | **identical** |

Run 1 burned round 1 on openalex+gdelt and re-tried openalex in round 2 before
reaching sufficiency. Run 2 loaded the class records (openalex now 3 chronic
nulls → deprioritised to the back of the fan-out; gdelt flagged fragile),
filled its budget with productive sources first, reached 2 independent voices
in ONE round, and never touched openalex. Conclusion byte-identical: same
admitted set in the same order, same stance/tier/confidence, same seal.

Run 2's remaining 1 wasted fetch is gdelt erroring once: fragile sources are
disclosed in `result.notes` ("fragile (retrieval failures): gdelt …") but are
still fetched — flagging, not silent blacklisting. Deprioritisation is order
only; if budget allows, a disliked source is still tried.

## Verification

- New tests: 13 passed (`tests/test_build_crossrun_memory.py`).
- Full suite: failure sets byte-identical with and without this change on a
  clean worktree at the pre-change commit (38 pre-existing environment
  failures here — xgboost collection errors, backtest_e2e, artifacts-store
  redteam — none touching the pipeline; the owner's quoted baseline of 21 was
  measured on a different machine). Zero regressions, +13 passing tests.

## Known limits (honest)

- Under checkpointing, resumed leaves rebuild traces without per-round source
  detail, so their records carry admits but not rejected/errored granularity.
- Class buckets are keyword-coarse; a misclassified run pollutes its wrong
  bucket exactly as task budgets always have (tolerated, disclosed).
- Records grow unbounded per class; window bounds READS but not the file.
