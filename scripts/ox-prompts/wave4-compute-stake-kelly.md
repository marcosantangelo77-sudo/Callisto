# OX TASK: BetExecutor.compute_stake imports canonical Kelly only

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-compute-stake-kelly-2ac0`
Worktree: `/tmp/callisto-ox-compute-stake-kelly`

## Exclusive files (HARD)

You MAY edit:
- `tools/bet_executor.py` (`compute_stake` imports and comments only)
- `tests/test_compute_stake_kelly_imports.py` (create — source contract)

Do NOT change stake numbers. Do NOT enable the executor. Do NOT touch
`tools/kelly.py` / `tools/sizing.py` / `tools/edge.py`.

## Git rules

No stash / reset --hard / full pytest. Push.

## Bug

`BetExecutor.compute_stake` historically imported Kelly helpers from
**both** `tools.kelly` and `tools.sizing`. After `kelly_binary` became a
wrapper, keep push-aware helpers from sizing if they are the only
implementation; import `kelly_full` / `kelly_fractional` / `kelly_dynamic`
from `tools.kelly` only. Add a one-line comment: canonical module is
`tools.kelly`.

If a numeric fixture would change, STOP.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_compute_stake_kelly_imports.py -q
```

The test should read `compute_stake` source (inspect.getsource) and assert
it does not import `kelly_full` from sizing, and does mention `tools.kelly`.

Commit: `refactor(kelly): compute_stake imports canonical tools.kelly`

Write `OX_DONE.md`.
