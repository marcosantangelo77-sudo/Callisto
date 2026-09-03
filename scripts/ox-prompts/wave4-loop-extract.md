# OX TASK: extract ResearchLoop sequencer (first slice only)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-loop-extract-slice-2ac0`
Worktree: `/tmp/callisto-ox-loop-extract`

## Exclusive files (HARD)

You MAY edit:
- `tools/autonomous.py`
- `tools/loop/__init__.py` (create)
- `tools/loop/phase_ledger.py` (create)
- `tests/test_loop_phase_errors.py` (move/adapt if it exists on this branch)
- `tests/test_loop_sequencer_slice.py` (create)

You MUST NOT edit other files. Do NOT split the whole 8k-line file.
Do NOT delete `_phase_live_execute`. Do NOT widen paper-signal to live.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Goal

Land **one importable package** behind the same `ResearchLoop` class.

Move the phase-failure ledger out of `autonomous.py`:

- `tools/loop/phase_ledger.py`: `PhaseFailureLedger` with cap 50,
  `record(cycle, phase, kind, exc)`, `latest(n)`, `count`.
- `ResearchLoop` holds `self._phase_failures_ledger = PhaseFailureLedger()`
  and `_record_phase_failure` delegates to it. `get_status` still exposes
  `phase_failures` / `phase_failure_count` with the same keys.

If this worktree is based on master **without** the ledger yet, implement
the ledger in the new module AND wire `_loop` the same way the
`cursor/ox-loop-refresh-2ac0` commit did (gated signal refresh is a
**different** worker — do not re-do signal refresh here unless that
code is already in your tree).

If `1227f4a` is already in your tree, **only extract** the ledger; do not
change gate policy.

Tests: ledger cap-50, timeout vs exception kinds, get_status keys still present.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_sequencer_slice.py tests/test_loop_phase_errors.py -q
```

Skip missing test files if not in this tree; create `test_loop_sequencer_slice.py`.

Commit: `refactor(loop): extract phase-failure ledger to tools.loop`

Write `OX_DONE.md`.
