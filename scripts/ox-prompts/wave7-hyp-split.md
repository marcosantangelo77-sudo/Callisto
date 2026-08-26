# OX TASK: split tools/hypothesis.py into a package (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-hyp-split-2ac0`
Worktree: `/tmp/callisto-ox-hyp-split`

LONG extract. `tools/hypothesis.py` is ~2885 lines. Carve it until the
facade file is mostly imports + `HypothesisManager` re-exports. Do not
stop after moving one helper.

## Exclusive files (HARD)

You MAY edit:
- `tools/hypothesis.py` (become facade OR keep class and import mixins)
- `tools/hypothesis/` (create: e.g. `stages.py`, `auto_promote.py`, `store.py`)
- `tests/test_hyp_split.py` (create)
- existing hypothesis tests only if imports break

Do NOT change `auto_promote` diagnose-only behavior (no writes to
`edge_threshold` / `signal_generated`). Do NOT arm live. Do NOT touch
`api.py` or `tools/autonomous.py`.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- `from tools.hypothesis import HypothesisManager` still works.
- `STAGE_ORDER` still exists with draft/backtesting/paper_trading/live/retired.
- auto_promote remains diagnose-only (source pin).
- Shrink `tools/hypothesis.py` materially (thousands of lines moved, not 40).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_hyp_split.py tests/test_fail_closed_registry.py -q
```

Add a focused auto_promote source test if one exists; skip missing.

Commit: `refactor(hypothesis): split manager into tools.hypothesis package`

Write `OX_DONE.md` with before/after line counts.
