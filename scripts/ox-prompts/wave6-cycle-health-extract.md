# OX TASK: extract last_cycle_ok helpers out of autonomous.py

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-cycle-health-extract-2ac0`
Worktree: `/tmp/callisto-ox-cycle-health-extract`

## Exclusive files (HARD)

You MAY edit:
- `tools/loop/cycle_health.py` (create)
- `tools/loop/__init__.py` (export)
- `tools/autonomous.py` (the two helpers + their call sites in `get_status` only)
- `tests/test_loop_cycle_health.py` (adapt)

Do NOT rewrite the 8k file. Do NOT change continue-to-next-phase.
Do NOT delete `_phase_live_execute`. Do NOT widen paper-signal.
Do NOT `import` hung paths in new tests; keep the stub-loop style.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. Base origin/master.

## Why

God-module diet. `last_cycle_ok` / `last_cycle_phase_failures` just landed
inline on `ResearchLoop`. Move the **pure** functions to
`tools/loop/cycle_health.py` taking `(cycles, ledger)`.

## Required

```python
def last_cycle_phase_failures(cycles: int, ledger) -> int:
    """Count ledger entries whose cycle == cycles. 0 if cycles == 0."""

def last_cycle_ok(cycles: int, ledger) -> bool:
    """True iff last_cycle_phase_failures(...) == 0."""
```

`ResearchLoop` methods become one-line wrappers (keep method names so
`get_status` keys stay). Semantics already on master: count is **current
cycle only** (`entry["cycle"] == self._cycles`), not the latest failing
cycle.

Adapt stub tests to import the functions directly as well as the wrappers.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_cycle_health.py tests/test_loop_phase_errors.py tests/test_loop_sequencer_slice.py -q
```

Commit: `refactor(loop): extract last_cycle_ok helpers to tools.loop.cycle_health`

Write `OX_DONE.md`.
