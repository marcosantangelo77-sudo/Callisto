# verify-merge.sh blind spot — the guard missed its own check

2026-08-24 · branch fix/verify-merge-blindspot (commit 1d50911)

## The claim, verified

`tests/test_redteam_answer_correctness.py` (487 lines — the reproductions for
the arithmetic contradiction and the composition bug) was deleted from the
pm1/sm1 line that is now origin/master by an **autosave commit**, and
verify-merge.sh reported PASS on every merge afterward. Confirmed by running
v1 against the actual bad merge in a detached worktree.

## Where the deletion actually happened

- db08c13 "Land review/ox-alpha-0824b…" added the file (487 lines) plus three
  review suites and six findings docs to master's line.
- cd7f068 "autosave: in-flight work on fix/worldbank-planner" — cut from a
  base that had db08c13 but committed by the autosave daemon with an older
  working tree — DELETED all of it: 4 test files, 6 findings docs,
  scripts/discriminate_goldens.py, and reverted the C5 reconciliation block
  in tools/pipeline/engine.py (1280 → 1165 lines).
- Merge 32ac69e ("Merge remote-tracking branch 'origin/fix/worldbank-planner'
  into pm1") carried those deletions onto the pm1→sm1→master line.
- Later merges re-added only test_redteam_answer_correctness.py (via sm1's
  retrieval-starvation merge, now 508 lines). As of this writing on
  origin/master these are STILL MISSING:
  - tests/test_review_0824_audit.py (272 lines)
  - tests/test_review_0824_run3.py (213 lines)
  - tests/test_review_2026-08-24.py (311 lines)
  - findings/arithmetic_contradiction.md, arithmetic_goldens.md,
    redteam_answer_correctness.md, review_0824_run2.md,
    review_2026-08-24.md, review_2026-08-24_run3.md
  - scripts/discriminate_goldens.py

## Why the guard was blind — three compounding defects

A. **Hardcoded cd.** Line 10 did `cd ~/Documents/GitHub/Callisto`. Any
   invocation from a worktree — including merge-train.sh, its primary
   production caller — silently graded the MAIN CHECKOUT's tree, not the
   branch just merged. Proven by xtrace: run at the real bad merge, the
   guard's loops iterated the main repo's file list.
B. **Wrong base for merges.** `PREV=${1:-HEAD~1}`; for a merge commit M,
   HEAD~1 = M^1^1's side — skipping the entire first-parent diff. The
   correct pre-merge base is M^1 (first parent).
C. **Vanished-test check compared one commit, not trees.** It built its
   baseline from `git show $PREV --name-only` — files touched by ONE COMMIT.
   For any single-commit base that list contains no deletions, so the comm()
   found nothing. The ls-tree loop that followed could have caught it, but it
   ran against the wrong repo (defect A) and, for merge bases chosen per B,
   against a base that predated the files.

Additional finding: whole-module source deletions were skipped entirely
(`[ -f "$f" ] || continue`) — the exact "stale branch buries newer work"
failure class check #3 exists for.

## Reproduction

`tests/guard/repro_harness.sh` builds scratch histories for five scenarios
(direct delete / merge-carried autosave deletion / rebase drop /
public-function deletion via merge / >25% shrink) and asserts old-miss,
new-catch on each. 5/5 as designed. Running v1 directly at merge 32ac69e:

    ── deleted public functions (vs HEAD~1)   ok
    ── vanished test files                    ok     ← 487 lines missing here
    ── source files that shrank >25%          ok
    MERGE GUARD: PASS

## The fix (verify-merge.sh v2, commit 1d50911)

1. No hardcoded cd. Runs where invoked (`git rev-parse --show-toplevel`).
2. Base selection: first parent (HEAD^1) for merge commits, HEAD~1 otherwise;
   explicit arg still wins.
3. Vanished-test check compares full PREV trees: every tests/**/*.py in PREV
   must exist in the working tree. Direct commit, merge, or rebase all land
   here because the comparison is tree-vs-tree.
4. Whole-module deletions (>100 lines) now fail instead of being skipped.

## Guard self-tests

`tests/guard/test_verify_merge_guard.sh` — 7 assertions over the new guard
itself (direct delete, merge-carried delete, merge carrying deletions fails
the gate, >25% shrink, whole-module deletion, clean tree passes). All pass.

## Action items (not done here, flagged)

- Restore the four still-missing artifacts listed above from cd7f068^ / db08c13
  onto master — deliberate commits, not autosaves.
- merge-train.sh should export BASELINE_IMPORTS per-car and invoke the guard
  from $R (it already cds there; v2 makes the guard honor that).
- The deeper disease remains: the autosave daemon commits stale working-tree
  snapshots that revert newer work (review run 12 family 10). A guard catches
  the aftermath; only branch-drift detection before merging prevents it.
