# OX_DONE — split callisto.py CLI commands

Branch: `cursor/ox-cli-split-2ac0` (pushed, commit `8a700fb`)
Scope: worktree `/tmp/callisto-ox-cli-split` only.

## What was done

Extracted the real command bodies from `callisto.py` into a new
`tools/cli/` package:

- `tools/cli/ask.py` — `cmd_ask`, `check_seal_key` (fail-closed), run-record
  serialization/persistence (`_result_record`, `_persist_run`, `_runs_dir`),
  provider seams (`_load_router`, `_make_engine`)
- `tools/cli/doctor.py` — `cmd_doctor`
- `tools/cli/status.py` — `cmd_status`
- `tools/cli/help.py` — `cmd_help`

`callisto.py` remains the entry script: argparse parser + `main()` dispatch.
It re-exports the extracted functions (`_cmd_ask`, `_cmd_doctor`,
`_cmd_status`, `check_seal_key`, …) so existing tests and callers keep
working unchanged. A new `help` subcommand routes through `tools.cli.help`.

## Seam compatibility

Tests monkeypatch attributes on the `callisto` module. The command bodies in
`tools/cli` resolve those seams lazily through the entry module
(`_entry()._load_router()`, `callisto._db_path`), so patching
`callisto._load_router` / `callisto._make_engine` / `callisto._db_path`
still takes effect. No test semantics changed.

## Safety

- `check_seal_key` still fails closed on unset/blank/non-hex keys:
  `ask` returns rc=2 and never starts research. Verified live.
- The seal key value is never printed anywhere (test added).
- No CALLISTO_SEAL_KEY generated; no live betting touched.

## Verification

Required suite — all pass:

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_ask_seal.py \
  tests/test_cli_split.py tests/test_cli_help_money.py \
  tests/test_cli_seal_doctor.py -q
=> 18 passed
```

Other CLI suites (`test_cli_doctor_money.py`, `test_cli_status_money.py`,
`test_cli_front_door.py`, `test_redteam_cli_persistence.py`) show exactly
the same failure set as at HEAD (12 pre-existing failures, verified by
diffing failure lists before/after) — no regressions introduced by this
change.

Smoke test: `ask` with empty key → FAIL + rc=2; `status` → rc=0;
`doctor` → rc=1 (reports problems on this box, expected);
`help` prints usage.

New file `tests/test_cli_split.py` pins: implementations live in
`tools/cli`, entry-script delegation, fail-closed seal gate via the
extracted module, key never printed, help subcommand works.

## Files touched

- `callisto.py` (modified: −384/+20)
- `tools/cli/{__init__,ask,doctor,status,help}.py` (new)
- `tests/test_cli_split.py` (new)
