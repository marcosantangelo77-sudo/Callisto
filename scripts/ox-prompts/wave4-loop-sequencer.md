# OX TASK: extract ResearchLoop sequencer (slice 2)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-loop-sequencer-2ac0`
Worktree: `/tmp/callisto-ox-loop-sequencer`

## Exclusive files (HARD)

You MAY edit:
- `tools/autonomous.py`
- `tools/loop/sequencer.py` (create)
- `tools/loop/__init__.py`
- `tests/test_loop_sequencer_slice.py` (extend)
- `tests/test_loop_phase_errors.py` (only if import paths break)

Do NOT delete `_phase_live_execute`. Do NOT widen paper-signal to live.
Do NOT rewrite the 8k-line file. Do NOT change gate policy
(`CALLISTO_ALLOW_LIVE_EXECUTE`, `CALLISTO_ALLOW_SIGNAL_REFRESH`).

## Git rules

No stash / reset --hard / full pytest. Base is origin/master with
`tools/loop/phase_ledger.py` already present (`373352e`+).

## Goal

Move the **phase name list / cycle order** out of `ResearchLoop._loop`
into `tools/loop/sequencer.py`:

- `PHASES: tuple[str, ...]` (or equivalent) documenting the ordered
  phase method names.
- `ResearchLoop._loop` still exists and still calls the same `_phase_*`
  methods in the same order; it should iterate the imported PHASES
  (or a helper `iter_phases(self)`) instead of a hardcoded sequence
  copy-pasted in the method.

If `_loop` uses explicit `await self._phase_foo()` calls rather than a
list, introduce a small tuple of `(name, coro)` and iterate it. Behavior
must stay identical including try/except per phase and ledger recording.

Tests: PHASES includes `live_execute` / `_phase_live_execute`; order
stable; `test_loop_phase_errors.py` + `test_live_execute_gate.py` still pass.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_sequencer_slice.py tests/test_loop_phase_errors.py tests/test_live_execute_gate.py tests/test_loop_signal_refresh_gate.py -q
```

Commit: `refactor(loop): extract phase order to tools.loop.sequencer`

Write `OX_DONE.md`.
