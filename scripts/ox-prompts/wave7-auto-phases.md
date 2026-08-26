# OX TASK: extract remaining _phase_* methods from autonomous.py (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-auto-phases-2ac0`
Worktree: `/tmp/callisto-ox-auto-phases`

LONG extract. `tools/autonomous.py` is ~8k. `tools.loop.sequencer` already
has PHASES. Move `_phase_*` **implementations** that are self-contained
into `tools/loop/phases_*.py` as functions taking `loop` (the ResearchLoop
instance) so `_loop` still iterates the table. Do not stop after one phase.

## Exclusive files (HARD)

You MAY edit:
- `tools/autonomous.py` (wrappers + imports only beyond the moved phases)
- `tools/loop/phases_impl.py` (create) and `tools/loop/__init__.py`
- `tests/test_loop_sequencer_slice.py` / `tests/test_loop_phase_errors.py` /
  `tests/test_loop_cycle_health.py` (adapt)
- `tests/test_auto_phases_extract.py` (create)

Do NOT delete `_phase_live_execute` gate (`CALLISTO_ALLOW_LIVE_EXECUTE`).
Do NOT change continue-to-next-phase. Do NOT widen paper-signal to live.
Do NOT import hung paths; AST/source pins OK.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- ResearchLoop._phase_* names remain (wrappers are fine).
- live_execute still env-gated before list_hypotheses.
- last_cycle_ok semantics unchanged (current cycle only).
- Line count of autonomous.py must drop by hundreds, not dozens.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_auto_phases_extract.py tests/test_loop_cycle_health.py tests/test_loop_phase_errors.py tests/test_loop_sequencer_slice.py tests/test_fail_closed_registry.py -q
```

Commit: `refactor(loop): extract ResearchLoop phase implementations to tools.loop`

Write `OX_DONE.md` with before/after `wc -l tools/autonomous.py`.
