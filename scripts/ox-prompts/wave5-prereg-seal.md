# OX TASK: preregistration verify_seal matches keyed fail-closed regime

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-prereg-seal-2ac0`
Worktree: `/tmp/callisto-ox-prereg-seal`

## Exclusive files (HARD)

You MAY edit:
- `agp/preregistration.py`
- `tests/test_preregistration_seal.py` (create)
- `tests/test_seal_invalid_key.py` (add cases only if needed)

Do NOT edit `agp/__init__.py` unless a one-line import is required (prefer not).
Do NOT edit `callisto.py`.

## Git rules

No stash / reset --hard / full suite. Push. Base origin/master (keyed
`AGPSession.verify_seal` + invalid-hex fail-closed already landed).

## Bug

`Preregistration.verify_seal` calls `_seal_digest` and compares. When
`CALLISTO_SEAL_KEY` is set-but-invalid, `_seal_digest` now **raises**
`AGPSealKeyInvalid`. `verify_seal` on AGPSession catches and returns
False. Preregistration's `verify_seal` may raise instead of returning
False, or may still write unkeyed hashes via a local path.

## Required

- `verify_seal()` never raises; returns False on invalid key / mismatch.
- `seal()` with invalid hex refuses (raise or refuse) — same as AGPSession.
- Unset key: legacy unkeyed still works.
- Valid key: HMAC; unkeyed SHA-256 of payload does not verify.
- Tests for the four cases above. No network.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_preregistration_seal.py tests/test_seal_invalid_key.py tests/test_agp_seal.py -q
```

Skip missing. Commit: `fix(agp): preregistration seals follow keyed fail-closed regime`

Write `OX_DONE.md`.
