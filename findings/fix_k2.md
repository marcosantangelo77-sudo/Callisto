# FIX K2 — routing store: question identity + coverage gate

**Property enforced:** a comparator must not reward selective participation.
No tuned formula: the implementation is dedup on question identity, plus a
coverage comparison in `decide()` that excludes a lower-coverage candidate
from winning. No confidence score was raised anywhere; no file outside
`tools/routing/` and the new test file was touched.

## Defect 1 — duplicates count (K2.1)

`ModelScoreStore.aggregate()` now groups records by `question_id` (latest row
wins) before computing n, means, shrinkage, or cost. One question recorded
100x is one observation: n=1, `basis="sparse"`, with
`duplicate_rows_ignored` reported for visibility. Volume can no longer
substitute for breadth. A re-recorded question is a correction that
supersedes, never a second vote.

## Defect 2 — the selection loop (K2.2, acceptance repro)

The red team's exact setup, 500 Thompson decisions, seed 1:

- cherry-picked model: 10 distinct easy questions, brier 0.01 each
- honest model: 30 distinct questions, brier 0.24 each

| | cherry wins / 500 |
|---|---|
| before fix | **500/500** |
| after fix | **0/500** |

Mechanism: `decide()` counts each candidate's DISTINCT questions answered on
the judged slice. Any candidate below the best coverage among candidates is
excluded from winning that decision outright (`coverage_gated: true`,
coverage counts returned in `scores_used`). Its subset mean may look heroic,
but it is not evidence about the questions it skipped — and under the old
comparator those skipped questions were invisible, which is what made the
loop exploitable.

Why exclusion rather than a penalty: any additive penalty is a constant that
can be outbid by an extreme subset score (first attempt: a chance-centred
exploration draw still let cherry win 230/500). The property "must not reward
selective participation" is enforced exactly by making partial participation
disqualifying, with zero parameters.

Equal-breadth models compete purely on quality — covered by
`test_equal_coverage_competes_normally`.

## Defect 3 — task_class stored but never read

Already fixed on this branch by the earlier routing pass (commit 9ecffbb):
`decide()` judges each candidate on its `(role, task_class)` slice via
`_records_for()`. The K2 red-team test pinning this as a bug
(`test_K2_decide_ignores_task_class`) describes pre-W8 code; the fixed-
behaviour pins live in `tests/test_improve_provider_routing.py::
TestTaskClassScoping`. Nothing further to do.

## Tests

New: `tests/test_fix_k2_routing_coverage.py` (5 passing) — duplicate rows
count once; latest-per-question correction semantics; the 500-decision
acceptance repro at 0 wins; gate visible in `scores_used`; equal breadth
competes normally.

Routing suites: `test_improve_provider_routing.py` +
`test_build_w2_empirical_routing.py` 29 passed. Full local suite unchanged:
the only failures are the 3 pre-existing baseline failures outside routing
(`test_w3_poisoned_answer_checkpoint`, `test_w4_resume_counts_own_sandbox`
— engine.py owner's work in flight; `test_neg_floor_conf_never_raises`
— imports from unmerged build/dd-decomposition-diversity), verified failing
on HEAD without this change.
