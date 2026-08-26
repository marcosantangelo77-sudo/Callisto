# OX TASK: split callisto.py CLI commands (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-cli-split-2ac0`
Worktree: `/tmp/callisto-ox-cli-split`

Extract `_cmd_ask`, `_cmd_doctor`, `_cmd_status`, `_cmd_help` implementations
into `tools/cli/` while `callisto.py` remains the entry script. Keep
`check_seal_key` fail-closed on ask. Do not print the seal key.

## Exclusive files (HARD)

You MAY edit:
- `callisto.py`
- `tools/cli/` (create)
- `tests/test_cli_ask_seal.py` (adapt imports)
- `tests/test_cli_split.py` (create)
- other `tests/test_cli_*.py` only if imports break

Do NOT generate CALLISTO_SEAL_KEY. Do NOT arm live betting.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- `python callisto.py ask ...` still routes through `_cmd_ask` which refuses
  unset/invalid seal keys (rc != 0, no research).
- Doctor/status/help still exist.
- Move real command bodies; leave argparse in callisto.py if easier.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_ask_seal.py tests/test_cli_split.py tests/test_cli_help_money.py tests/test_cli_seal_doctor.py -q
```

Skip missing. Commit: `refactor(cli): extract callisto commands to tools.cli`

Write `OX_DONE.md`.
