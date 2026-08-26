# OX TASK: cycle status is not OK when phases failed

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-loop-cycle-health-2ac0`
Worktree: `/tmp/callisto-ox-loop-cycle-health`

## Exclusive files (HARD)

You MAY edit:
- `tools/autonomous.py` (`get_status` and `_loop` bookkeeping only)
- `tools/loop/sequencer.py` (only if you need a helper; prefer not)
- `tests/test_loop_cycle_health.py` (create)
- `tests/test_loop_phase_errors.py` / `tests/test_loop_sequencer_slice.py` (adapt)

Do NOT delete `_phase_live_execute`. Do NOT widen paper-signal. Do NOT
rewrite the 8k file. Base origin/master already has `tools.loop.sequencer`.

## Git rules

No full suite. Push.

## Bug

`_loop` records phase failures on the ledger then `continue`s. The cycle
still looks successful. `get_status` exposes the last 10 failures but
nothing like `last_cycle_ok: false`. Overnight operators cannot tell a
green loop from a loop that swallowed every phase.

## Required

- After each cycle (or in `get_status`), expose:
  - `last_cycle_ok` bool (False if any phase failed this cycle)
  - `last_cycle_phase_failures` int
- Do not change the continue-to-next-phase behavior (non-fatal phases stay
  non-fatal). Only make the lie visible.
- Tests: record two failures on a stub loop / ledger; `get_status()` has
  `last_cycle_ok is False`. Zero failures → True. Do not `import` a hung
  path; if `ResearchLoop` import is slow, test the ledger+status dict
  construction only.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_cycle_health.py tests/test_loop_phase_errors.py tests/test_loop_sequencer_slice.py -q
```

Commit: `fix(loop): get_status last_cycle_ok is false when phases failed`

Write `OX_DONE.md`.
