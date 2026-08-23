# verify-merge.sh — landing the checkpoint resume re-gate fix on master

Date: 2026-08-23
Branch: land/ckpt-resume-gate (fast-forward target: master @ 102f319)
Fix source: a5292e5 "checkpoint: restore the relevance gate's full verdict set
on resume" (tools/pipeline/engine.py +47, tests/test_fix_ckpt_confidence.py +228)

## Raw output (first run, against clean HEAD + fix changes)

```
── deleted public functions (vs HEAD)
  ok
── vanished test files
  ok
── source files that shrank >25%
  ok
── imports resolve
  ✗ tools.ml_backtest: MISSING FIRST-PARTY ml_classifier

MERGE GUARD: FAIL — do not push
```

## Triage of the import failure (NOT merge damage)

`tools/ml_backtest.py` does `import tools.ml_classifier`; `ml_classifier.py`
exists and is tracked. Its own top-level `import joblib` fails because joblib
is not installed in the current interpreter. The guard's third-party filter
checks the *innermost* missing module name (`ml_classifier`) instead of the
root cause (`joblib`, which IS in its ignore list), so it misclassifies this
as first-party breakage.

Confirmed pre-existing via the guard's own baseline mechanism:

```
$ git stash   # clean master
$ BASELINE_MODE=1 bash ~/callisto-wt/verify-merge.sh HEAD
  ✗ tools.ml_backtest: MISSING FIRST-PARTY ml_classifier      <- same on CLEAN master
$ git stash pop
$ BASELINE_IMPORTS="$BASELINE" bash ~/callisto-wt/verify-merge.sh HEAD

── deleted public functions (vs HEAD)
  ok
── vanished test files
  ok
── source files that shrank >25%
  ok
── imports resolve
  ok (pre-existing ignored)

MERGE GUARD: PASS
```

No NEW failures vs baseline; all three structural checks pass.
Verdict: safe to push.

## Tests

- tests/test_fix_ckpt_confidence.py: 4 passed (incl. hypothesis property:
  resumed confidence never exceeds live confidence)
- tests/test_build_i1_integration.py: xfail(strict) REMOVED from
  test_no_checkpointer_is_byte_identical; suite passes non-strict-free (10 passed)
- tests/test_build_w3_checkpoint.py: 18 passed
- engine/pipeline neighbours (i3_synthesis, p1_findings, p1_pipeline,
  w1_retrieval): 53 passed
