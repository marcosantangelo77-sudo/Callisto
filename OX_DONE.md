# OX TASK: OrderManager.enable refuses CALLISTO_LOCAL_ONLY

- **Branch:** `cursor/ox-om-local-only-2ac0`
- **Commit:** `4663be2`
- **Date:** 2026-08-26

## Change

`tools/order_manager.py` — `OrderManager.enable()` now mirrors
`BetExecutor.enable()`: when `CALLISTO_LOCAL_ONLY` env var is set to
`1`/`true`/`yes` (case-insensitive), the manager stays disabled, logs a
warning, and returns `False`. Otherwise it arms and returns `True`.
`disable()` unchanged. Default `_enabled=False` retained.

## Tests

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_order_manager_local_only.py tests/test_order_manager_default_disabled.py -q
..........                                                               [100%]
10 passed in 0.07s
```
