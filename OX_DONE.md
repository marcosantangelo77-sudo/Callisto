# OX DONE: doctor money switches + bind

Branch: `cursor/ox-doctor-money-2ac0`
Commit: `f202f2ae039c7bf2f3c0e3b9b0ebae9c4cf0ed04`
Pushed to origin.

## Changes
- `callisto.py`: added `== bind ==` (fails closed on `0.0.0.0` / `::`, loopback default `127.0.0.1`) and `== money switches ==` sections after the existing `== seal ==` section. Money switches reads source contracts only (`OrderManager.__init__` must default `_enabled = False`; `BetExecutor.__init__` must assign `_enabled = False`) — no instantiation, no `enable()` calls — plus env-only on/off reporting for `CALLISTO_LOCAL_ONLY` and `CALLISTO_ALLOW_LIVE_EXECUTE`. No tokens or seal key values are printed.
- `tests/test_cli_doctor_money.py`: new tests (wildcard bind fails, loopback default passes, money switches reported off, seal key value never printed, IPv6 wildcard fails).

## Note
`tools/order_manager.py` currently defaults `_enabled = True` in `__init__`, so the money-switches check correctly FAILs on this branch; that file is outside the exclusive-edit list.

## Test output

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_doctor_money.py tests/test_cli_seal_doctor.py -q
.........                                                                [100%]
9 passed in 0.13s
```
