# OX TASK: callisto doctor must fail-closed on missing seal key

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-cli-seal-warn-2ac0`
Worktree: `/tmp/callisto-ox-cli-seal-warn`

## Exclusive files (HARD)

You MAY edit:
- `callisto.py`
- `tests/test_cli_seal_doctor.py` (create)

You MUST NOT edit `agp/__init__.py` (another worker owns seals in the kernel).
You MUST NOT edit other files, credentials, or `master`.

## Git rules (HARD)

Stay on this branch. No stash / reset --hard / checkout --. No merge.
Commit and `git push -u origin HEAD` when tests pass.

## Why

Unkeyed SHA-256 is a checksum, not a seal. `callisto doctor` currently does
not mention `CALLISTO_SEAL_KEY`. An operator can think the box is healthy
while every sealed session is forgeable.

## Required change

In `_cmd_doctor`:
- If `CALLISTO_SEAL_KEY` is unset/blank: print a clear FAIL/WARN line that
  seals are unkeyed/forgeable, and treat doctor as not-ok (`ok = False`).
- If set: print that a seal key is present. Do NOT print the key, prefix,
  or length beyond "set" / hex-ok. If the value is not valid hex, FAIL
  (same as `agp._seal_keys` ValueError path) without printing the value.

Do not generate a key automatically in this task (no writing .env).

## Tests

`tests/test_cli_seal_doctor.py` can call `_cmd_doctor` with a dummy
providers path or monkeypatch provider loading so doctor gets far enough
to hit the seal check. Capture stdout. Env monkeypatch:
- unset → return code != 0 and stdout mentions seal / CALLISTO_SEAL_KEY
  without dumping a secret
- valid 64-char hex → that check passes (other doctor failures may still
  make overall not-ok; assert the seal line is OK / present)

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_cli_seal_doctor.py -q
```

Commit: `fix(cli): doctor fails closed when CALLISTO_SEAL_KEY is missing`

Write `OX_DONE.md` with SHA and test output.
