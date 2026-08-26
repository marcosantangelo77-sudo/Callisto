# OX_DONE — extract ResearchLoop phase-failure ledger (slice 1)

Branch: `cursor/ox-loop-extract-slice-2ac0`
Commit: `refactor(loop): extract phase-failure ledger to tools.loop` (pushed)

## What landed
- `tools/loop/__init__.py` — new importable sub-package.
- `tools/loop/phase_ledger.py` — `PhaseFailureLedger`: cap 50 (oldest dropped),
  `record(cycle, phase, kind, exc)` (error = truncated repr ≤300 chars, or
  literal "timeout"), `latest(n)`, `count`.
- `tools/autonomous.py`:
  - imports `PhaseFailureLedger`; `ResearchLoop.__init__` now holds
    `self._phase_failures_ledger = PhaseFailureLedger()` (raw `_phase_failures`
    list and `_PHASE_FAILURES_MAX` removed).
  - `_record_phase_failure` delegates to the ledger; all call sites unchanged.
  - `get_status` exposes the same `phase_failures` (last 10) and
    `phase_failure_count` keys via `ledger.latest(10)` / `ledger.count`.
- `tests/test_loop_phase_errors.py` — adapted: unit tests on the ledger,
  delegation tests via a minimal stand-in, AST checks that get_status keys
  survive and no raw-list attribute remains.
- `tests/test_loop_sequencer_slice.py` — new: package importable,
  ResearchLoop wiring, `_phase_live_execute` untouched.

## Not touched
Gate policy / signal refresh (different worker), `_phase_live_execute`,
paper-signal→live widening. No other files modified.

## Verification
```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_sequencer_slice.py tests/test_loop_phase_errors.py -q
14 passed in 0.55s

tests/test_loop_signal_refresh_gate.py -q → 4 passed (no regression)
```
