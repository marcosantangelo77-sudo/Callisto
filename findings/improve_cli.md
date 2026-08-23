# CLI FRONT DOOR — improvement pass (build/cli-front-door)

**Area chosen: the CLI and how a human actually uses this thing.**

Why this one, of the eleven areas: every recent wave improved the engine's
internals (synthesis, retrieval, checkpointing, routing). MORNING_REPORT's own
end-to-end backlog ends at "construction ergonomics" — a person still could not
drive one question without reading pipeline source. `callisto.py` existed as an
untracked file on this branch claiming to be the fix; this run made the claim
true and verified.

## What was wrong — measured

Baseline on this machine before any change:

1. **`callisto status` crashed with a raw traceback** (`sqlite3.OperationalError:
   no such table: hypotheses`) — the lifecycle DB lives on the workstation;
   this checkout carries only hermes memory tables. A front door that dies ugly
   on first contact fails its purpose.
2. **`callisto doctor` had a live UnboundLocalError**: with an unreadable
   providers path, `provs` was referenced after the failed load
   (verified: exit 1, traceback, no diagnosis printed). A diagnostic tool must
   never itself be the crash.
3. **The docstring lied**: nine one-off SQL debug scripts were claimed
   "quarantined to attic/" but all still sat at repo root. ARCHITECTURE_MAP §1.3
   lists them as VERIFIED orphans (fan-in 0, no test/script/launcher consumer).
4. **`callisto.py` had zero tests** despite having test seams already built in.

## What changed (2 commits)

**6a475a5 — quarantine:** nine scripts moved to `attic/debug-scripts/` with a
RESTORE_NOTE.md recording how to restore and what covers their reports now.
Zero references verified by grep across tests/, tools/, agp/, scripts/, api.py,
orchestrator.py, inference.py, task_queue.py, all shell/ps1/bat launchers.
~580 lines of session debris off the import path.

**db26ef9 — fixes + tests:**
- doctor: `provs = {}` initialised before the try; hermes-cli check wrapped so
  it degrades gracefully; it fails doctor ONLY when a configured provider uses
  backend=hermes_cli AND the CLI is unreachable (previously a dead
  `if ... pass`).
- status: graceful "lifecycle has not run on this machine" when the hypotheses
  table is absent (exit 0).
- ask: unknown --backend refused up front listing configured tiers (before:
  it silently rewired routes to a nonexistent tier); unreachable provider names
  `callisto doctor` as the next step.
- tests/test_cli_front_door.py: 13 tests pinning the contract — parser shape,
  status against missing/lifecycle-less/full DBs, doctor diagnosing-not-crashing,
  ask's exit codes, sealed rendering, adversary wiring vs --self-review,
  engine-never-built on unknown backend.

## Before/after numbers

| measure | before | after |
|---|---|---|
| root-level orphan debug scripts | 9 | 0 |
| `status` on laptop checkout | raw traceback, exit 1 | message, exit 0 |
| `doctor` w/ unreadable config | UnboundLocalError | "config unreadable" + PROBLEMS FOUND, exit 1 |
| unknown `--backend` | silent misroute | refused, exit 2, tiers listed |
| CLI test coverage | 0 tests | 13 tests |
| full suite | 2015 passed / 17 failed | 2028 passed / 17 failed |

Failure set identical before/after (backtest_e2e ×11, claude_findings,
prop_scanner, adaptive_timeout) — pre-existing, recorded in i3_synthesis.md.
Sports stays green.

## Live verification run

    callisto doctor            → OK, 20 adapters listed, exit 0
    callisto doctor --providers /nonexistent.yaml → diagnosed, exit 1 (was crash)
    callisto status            → graceful no-lifecycle message, exit 0 (was traceback)
    callisto ask --backend nonexistent … → refused with tier list, exit 0→2

(A live `ask` through a real model was NOT re-run this session; `ask` is
covered at the seams with fakes. The last live run is documented in
MORNING_REPORT SECOND LIVE RUN.)

## What I deliberately did not do

- No new commands nobody asked for. `ask/status/doctor` is the honest surface.
  A `seal <id>` / `verify <artifact>` command would fit property 3 (checkable
  evidence) and is the natural next CLI increment if someone drives more real
  questions — proposed, not built.
- Did not touch `tools/pipeline/cache.py` or `counting.py` (untracked, a peer's
  perf work) or either stray stash on this repo.
- Did not merge to master.

## Honest caveats

- The concurrent-edit warning fired twice while editing callisto.py — another
  writer touched it mid-session. Final state was re-read and re-measured after
  each conflict; the committed version is what the numbers above describe.
- A stash pop I triggered during a regression check pulled in foreign worktree
  state; I restored the tree exactly (git status clean except my two files +
  the peer's two untracked files) before committing. Lesson recorded: do not
  stash/pop on a shared multi-worktree repo to A/B a suite — diff the failure
  list instead.
