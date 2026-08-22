# PARALLEL INSTANCE COORDINATION — Callisto audit

Separate OpenCode instances share no memory and cannot message each other.
This directory is the only channel between them. It lives OUTSIDE every
worktree deliberately, so it is never touched by a branch switch, reset,
or merge.

## The instances

| # | worktree                  | branch             | owns                                              |
|---|---------------------------|--------------------|---------------------------------------------------|
| 0 | ~/Documents/GitHub/Callisto | cartography/*    | Wave 1 cartography — coverage, call graph, dead code |
| 1 | ~/callisto-wt/loop        | audit/tier1-loop   | tools/autonomous.py, tools/self_repair.py, orchestrator.py |
| 2 | ~/callisto-wt/money       | audit/tier0-money  | tools/bet_executor.py, tools/kelly.py, tools/clv_tracker.py, bankroll/staking |
| 3 | ~/callisto-wt/gate        | audit/tier2-gate   | tools/hypothesis.py, tools/backtest.py, tools/hypothesis_generator.py, tools/ml_backtest.py |
| 4 | ~/callisto-wt/epistemics  | audit/tier3-epistemics | agp/, tools/knowledge_wiki.py, tools/hermes_memory.py, seal + calibration |

**Ownership is exclusive and absolute.** You may READ any file in the repo.
You may EDIT only files your row lists. If you find a defect outside your
files, you do not fix it — you write it to `findings/` and move on. Two
instances editing one file produces a silent overwrite that no test catches
and no one notices.

## Reporting to each other — VIA GIT (the sandbox blocks shared directories)

OpenCode confines file access to the project directory, so a shared folder
outside the worktree is unreadable. Git is the channel instead.

**Write** your findings to `findings/instance<N>.md` INSIDE your own worktree.
Append only. Commit and push them often — that is what publishes them:

    git add findings/ && git commit -m "findings: <what>" && git push

**Read** the others by fetching their branches:

    git fetch origin
    git show origin/audit/tier1-loop:findings/instance1.md
    git show origin/audit/tier0-money:findings/instance2.md
    git show origin/audit/tier2-gate:findings/instance3.md
    git show origin/audit/tier3-epistemics:findings/instance4.md

Do this before each work unit. A branch that does not exist yet simply errors —
that instance has not published anything, which is fine. Never edit another
instance's findings file; you cannot, and you should not try.

Entry format — terse, one per finding:

    ## [VERIFIED|INFERRED] <file>:<line> — <one-line claim>
    Blast radius: LOUD | SILENT | ARMING
    Evidence: <what you ran or read>
    Falsifier: <what would prove this wrong>
    For: <which instance owns the fix, or "unowned">

## Git discipline

- Each instance commits ONLY on its own branch. Never checkout another
  instance's branch — git refuses it anyway across worktrees, and that
  refusal is a feature.
- Commit small and often. Never one commit at the end.
- Push freely: `git push -u origin <your-branch>`. SSH auth is silent.
- Do not merge to master. Merges are a human decision at the end.
- Never `git worktree remove` or `git worktree prune` — you would delete a
  peer's working directory out from under a running process.

## The full test suite — hard rule

The suite is 1006 tests and each Python worker is ~140 MB. This machine has
8 GB total and is already swapping.

**Only instance 0 runs the full suite, and only serially.** Everyone else runs
targeted subsets scoped to their own files:

    python -m pytest tests/test_<yourmodule>.py -x -q

Parallel full-suite runs will exhaust RAM and macOS will start killing
processes without asking which.

## Coverage baseline — measured 2026-08-22, use these numbers

    tools/ml_backtest.py       0.0%    169 stmts
    tools/autonomous.py        3.6%   3849 stmts   <- the unattended loop
    orchestrator.py           16.7%    580 stmts
    tools/self_repair.py      17.1%    666 stmts   <- lowers its own gates
    api.py                    23.2%   2279 stmts
    tools/backtest.py         33.7%   1714 stmts
    tools/hypothesis_generator 38.8%   436 stmts
    tools/kelly.py            39.6%    250 stmts
    tools/clv_tracker.py      45.7%    317 stmts
    tools/bet_executor.py     46.3%    510 stmts
    tools/embeddings.py       49.5%    305 stmts
    tools/hypothesis.py       54.7%   1200 stmts

Note: an earlier claim that the money path was "untested" was WRONG — it is
roughly half covered. The real gap is the autonomous loop at 3.6%. Correct
prior findings loudly when the data disagrees; that is the job.

## Briefs

### Instance 1 — the unattended loop
`autonomous.py` is 3,849 statements at 3.6% coverage and it runs when nobody
is watching. `self_repair.py` (17.1%) responds to "nothing is passing the
quality bar" by lowering the quality bar — three paths, two of which write
config keys nothing reads while logging confidence-0.8 successes.

Establish and ENFORCE the principle: a maintenance routine must never be able
to weaken a gate. Design the enforcement mechanism; do not merely patch three
call sites. Then get characterization tests under the loop before touching its
behaviour. Apply the §2 interrogation protocol to every phase in the loop —
`autonomous.py` at 7,955 lines is a monolith and Q6 applies hard.

### Instance 2 — the money path
`bet_executor.py`, `kelly.py`, `clv_tracker.py`. ROADMAP §0 flags the live
path as structurally dead and warns the naive one-line fix ARMS untested
sizing code.

**Do not arm it. Not as a fix, not as a test, not as a demo.**

Your job is to make it *safe to arm later*: characterization tests pinning
current arithmetic, a unit audit (fraction vs rate, American vs decimal,
devigged vs raw — a confirmed unit bug already guards real money in
MIN_CLV_RATE), and a written proof of the Kelly sizing. Coverage near 46% with
zero tests on the arithmetic itself means the tests exercise plumbing, not
correctness. Determine which half is covered.

### Instance 3 — the gate
3,192 rejections, zero promotions. ROADMAP §3.2 establishes the Šidák
denominator is *lifetime* N, forcing alpha ~9e-05 — a ratchet that got
permanently harder with every hypothesis ever generated. Verify that
arithmetic independently; it is the load-bearing claim of the whole audit.

Then answer the design question behind it: what SHOULD the multiple-comparison
correction be scoped to — per family, per time window, per sport? That is a
modelling decision, not a constant, and it deserves a first-principles answer
rather than a tuned number. `backtest.py` is 33.7% covered across 1,714
statements and `ml_backtest.py` is at 0.0% — characterization tests before any
numerical change, without exception.

### Instance 4 — the epistemics (the moat)
`agp/` is 453 lines carrying the entire protocol. Prior-art research found
nothing importable doing enforced confidence tiers, promotion-gate
architecture, or seal discipline — this is the differentiated asset.

It is also where the softest failures live: the seal is an unkeyed hash
forgeable by anyone with DB write, the Sentinel vetoes nothing, VERIFIED tier
is unreachable because PRIMARY is never assigned, and the wiki/hermes loop is
a trust escalator promoting yesterday's unverified INFERRED into today's
0.75-ceiling prior.

A moat made of self-reported inputs gating self-reported confidence is not a
moat. Your central question: what would it take for a confidence tier to be
EARNED AGAINST REALITY rather than asserted? Answer that and the system
becomes genuinely hard to replicate. 453 lines carrying the whole protocol is
either elegance or under-specification — determine which.

## Restarting instances

opencode memory GROWS with context: ~768 MB fresh, ~1.4 GB after a long
session. On 8 GB that is the binding constraint, not instance count.

When an instance finishes a work unit: have it write findings, commit, push,
then **close the window and start a fresh one on the same worktree**. A fresh
instance re-reads COORDINATION.md and findings/ and picks up with full
context at 768 MB instead of 1.4 GB. Short focused sessions beat long ones on
this machine, and they produce better work anyway.

## Wave 1 output — READ THIS BEFORE YOUR FIRST WORK UNIT

Instance 0 produced `COVERAGE_MAP.md` on branch `cartography/architecture-map`.
It is VERIFIED — measured on this machine, not inferred. Read it:

    git show cartography/architecture-map:COVERAGE_MAP.md

Headline: 48,610 statements, 45.2% covered overall. It supersedes any earlier
claim about which modules are untested. In particular, an earlier assertion that
the money path was "untested" was WRONG — kelly 39.6%, clv_tracker 45.7%,
bet_executor 46.3%. The real gap is the unattended loop: autonomous.py at 3.6%
of 3,849 statements.

### The suite has 12 known failures — treat this as a finding, not a fact of life

    1006 passed, 8 skipped, 12 FAILED, ~116s wall

    11 × tests/test_backtest_e2e.py  (TestBacktestEndToEnd, TestPaperTradeResolution,
                                      TestRecalculateRunStats)
     1 × tests/test_prop_scanner.py::TestPropScanner::test_finds_edge

**Instance 3 owns these** — they are backtest/prop-scanner tests. A suite with a
dozen accepted failures is a suite whose signal has been trained away: every
future real regression lands in a red column people already ignore. Determine
for each whether the TEST is wrong or the CODE is wrong. Do not "fix" a test by
loosening its assertion — that is the same failure mode as self-repair lowering
its own gates, one level up.

Also note: `tools/ml_classifier.py` and `tools/ml_drift.py` show 0% because
xgboost/libomp are unavailable on THIS machine — an artifact, not a finding.
`tools/ml_backtest.py` at 0% is NOT xgboost-gated; that 0% is real.
