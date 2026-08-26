# OX TASK: doctor reports money switches and bind, never prints secrets

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-doctor-money-2ac0`
Worktree: `/tmp/callisto-ox-doctor-money`

## Exclusive files (HARD)

You MAY edit:
- `callisto.py`
- `tests/test_cli_doctor_money.py` (create)

You MUST NOT edit `agp/`, `api.py`, `tools/bet_executor.py`, credentials,
or `master`. Do NOT generate or print `CALLISTO_SEAL_KEY`. Do NOT remove
the existing `== seal ==` doctor section (it must keep failing closed on
unset/invalid hex).

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Goal

`callisto doctor` is the appliance honesty surface. After the seal section,
add two short sections that **read env + source contracts**, no live betting:

1. `== bind ==`
   - Print `CALLISTO_BIND_HOST` (default `127.0.0.1` if unset).
   - FAIL (`ok=False`) if the effective host is `0.0.0.0` or `::`.
   - OK if loopback / unspecified default.

2. `== money switches ==`
   - Source-contract grep/read (do not instantiate BetExecutor/browser):
     `OrderManager.__init__` must assign `_enabled = False` (fail if the
     source shows default True).
     `BetExecutor.__init__` must assign `_enabled = False`.
   - Print `CALLISTO_LOCAL_ONLY` on/off (env only).
   - Print `CALLISTO_ALLOW_LIVE_EXECUTE` on/off (env only).
   - Do not call `enable()`. Do not print tokens.

Keep doctor fast. Stub heavy checks the same way `tests/test_cli_seal_doctor.py` does
if you import `_cmd_doctor`.

## Tests (`tests/test_cli_doctor_money.py`)

- `CALLISTO_BIND_HOST=0.0.0.0` → rc != 0, output contains FAIL and bind.
- Unset bind → does not fail the bind check (loopback default).
- Output contains `money switches` (or `== money ==`) and does not contain
  a hex seal key value you set in the env.
- Existing `tests/test_cli_seal_doctor.py` must still pass.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_doctor_money.py tests/test_cli_seal_doctor.py -q
```

If `test_cli_seal_doctor.py` is missing on this branch (branched from master
before that merge), skip it and only run the new file.

Commit: `fix(cli): doctor fails closed on 0.0.0.0 bind and reports money switches`

Write `OX_DONE.md` with SHA and test output.
