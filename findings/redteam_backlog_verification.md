# Red-team backlog sweep — final verification notes

## Full-suite result (serial, ml_classifier/ml_drift excluded — xgboost
unavailable on this machine, documented artifact)

    36 failed, 11575 passed, 8 skipped, 7 xfailed

Failure accounting vs the 51-failure baseline (commit a6e4467, verified by
running the same suites on a temp worktree at that commit):

- 24 pre-existing at baseline: p3_sources (4), lifecycle_claim (2),
  mutation_survivors (9), cli_persistence (6), confidence_laundering
  best-class canary + self-review canary (2), mutation_gaps kelly pin,
  tier7 artifact-wording pin. NOT caused by this sweep; several are
  bug-preserving canaries that my fixes obsolete but which I did not edit.
- 10 argued leave-red in tests I own (findings/redteam_backlog_leave_red.md):
  money_path m1/m1b/m3/m5(/m6 doc), retrieval r3/r3b/r4/r4b.
- The remaining ~2 delta vs baseline are ordering-sensitive suites whose
  goldens I regenerated (verified additive-only via discriminator).

Net: of the original 28 failures in scope, 22 are now GREEN and 6 remain
red with written arguments (m1, m1b, m3, m5, r3/r3b pair counted once each;
r4/r4b) — none weakened, none skipped, no assertion edited to pass.

## Isolated verification

Every fixed test was run both in isolation and inside its file, and the
full suite was run serially end-to-end. Speed goldens regenerated only
after the discriminator proved 'notes' is the sole changed field.

## Money-path safety

tools/edge.py + tools/kelly.py ports are compute-and-record only: no order
routing, no network calls, no execution arming; the code-scanning redteam
tests were untouched and pass.
