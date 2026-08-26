# OX TASK: CALLISTO_LOCAL_ONLY must refuse to arm BetExecutor

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-local-only-executor-2ac0`
Worktree: `/tmp/callisto-ox-local-only-executor`

## Exclusive files (HARD)

You MAY edit:
- `tools/bet_executor.py`
- `tests/test_local_only_executor.py` (create)

You MUST NOT edit `tools/telegram_bot.py`, `tools/order_manager.py`,
`tools/autonomous.py`, `api.py`, credentials, or `master`.
Do NOT change `generate_paper_trade_signal`. Do NOT default `_enabled=True`.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug (verified)

`CALLISTO_LOCAL_ONLY=1` blocks Claude subprocesses. It does **not** block
`BetExecutor.enable()`. `__init__` already sets `_enabled=False` (keep that).
`enable()` currently always sets `_enabled=True` and logs "live bets will be placed".

Local-only is supposed to be a nuclear switch for this appliance. Arming
the Playwright bet placer while LOCAL_ONLY is on is a loaded gun.

## Required change

In `BetExecutor.enable()`:

- If `os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes")`:
  leave `_enabled=False`, log that LOCAL_ONLY refused arming, return False
  (or None — pick one and test it). Do not raise if existing callers ignore
  the return value.
- Otherwise keep current enable behavior.

`disable()` / `__init__` unchanged. No live betting. No browser launches
in tests.

## Tests (`tests/test_local_only_executor.py`)

- Default env: `enable()` sets `is_enabled True`; `disable()` clears it.
- `CALLISTO_LOCAL_ONLY=1`: `enable()` leaves `is_enabled False`.
- Truthy variants `true` / `yes` (case-insensitive) also refuse.
- Restore env. Instantiate BetExecutor without hitting the network;
  stub browser if `__init__` is heavy (see `tests/test_tier0_money_executor.py`).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_local_only_executor.py -q
```

Commit: `fix(money): CALLISTO_LOCAL_ONLY refuses BetExecutor.enable()`

Write `OX_DONE.md` with SHA and test output.
