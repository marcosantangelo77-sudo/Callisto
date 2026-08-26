# OX_DONE — cycle status is not OK when phases failed

Branch: `cursor/ox-loop-cycle-health-2ac0`
Commit: `fix(loop): get_status last_cycle_ok is false when phases failed`

## What changed

- `tools/autonomous.py` (`get_status` + two small helpers):
  - `get_status()` now returns `last_cycle_ok` (bool) and
    `last_cycle_phase_failures` (int).
  - `_last_cycle_ok()`: False iff the ledger's most recent failure belongs to
    the current cycle; True before any cycle or with a clean ledger.
  - `_last_cycle_phase_failures()`: count of failures recorded in the latest
    failing cycle.
- No behavior change: phases still continue after failure (continue-to-next-
  phase untouched); only the lie becomes visible. `_phase_live_execute` intact.

## Tests

`tests/test_loop_cycle_health.py` (new, stub loop — no hung paths):
- two failures this cycle → `last_cycle_ok is False`, count 2
- zero failures → True / 0
- no cycles yet → True / 0
- failure in an older cycle → still ok
- ledger cap (50) doesn't break health reporting
- get_status source exposes both keys

Result: `27 passed` for test_loop_cycle_health.py + test_loop_phase_errors.py +
test_loop_sequencer_slice.py via `/tmp/callisto-pytest/bin/python`.
