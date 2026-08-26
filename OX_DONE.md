# OX DONE: invalid CALLISTO_SEAL_KEY must not fall back to unkeyed verify

Branch: `cursor/ox-seal-invalid-key-2ac0`
Commit: `a749d35641aceb328910d975767297f946b620fa`
Message: `fix(agp): invalid seal key is fail-closed, not unkeyed fallback`
Pushed to origin.

## Change (agp/__init__.py only)

- New `_seal_key_configured()`: True when `CALLISTO_SEAL_KEY` is set
  (non-blank), regardless of validity — distinguishes the legacy unkeyed
  regime from a keyed-with-no-usable-key regime.
- `_seal_keys()`: error log reworded to "failing closed"; parse logic
  unchanged.
- New exception `AGPSealKeyInvalid`.
- `_seal_digest()`:
  - key unset → unkeyed SHA-256 (legacy, unchanged);
  - valid key → HMAC-SHA256 (unchanged);
  - key set but invalid hex → raises `AGPSealKeyInvalid` instead of
    writing a forgeable SHA-256. So `seal()` refuses via that raise.
- `verify_seal()`:
  - unkeyed SHA-256 candidate only appended when NO key is configured;
  - `_seal_digest` call wrapped so an invalid key returns False (never
    raises, per its contract).
  - `CALLISTO_SEAL_KEY_OLD` valid keys still verify during rotation.

## Tests

New file `tests/test_seal_invalid_key.py` (7 cases):
invalid-hex verify rejects forged unkeyed hash; seal() refuses with
AGPSealKeyInvalid and leaves seal_hash None; fail-closed on previously
unkeyed-sealed sessions; unset key legacy path still verifies; valid key
rejects unkeyed digest; valid key HMAC roundtrip; OLD-key rotation
verifies when current key is bad.

## Test output

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_agp_seal.py tests/test_seal_invalid_key.py -q
.........................                                                [100%]
25 passed in 0.06s
```
