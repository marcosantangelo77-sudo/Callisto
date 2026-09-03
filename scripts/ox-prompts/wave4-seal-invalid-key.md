# OX TASK: invalid CALLISTO_SEAL_KEY must not fall back to unkeyed verify

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-seal-invalid-key-2ac0`
Worktree: `/tmp/callisto-ox-seal-invalid-key`

## Exclusive files (HARD)

You MAY edit:
- `agp/__init__.py`
- `tests/test_agp_seal.py` (add cases; do not invert existing keyed tests)
- `tests/test_seal_invalid_key.py` (create if cleaner)

You MUST NOT edit `callisto.py`, `agp/preregistration.py` unless a one-line
import break forces it (prefer not). No credentials. No master.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug (verified)

Keyed `verify_seal` now rejects public SHA-256 when `_seal_keys()` is
non-empty. But `_seal_keys()` on invalid hex logs an error and returns
`[]`, which makes `verify_seal` treat the process as **unkeyed** and
accept a forgeable SHA-256. `callisto doctor` fails closed on invalid hex;
runtime still lies.

Unset key = legacy unkeyed (backward compatible).
Set-but-invalid hex = **keyed regime with no usable key** → must NOT
accept unkeyed SHA-256, and `seal()` must not write a forgeable hash.

## Required change

Distinguish:

- no `CALLISTO_SEAL_KEY` (blank/unset) → current unkeyed fallback for
  seal + verify (legacy).
- set but not valid hex → do not append unkeyed SHA-256 in `verify_seal`;
  `seal()` should raise a clear error (or refuse to set `seal_hash`)
  rather than writing SHA-256. Logging already exists; keep it.

Do not accept unkeyed just because the key failed to parse.

If `CALLISTO_SEAL_KEY_OLD` has valid keys, those still verify (rotation).

## Tests

- Invalid hex in `CALLISTO_SEAL_KEY`, no old keys: `verify_seal` on a
  public SHA-256 of the payload returns False.
- `seal()` with invalid hex raises / refuses.
- Unset key: existing unkeyed path still verifies (legacy).
- Valid hex: still HMAC, still rejects unkeyed (the test that was inverted
  on `cursor/ox-seal-fail-closed-2ac0` — re-add it here if missing on master).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_agp_seal.py tests/test_seal_invalid_key.py -q
```

Skip missing files. Do not run the full suite.

Commit: `fix(agp): invalid seal key is fail-closed, not unkeyed fallback`

Write `OX_DONE.md` with SHA and test output.
