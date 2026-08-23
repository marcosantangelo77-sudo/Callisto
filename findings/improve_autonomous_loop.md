# AUTONOMOUS LOOP — improvement pass (build/cli-front-door)

**Area chosen: the autonomous loop** (`tools/autonomous.py` — AutonomousLoop +
ResearchLoop, ~8,150 lines).

Why this one: CLI was covered twice; the four runs before this took AGP core,
retrodiction/calibration, edge sizing, artifacts/sandbox, and the hypothesis
lifecycle. The loop is the second-largest hub in the system (fan-out 38, 4%
line coverage per ARCHITECTURE_MAP) and the home of three of the eight
gate-weakening mechanisms from the audit — yet no improve pass had owned it.
Memory/wiki and source registry carry a peer's uncommitted work this run, so
both were off-limits.

## What was wrong — measured

MORNING_REPORT's headline says all eight automated gate-weakening mechanisms
are "now guarded." Two are not:

1. **`_phase_refresh_signals` rewrites historical evidence on EVERY cycle with
   no opt-in** (was autonomous.py:3193). It flips `backtest_events.signal_generated`
   0→1 wherever `edge >= current threshold` — literally audit mechanism #2
   ("rewrote historical signal_generated"). Its docstring justified it by
   "Claude deep work lowered the threshold after backtest" — but the deep-work
   apply step now REFUSES every lowering outright (direction guard at
   `_phase_interpret_backtests`, pinned by test_tier1_loop_autonomous_gate_policy).
   No legitimate automated path can create the state this phase exists to paper
   over, so it runs unconditionally against evidence whose only effect is to
   make historical signal counts match whatever the gate currently is.
2. **`_requeue_stale_signal_rejections` un-rejects hypotheses at every loop
   start with no opt-in** (was :1895). Its two siblings — `_requeue_threshold_rejections`
   and `_requeue_prop_rejections` — do the same class of action (rejected →
   backtesting/draft) and were both gated behind CALLISTO_ALLOW_THRESHOLD_MIGRATION.
   This one was missed. An inconsistent guard is a hole: any future refactor
   that routes an un-reject through this path gets a free bypass.

## What landed

Both routines now follow the established pattern exactly: without
`CALLISTO_ALLOW_THRESHOLD_MIGRATION=1` they are no-ops that LOG what they would
have done (with counts); with the flag their behavior is unchanged. Default
behavior for every operator who never set the flag changes from "rewrites /
un-rejects silently each cycle" to "logs and leaves evidence alone."

- `tools/autonomous.py`: guard in both routines; `_phase_refresh_signals`
  docstring rewritten to state why the phase exists only under explicit opt-in;
  `_requeue_stale_signal_rejections` gains the same warning its siblings emit.
- `tests/test_gate_policy_startup_rewrites.py`: NEW — behavioral tests driving
  the real coroutines against a fixture DB:
  - no flag → backtest_events untouched / rejected hypothesis stays rejected;
  - flag set → documented rewrite / un-reject happens (the opt-in still works).
  Existing gate-policy suite (9 tests) unchanged and green.

## Before/after

| | before | after |
|---|---|---|
| startup routines rewriting evidence/un-rejecting without opt-in | 2 | 0 |
| `signal_generated` rewritten per cycle by default | all matching events | none |
| gated routines requiring the operator flag | 4 | 6 |
| tests pinning the gate policy | source-grep + modify-path only | +4 behavioral |

## What else I looked at and deliberately did NOT do

- The `_loop` phase runner's 25 copy-pasted try/except blocks — real debt, but
  a table-driven rewrite is a big-bang change to the system's most load-bearing
  file for zero behavioral gain. Not this pass.
- The DB prune block inside `_loop` opens its own aiosqlite connection to
  CALLISTO_DB_PATH rather than using data_collector._db — works, slightly
  smelly, left alone.
- `ResearchLoop.start()` still runs seven one-time migration/requeue routines
  on every start; four of six gate-touching ones are now no-ops by default, so
  the residual risk is bounded.

Sports stayed green throughout (full suite run recorded in session log).
