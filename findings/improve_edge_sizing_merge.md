# IMPROVE — edge quantification & sizing, run 2: the landing that kept not
# happening

**Run:** 2026-08-25, ox-alpha standing-improvement role
**Area:** edge quantification and sizing (`tools/edge.py`, `tools/kelly.py`,
`tools/sizing.py`, Kalshi wiring)
**Branch worked from:** this worktree's checked-out line; merge landed on
local `landmerge`, pushed as `origin/landmerge` (merge commit `ee3ee9a`).

## The choice, stated up front

improve_* findings already cover the CLI (twice), artifacts/sandbox, memory/
wiki, retrodiction/calibration. Edge quantification was covered by a previous
ox-alpha run (2026-08-24) whose entire finding was: **the verified money-path
fixes exist on side branches and never reach any integration line.** That run
ended by pushing `improve/money-path-landing` "for the train" and NOT merging.

This run asked the obvious follow-up: did it land?

## Family hunted: #2 at process scale, fourth instance

PATTERNS family 2 is "a fix lands in one copy while another keeps the bug."
The process-scale version recurred exactly as the previous run feared:

- `origin/master`: still ships every defect (100x EV on cents contracts,
  crossed books sized at full Kelly, round() raising stakes).
- local `landmerge` (the integration train): **does not contain
  `improve/money-path-landing` either.** `git branch --contains` over all
  refs shows the fix branch contained only in itself.
- Meanwhile review run 7 had already documented "master is 53-red with
  money-path fixes stranded on two unmerged branches (family 2, 4th
  instance)" — so this is the FOURTH recorded recurrence of the same family,
  now at the level of the merge train itself.

## What I did

1. Re-derived the defect state independently rather than trusting prior
   findings. On the pre-fix line, `tests/test_redteam_money_path.py`:
   7 failed / 3 passed (M1–M5 all live). On the fix branch worktree: only the
   five argued-wrong repros stay red (documented in
   findings/redteam_backlog_leave_red.md; each dies on its own sign error or
   precondition, not on production behaviour). Characterization suites
   (r5 edge, devig, tier0 kelly/sizing): 127 passed. Sports regression
   test_backtest_e2e.py: 40 passed.
2. Verified the merge is clean: `git diff --name-only --diff-filter=U`
   empty against landmerge's tip (edge.py/kelly.py/sizing.py untouched on
   the landmerge side since the merge-base).
3. Merged `improve/money-path-landing` into `landmerge` with --no-ff and a
   message naming what landed, then re-ran everything on the merged tree:

| measure | landmerge before | landmerge after merge |
|---|---|---|
| money suite (-k filter) failures | 7 red (M1,M1b,M2,M2b,M3,M4,M5) | 6 red, ALL of them the deliberately-red wrong-invariant pins + 1 unrelated retrieval r4 |
| M2/M2b/M4 (real defects) | failing | **passing** |
| r5/devig/tier0 characterization | 127 pass | 127 pass |
| sports e2e | 40 pass | 40 pass |

Live probes post-fix (verified in-process): crossed book → actionable=False,
kelly=0; 47-cent contract EV ≈0.277 (was ~27); summary() cannot round an
edge upward.

4. Pushed `landmerge` to origin.

## What remains red on the merged tree, and why that is correct

- m1/m1b/m3/m5(/m6-doc): pinned with the wrong invariant; the intended
  invariants are proven by replacement pins on the merged tree. Owner should
  rewrite or delete these five; nobody else can satisfy them without
  re-introducing a bug.
- test_r4 (retrieval): different layer, owned elsewhere.

## Honest note about scope

This run shipped no new arithmetic. That is deliberate: the area's math has
now been fixed twice, swept by two property campaigns, and mutation-checked;
what was broken was the DELIVERY. Per rule 3 ("the system must work at every
step") the highest-value change available was making the integration line
contain the verified fixes rather than writing a third copy.

## Follow-ups (real backlog, ranked)

1. Whoever runs the merge train next must take `landmerge` (or cherry-pick
   `ee3ee9a`) — master is still red-with-known-fixes until then.
2. The five wrong-invariant pins in test_redteam_money_path.py need an owner
   to rewrite them as invariant-correct tests.
3. Stale-quote gate: `as_of` is recorded but compared nowhere; sizing still
   has no clock seam.
4. clv_points is YES-side only; needs `side=` before NO-side claims score.
