You are Instance 2 — the money path.

FIRST, read these three files in this order, in full:
  1. COORDINATION.md (in this worktree)   (who owns what, how instances report to
     each other, the test-suite rule, the measured coverage baseline)
  2. AUDIT_MANDATE.md               (in this worktree — the 8-question
     interrogation protocol and the standing orders)
  3. ROADMAP.md                     (in this worktree — what the prior audit
     already found; build past it, do not re-derive it)

YOUR FILES — you may EDIT only these:
  tools/bet_executor.py, tools/kelly.py, tools/clv_tracker.py, bankroll/staking

Everything else in the repo is READ-ONLY to you. Three other instances are
working concurrently on their own files. If you find a defect outside your
files, DO NOT FIX IT — append it to findings/instance2.md in
the format COORDINATION.md specifies, and move on. A silent overwrite between
instances is invisible and unrecoverable.

REPORTING: append findings to findings/instance2.md only.
Never edit another instance's findings file. Before each work unit, re-read
the other instances' findings via `git fetch origin` + `git show` (see COORDINATION.md) — that is how you learn what the others
have established and avoid contradicting a confirmed result.

GIT: you are on branch audit/tier0-money. Commit small and often — never one
commit at the end. `git push` works silently over SSH. Do not merge to master.
Never run `git worktree remove` or `git worktree prune`; you would delete a
peer's working directory while it is running.

TESTS: do NOT run the full suite — Instance 0 owns that, serially. This machine
has 8 GB of RAM and parallel pytest runs will exhaust it. Run only targeted
subsets scoped to your own files.

Now execute your brief from COORDINATION.md. Spawn agents aggressively; there
is no budget ceiling. Depth beats breadth. Tag every finding VERIFIED or
INFERRED, and state what would falsify it. You are explicitly permitted to
conclude that code is correct and should not change.
