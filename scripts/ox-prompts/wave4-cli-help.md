# OX TASK: callisto --help / ask help mentions fail-closed money defaults

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-cli-help-2ac0`
Worktree: `/tmp/callisto-ox-cli-help`

## Exclusive files (HARD)

You MAY edit:
- `callisto.py` (argparse help / epilog only, plus maybe a one-line status already present)
- `tests/test_cli_help_money.py` (create)

Do NOT remove doctor/status money sections. Do NOT print secrets.

## Git rules

No full pytest. Base origin/master after status-money if present; if
`_cmd_status` already prints switches, do not duplicate — only argparse
epilog/help.

## Required

`python callisto.py --help` (or `ask --help` / module epilog) must mention
that live execution is off unless `CALLISTO_ALLOW_LIVE_EXECUTE=1` and that
the API defaults to loopback. Keep it ≤6 lines. No key generation.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_help_money.py tests/test_cli_status_money.py tests/test_cli_doctor_money.py -q
```

Skip missing files.

Commit: `fix(cli): help text states live-execute and bind defaults`

Write `OX_DONE.md`.
