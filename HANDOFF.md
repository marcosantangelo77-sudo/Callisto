# HANDOFF — resuming this work

Written 2026-08-22 for whoever picks this up: a fresh Claude session, a new
OpenCode instance, or the owner at the workstation.

## State

`master` is green and pushed. Read in this order:

  BUILD_MANDATE.md   what Callisto is for, and the standing rules
  NEXT.md            the queue, the scope guard, everything decided so far
  ROADMAP.md         the original audit findings
  ARCHITECTURE_MAP.md / COVERAGE_MAP.md   the cartography
  DOMAIN_GENERALITY.md / DEEP_RESEARCH.md the design analyses

## The parallel-agent setup

Six git worktrees, each its own branch, sharing one object store:

    ~/callisto-wt/loop  money  gate  epistemics  serving  edgar  data  research

**Exclusive file ownership is the whole safety model.** No two instances may
edit one file. It has held across thirteen branches with zero source conflicts.
Give each instance an explicit OWN list and tell it everything else is
read-only, with defects written to findings/ rather than fixed.

Helpers:

    ~/callisto-wt/status.sh        live view of every instance
    ~/callisto-wt/COORDINATION.md  the protocol instances read

## Running an instance (Hermes / Nous Portal — current setup)

    cd ~/callisto-wt/<worktree>
    git fetch -q origin && git checkout -B build/<name> origin/master
    ~/.hermes/bin/hermes -z "<brief>" --in ~/callisto-wt/<worktree> > /tmp/<name>.log 2>&1 &

Check auth: `~/.hermes/bin/hermes portal info` — expect "logged in", model
`stealth/ox-alpha`, API `inference-api.nousresearch.com`.

**Watch for the silent checkout failure.** `git checkout -B` refuses when the
worktree has uncommitted modifications, and with `-q` it fails quietly — an
instance then runs on a stale branch missing files its brief references. Always
verify afterwards: branch name correct, `rev-list --count HEAD..master` is 0.

## If Nous rate-limits (fallback to OpenRouter / OpenCode)

OpenRouter's free tier died at `free-models-per-day-stealth`; it resets daily.

    cd ~/callisto-wt/<worktree>
    ~/.opencode/bin/opencode serve --port 40XX --hostname 127.0.0.1 &
    ~/.opencode/bin/opencode run --attach http://127.0.0.1:40XX \
      --dir ~/callisto-wt/<worktree> -i --title "<name>" "<brief>"

OpenCode sandboxes file reads to the project directory — coordination files must
live INSIDE the worktree, and instances exchange findings via git
(`git fetch origin && git show origin/<branch>:findings/<file>`), never a shared
directory. OpenCode also uses ~600 MB fresh and grows to ~1.4 GB; Hermes uses
~60-140 MB. On 8 GB that is the difference between two instances and eight.

## Git

SSH auth is configured and silent — `git push` just works. Verify with
`ssh -T git@github.com` (expect "Hi marcosantangelo77-sudo!"). Never use HTTPS,
`gh`, or GitHub Desktop on this machine; they hit a keychain wall.
Callisto's default branch is `master`, not `main`.

## Merging

Merge each branch into master, then RUN THE SUITE. Merging is where the real
defects surface — three integration bugs tonight were invisible on their own
branches. Conflicts have only ever been in scaffolding (NEXT.md,
COORDINATION.md, START_HERE.md); a SOURCE conflict means ownership was violated
and should be investigated, not auto-resolved.

    python3 -m pytest tests/ -q -p no:cacheprovider \
      --ignore=tests/test_backtest_e2e.py --ignore=tests/test_dashboard.py \
      --ignore=tests/test_ml_classifier.py --ignore=tests/test_ml_drift.py \
      --ignore=tests/test_api_auth.py --ignore=tests/test_adaptive_timeout.py \
      --ignore=tests/test_claude_findings.py --ignore=tests/test_prop_scanner.py

Those ignores are pre-existing missing-dependency failures (polars, fastapi,
joblib, xgboost), identical on the base commit. Not caused by this work.

## Verify claims; do not trust reports

Agents report successes they have not verified. Tonight: B2 claimed 62 passing
tests when three were failing on its own branch; B6 shipped 1,492 lines with
zero tests and substituted live SEC verification, which got this machine
HTTP 403'd; R3 shipped a test asserting the right invariant with inputs that
never hit the failing boundary. **Re-run everything. Probe properties with
random inputs, not chosen ones.**

## Hard rules

  1. Never arm the live execution path.
  2. Nothing automated may weaken a gate — raise, never lower.
  3. Characterization tests before numerical edits.
  4. No live API calls in tests; cached fixtures, plus a no-socket guard.
  5. No migrations against the owner's real database without a backup and
     his explicit go-ahead.
  6. Quarantine to attic/ rather than delete.

## Open items

  - SEC is currently 403ing this machine (rate-limit fallout). Temporary.
  - The real database is on the workstation, not here. `memory/callisto.db`
    is a 20 KB stub. The ghost-FK check and migration 013 wait for that machine.
  - Nothing has been driven end to end yet. That remains the highest-value
    unfinished thing — see NEXT.md "THE DISCIPLINE".

---

## RESUME PROTOCOL — added 2026-08-22 ~04:00

**If the Claude session ended mid-flight (usage limit, crash, closed terminal):**

Agents keep running independently. Their work is protected by a detached daemon:

    ~/callisto-wt/autosave.sh      commits + pushes every worktree every 5 min
    ~/callisto-wt/autosave.log     what it saved and when
    pgrep -f autosave.sh           check it is alive; relaunch with nohup if not

So **nothing is lost**. Resuming is a merge job, not a recovery job.

### Steps to resume

1. `~/callisto-wt/status.sh` — see every instance, branch, commits, uncommitted.
2. For each branch ahead of master: read its `findings/` and its final log in
   `/tmp/*.log`, then merge into master and RUN THE SUITE (command above).
3. Fix integration failures. They will exist — merging is where defects surface.
   Three appeared tonight that were invisible on individual branches.
4. Push master. Verify `pmset -g batt` shows charging and `pgrep -x caffeinate`
   is alive, or the machine sleeps and everything stops.

### The standing instruction

**Stop adding capability. Start testing.** As of this writing the repo has
~1,490 tests and every major component built, but nothing has been driven end
to end. The owner's own words: *"we just gotta test it at this point."*

The pipeline instance (`build/pipeline`) is the one that makes testing possible
— it wires the eleven disconnected components into one chain. When it lands,
the next action is NOT another feature. It is:

  - drive one real question through the whole chain
  - whatever breaks is the real backlog, better ranked than any list

Resist the pull toward more components. The failure mode this project already
survived once was four months of building without running.

### Branch inventory at handoff time

    merged to master:  audit tiers 0-7, sandbox, artifacts, charts, model
                       registry, tool registry, citation grounding, provenance,
                       HMAC seal, OutcomeResolver, CLV rewire, base-rate floors,
                       inheritance rule, ResearchProgram, schema seam +
                       migrations, EDGAR + fixtures, retrodiction harness,
                       adversary, source registry (8 adapters), edge
                       quantification, Fermi, reference classes

    in flight:         build/pipeline           end-to-end wiring  <- the one that matters
                       build/preregistration    sealed falsifiers + long-lived claims
                       build/sources-2          more adapters incl. Wayback-as-proof
                       build/loop-quality       information-gain termination

## MERGE GUARD — run it, do not eyeball merges

    ~/callisto-wt/verify-merge.sh [PREV_REF]     # default HEAD~1

Catches what a human reviewer misses by eye, because it happened repeatedly:
  - a merge that DELETED a public function another branch added
    (tools/why.py lost independence_from_fetches this way; engine.py lost the
     checkpoint re-gate fix the same way an hour later)
  - test files that vanished
  - a source file that shrank >25% (a stale branch overwriting newer work —
    one merge would have deleted 526 lines of another agent's query builder)
  - first-party imports that no longer resolve

RUN IT AFTER EVERY MERGE, BEFORE PUSHING. Exit 1 means do not push.

**Conflict rule, learned the hard way:** OWNERSHIP decides, not recency, and
never blanket `--theirs`. When two branches both edit a file, the instance that
OWNS it wins. When both legitimately changed it (engine.py: one added
parallelism, one added checkpoint re-gating), neither side wins — do a real
three-way merge with `git merge-base`, or re-queue the smaller change as work
against the merged file. Picking a side silently discards a fix.
