# OX_DONE — extract last_cycle_ok helpers to tools.loop.cycle_health

Branch: `cursor/ox-cycle-health-extract-2ac0`

## Changes

- **tools/loop/cycle_health.py** (new): pure `last_cycle_phase_failures(cycles, ledger)` and `last_cycle_ok(cycles, ledger)`. Count is scoped to the current cycle (`entry["cycle"] == cycles`); 0 failures / `cycles == 0` → healthy.
- **tools/autonomous.py**: `_last_cycle_phase_failures` / `_last_cycle_ok` are now one-line wrappers delegating to the pure helpers; method names kept so `get_status` keys stay stable. Added one import line. No other changes.
- **tools/loop/__init__.py**: re-exported both helpers.
- **tests/test_loop_cycle_health.py**: kept stub-loop wrapper tests; added `TestPureFunctions` exercising the helpers directly with `(cycles, ledger)`. No hung-path imports.

## Verification

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_cycle_health.py tests/test_loop_phase_errors.py tests/test_loop_sequencer_slice.py -q
29 passed in 0.70s
```
