# OX TASK: OrderManager.enable refuses CALLISTO_LOCAL_ONLY

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-om-local-only-2ac0`
Worktree: `/tmp/callisto-ox-om-local-only`

## Exclusive files (HARD)

You MAY edit:
- `tools/order_manager.py`
- `tests/test_order_manager_local_only.py` (create)
- `tests/test_order_manager_default_disabled.py` (only if present; keep passing)

You MUST NOT edit `tools/bet_executor.py`, `tools/telegram_bot.py`,
`tools/autonomous.py`, `api.py`, credentials, or `master`.
Do NOT default `_enabled=True`. Do NOT widen paper-signal statuses.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug

`BetExecutor.enable()` now refuses `CALLISTO_LOCAL_ONLY`. `OrderManager.enable()`
still always sets `_enabled=True`. Chat `/resume_all` calls `manager.enable()`.
Local-only must be nuclear for order submission too.

This worktree should already have OrderManager default `_enabled=False`
(from `cursor/ox-telegram-arming-2ac0`). Keep that.

## Required change

Mirror BetExecutor: if `os.getenv("CALLISTO_LOCAL_ONLY","").lower() in ("1","true","yes")`,
`enable()` leaves `_enabled=False`, logs a warning, returns False.
Otherwise enable and return True (or keep None if you must — then tests
assert `is_enabled` only). `disable()` unchanged.

## Tests

- Default env: enable() arms.
- LOCAL_ONLY=1/true/yes: enable() leaves disabled; submit_order still refused.
- Existing default-disabled tests still pass if present.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_order_manager_local_only.py tests/test_order_manager_default_disabled.py -q
```

Skip missing files.

Commit: `fix(money): CALLISTO_LOCAL_ONLY refuses OrderManager.enable()`

Write `OX_DONE.md` with SHA and test output.
