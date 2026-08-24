# REVIEW RUN 2026-08-23 (staleness) — reviewer: ox-alpha, standing role

Branch `review/rotating-0823-225544` @ 8123b86 (+c8fe8aa by a concurrent
reviewer). No production code edited.

**Under review this run** (chosen from the coverage map below — nobody had
touched these):

1. `build/source-staleness` @ ee6db37 — UNMERGED, 5 commits, 657 lines:
   persisted per-source health history (`tools/sources/staleness.py`),
   null-classifier amendment (`tools/gaps.py`), degraded-source disclosure
   at conclusion time (`tools/pipeline/engine.py`). Its sibling
   `build/source-health` and the information-gain/stasis merges were taken
   by concurrent run b minutes into this run; staleness was not.
2. **Meta-check of that concurrent reviewer's own fresh commit (c8fe8aa,
   "run b")** — reviews reviewing reviewers is this role's whole point.

Families hunted: **1** (a verification layer whose input never arrives),
**3** (absence treated as success), **6** (direction of error), with a
family-2 sweep of every other consumer of health status.

## Coverage map (who reviewed what — maintained for future runs)

Covered by prior runs 1–9 + rotating runs + concurrent run b: master-side
audit fixes (C1/C2/C3/C4, W5, D2, floor_conf, sidak scope), money path +
speed runs 7/8, fix fleet a6/a14/a2/b1, derived-analysis-loop,
information-gain/stasis, source-health, finance plugin inertness.
**Never reviewed by anyone:** `build/hierarchical-planner` (committed
22:36 tonight, 6 commits), `build/dd-decomposition-diversity` unique tail
(04:00 today: kelly/sizing poisoned-input hardening d638260, W-seam and
calibration redteams), `build/polymarket-domain` (8/22 evening), and the
staleness branch below. Next runs should take these in that order.

## WHAT HOLDS UP

- **The store semantics are genuinely good.** OK→DEGRADED preserves
  `last_ok`; corrupt JSON degrades to empty rather than fabricating
  health; atomic tmp+os.replace writes; BROKEN-streak counting and reset
  all behave as documented. All 13 of the branch's own tests pass on the
  branch bytes.
- **The engine disclosure wiring is real**, not another dead check:
  `session.sources` exists (engine.py:559), `coverage_note()` text lands
  in both `result.notes` and the assembled conclusion, and nothing in
  either path moves a confidence score (the branch's central claim).
- **Run b (concurrent reviewer) survives independent re-execution.** All
  five of its repros fail exactly as reported (R1 refine_query-dead,
  R2 epsilon-sigma ranking, R3 probe name mismatch, R4 inert finance
  plugin, R5 duplicate membership rule), and its claim-vs-code table
  matches my reads of the same diffs. Two reviewers, two branches, no
  contradictions.

## WHAT DOES NOT — ranked

### S1 · HIGH — the staleness amendment cannot see its stated target case (family 1: the check wired where its input never arrives)

`classify_null_kind` consults health history ONLY in its fall-through
branch — reachable only when a round has NO rejected, NO admitted, NO
error and NO skipped entries. But the module's entire reason to exist is
"source returns HTTP 200 with zero results" — and through the real
retriever that response is REJECTED at the relevance gate with a reason
(`retrieval.py:588-596`), which takes the FIRST branch
(`reachable_attempt`) and returns honest_null without ever importing
staleness. A trace shape `IterativeRetriever` emits can reach the
amendment only by accident; the integration test in
`tests/test_source_staleness.py` passes because it hand-builds
`rounds=[{"sources":[{"name":"gdelt"}]}]` — no rejected list, no error
keys, an impossible shape (family 7: hand-picked input that never touches
the failing boundary). The findings file's end-to-end claim ("empty
response from a stale source → gap_kind = retrieval_failure") is not
backed by any committed test driving the real pipeline.

Proof (detached worktree @ ee6db37, `tests/test_review_staleness_repro.py`):
realistic gate-rejected trace + clinicaltrials STALE in history →
`classify_null_kind` → `honest_null`. Control with the hand-built shape →
`retrieval_failure`, proving the rule works when reached; S1 is pure
reachability. Repro committed here as xfail(strict) in
`tests/test_review_20260823_staleness.py`.

Fix direction: amend on BOTH honest-null exits (pass `rejected` names and
round-touched sources into one call placed before the first return), or
compute touched-names via `source_names_from_trace` (already written on
this branch, then never used — itself a small family-1 fossil).

### S2 · MEDIUM-HIGH — a SKIPPED probe demotes HEALTHY to STALE, manufacturing the exact evidence the classifier consumes (families 3+6)

`SourceHealth.status` order is: last_verdict=="OK"→HEALTHY;
last_ok→STALE; verdict set→NEVER_OK. `record(SKIPPED)` overwrites
`last_verdict` while keeping `last_ok`, so any once-healthy source probed
while its key is unset flips HEALTHY→STALE. That is not hypothetical:
`health.main()` persists by default, `run_all` emits SKIPPED for every
unconfigured key env, and this machine currently has 403'd/unkeyed
sources. Consequences cascade exactly as designed — except now they fire
on fabricated staleness: honest nulls leaning on that source flip to
RETRIEVAL_FAILURE (S2 e2e repro), and SOURCE COVERAGE WARNINGs enter
sealed conclusions. The docstring says "an untested source is not a
failing one"; the findings file says "SKIPPED probes change neither
counter". Counters yes — verdict no. Their own test covers only
BROKEN→SKIPPED (NEVER_OK stays NEVER_OK); the HEALTHY→SKIPPED transition
is untested and broken. Direction of error: absence of evidence became
evidence of failure — the one direction this codebase is never allowed
to choose.

Proof: two xfails + passing control in
`tests/test_review_20260823_staleness.py`; demonstrated on branch bytes.

Fix direction: on SKIPPED, do not touch `last_verdict` (or derive STALE
only when last_verdict in {DEGRADED, BROKEN}).

### S3 · LOW — verification claims without artifacts

The findings file claims two end-to-end verifications with the real
pipeline; the committed suite contains neither (13 tests, none e2e).
Whatever was run manually is not re-runnable, which is how "62 tests
passing" happened before. Ship the artifact.

## Verdict on the branch

Merge-worthy AFTER S1/S2 — the persistence layer and disclosure are
sound and honestly scoped, but as merged today the flagship feature
("the null classifier finally has evidence") would be a verification
layer that cannot receive its input, plus a maintenance routine that can
poison its own evidence store in the healthy→skipped transition. Both
are small diffs. Do not wire the probe into scheduled maintenance until
S2 lands, or every keyless probe cycle degrades classification quality
system-wide.

Artifacts: `tests/test_review_20260823_staleness.py` (2 xfails strict +
1 xfail e2e + 2 passing controls, import-guarded until merge);
branch-byte proofs in throwaway worktree `/var/folders/.../opencode/ss-wt`
(test file `tests/test_review_staleness_repro.py`: 3 failed / 2 passed,
branch's own 13 still green).
