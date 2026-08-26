# OX TASK: callisto ask fails closed without a valid seal key

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-ask-seal-required-2ac0`
Worktree: `/tmp/callisto-ox-ask-seal-required`

## Exclusive files (HARD)

You MAY edit:
- `callisto.py`
- `tests/test_cli_ask_seal.py` (create)

Do NOT generate or print `CALLISTO_SEAL_KEY`. Do NOT write `.env`.
Do NOT remove doctor/status/help sections. Do NOT call live betting.

## Git rules

No full suite. Push. Base origin/master.

## Bug

`callisto doctor` fails if the seal key is missing/invalid, but `callisto ask`
can still run and write **unkeyed** (forgeable) session hashes. Stage B
appliance honesty: the front door must not pretend a seal exists.

## Required

`_cmd_ask` (or a helper it calls before work): if `CALLISTO_SEAL_KEY` is
unset/blank or not valid hex → print FAIL (unkeyed/forgeable), return
nonzero, do not start research. Valid hex → proceed; never print the key.

If ask already shells out too heavily to test, mock the research call
after the seal check so tests stay fast.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_ask_seal.py tests/test_cli_seal_doctor.py tests/test_cli_help_money.py -q
```

Skip missing. Commit: `fix(cli): ask refuses to run with unkeyed forgeable seals`

Write `OX_DONE.md`.
