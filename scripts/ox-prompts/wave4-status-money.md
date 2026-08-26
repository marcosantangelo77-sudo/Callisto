# OX TASK: callisto status reports money/bind switches

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-status-money-2ac0`
Worktree: `/tmp/callisto-ox-status-money`

## Exclusive files (HARD)

You MAY edit:
- `callisto.py`
- `tests/test_cli_status_money.py` (create)

You MUST NOT edit `agp/`, `api.py`, betting tools, credentials, or `master`.
Do NOT remove the doctor seal/bind/money sections. Do NOT print secrets
(`CALLISTO_SEAL_KEY`, admin token).

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.
Based on current origin/master (doctor already has bind + money sections).

## Goal

`callisto status` (not only `doctor`) should show, in a few lines:

- bind host (default 127.0.0.1)
- CALLISTO_LOCAL_ONLY on/off
- CALLISTO_ALLOW_LIVE_EXECUTE on/off
- CALLISTO_ALLOW_SIGNAL_REFRESH on/off

Read env only. Do not instantiate BetExecutor. Do not fail the command
just because LOCAL_ONLY is off (that's doctor). Status is informational.

If `_cmd_status` is huge, add a helper `_print_appliance_switches()` used
by status (and optionally reused by doctor — only if that is a 5-line
dedup; do not rewrite doctor).

## Tests

- Unset env: output contains `127.0.0.1` and `ALLOW_LIVE_EXECUTE` / `off`
  (flexible matching).
- A hex `CALLISTO_SEAL_KEY` you set is never printed.
- `CALLISTO_ALLOW_LIVE_EXECUTE=1` shows on.

Stub heavy status internals like the doctor tests if needed.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_status_money.py tests/test_cli_doctor_money.py tests/test_cli_seal_doctor.py -q
```

Skip missing. Do not run full suite.

Commit: `fix(cli): status prints bind and money-switch env`

Write `OX_DONE.md`.
