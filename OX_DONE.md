# OX DONE — doctor fails closed on missing seal key

- Branch: cursor/ox-cli-seal-warn-2ac0
- Commit: c2659b72f4e52cb10bc8fa7c481d2b9cda4fec11
- Pushed: origin/cursor/ox-cli-seal-warn-2ac0

## Change

`callisto._cmd_doctor` gained a `== seal ==` section:
- CALLISTO_SEAL_KEY unset/blank → FAIL lines ("seals are unkeyed … forgeable"), `ok = False`, rc=1.
- Set but not valid hex → FAIL ("not valid hex"), `ok = False`; value never printed.
- Valid hex → "OK: seal key is set (hex-valid); seals are HMAC-SHA256"; value never printed.

No key generation / .env writing.

## Test output

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_cli_seal_doctor.py -q
....                                                                     [100%]
4 passed in 0.09s
```

Note: full-suite run shows 6 pre-existing collection errors in unrelated
modules (`ModuleNotFoundError: No module named 'joblib'` etc.) — present on
the base branch, not caused by this change.

Files touched: callisto.py, tests/test_cli_seal_doctor.py (exclusive list only).
