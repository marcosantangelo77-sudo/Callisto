# OX TASK (WAVE 2 — queued, do not start until orchestrator launches you)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in the worktree
the supervisor passed via WORKING DIRECTORY.

## Exclusive file ownership (HARD)

You MAY edit:
- `tools/telegram_bot.py`
- `tools/order_manager.py`
- `tests/test_telegram_bot.py` (update)
- `tests/test_order_manager_default_disabled.py` (create)

You MUST NOT edit `tools/bet_executor.py` (do not change its default there).
You MUST NOT edit `tools/autonomous.py`, `api.py`, credentials, or `master`.

## Git rules (HARD)

- Stay on the worktree branch. No stash / reset --hard / checkout --. No merge.
- Commit and `git push -u origin HEAD` when tests pass.

## Bugs (verified)

1. Telegram `/resume_all` (`tools/telegram_bot.py` ~152–161) calls
   `bet_executor.enable()` after `manager.enable()`. HTTP arming of the
   executor is `require_admin`. Chat resume is a second, weaker money switch.

2. `OrderManager.__init__` sets `self._enabled = True` (`tools/order_manager.py` ~201).
   Fail-open: submitting orders works before anyone arms the manager.

## Required behavior

1. `_cmd_resume_all` MUST enable the order manager only. It MUST NOT call
   `bet_executor.enable()`. It may mention in the Telegram reply that the
   executor stays disabled until an admin HTTP arm. `/pause_all` may still
   disable both (fail-safe).

2. `OrderManager.__init__`: `self._enabled = False`.
   `enable()` already sets True (~273) — keep that for explicit arming.
   Callers/tests that submit orders must `manager.enable()` first, OR
   `submit_order` should refuse when disabled (if it already does, tests will
   need an explicit enable — that is correct).

3. Update `tests/test_telegram_bot.py`:
   - Existing `test_pause_and_resume` may keep asserting order_manager re-enables.
   - Add a test that passes a mock `bet_executor` into `handle_order_command`
     (see `handle_order_command` signature) and asserts `enable()` is NOT
     called on resume, while `disable()` IS called on pause.
   - Tests that `submit_order` after init: call `await m.enable()` if the
     manager now refuses while disabled.

## Tests

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_telegram_bot.py tests/test_order_manager_default_disabled.py -q
```

If other order-manager tests live in this repo and fail because default is
now False, you MAY add `await mgr.enable()` in those test files ONLY if they
are otherwise unusable. Prefer not spreading. If a test file is owned-adjacent
(`tests/test_order_manager*.py`), fixing fixtures there is allowed.

Commit: `fix(money): resume_all must not arm executor; OrderManager defaults disabled`

Write `OX_DONE.md` with SHA and test output.
