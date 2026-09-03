# OX TASK (WAVE 2 — queued, do not start until orchestrator launches you)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in the worktree
the supervisor passed via WORKING DIRECTORY.

## Exclusive file ownership (HARD)

You MAY edit:
- `agp/__init__.py`
- `tests/test_tier3_epi_seal.py`

You MUST NOT edit other files, credentials, `config/providers.yaml`, or `master`.

## Git rules (HARD)

- Stay on the worktree branch. No stash / reset --hard / checkout --. No merge.
- Commit and `git push -u origin HEAD` when tests pass.

## Bug (verified)

`AGPSession.verify_seal` (`agp/__init__.py` ~457–487) always includes
`hashlib.sha256(payload.encode("utf-8")).hexdigest()` in `candidates`,
EVEN WHEN `CALLISTO_SEAL_KEY` is set.

So a keyed deployment still accepts a public SHA-256 of the payload. Anyone
who can write the DB can forge a seal without the key.

The hole is pinned as a feature by
`tests/test_tier3_epi_seal.py::test_legacy_unkeyed_seal_still_verifies_under_keyed_regime`
(~115–126): it seals unkeyed, then sets `CALLISTO_SEAL_KEY`, and asserts
`verify_seal` is True.

Learning-layer code already refuses unkeyed when a key is set
(`tools/memory_epistemics.py` ~162–174). Seals should match that.

`_seal_digest` already HMACs when keys exist. The extra unkeyed candidate in
`verify_seal` is the hole.

## Required behavior

When `_seal_keys()` is non-empty:
- Candidates are: current `_seal_digest(payload)` and HMAC of each key in
  `_seal_keys()` (current + `CALLISTO_SEAL_KEY_OLD` rotation).
- Do NOT add raw unkeyed SHA-256.

When `_seal_keys()` is empty:
- Keep unkeyed SHA-256 verification (legacy workstation with no key set).

## Tests — invert the pinning test

Rename/replace `test_legacy_unkeyed_seal_still_verifies_under_keyed_regime`:

- Seal with NO key (unkeyed SHA-256).
- Set `CALLISTO_SEAL_KEY`.
- Assert `AGPSession.verify_seal(stored) is False`.

Add:
- Unkeyed seal still verifies when NO key is set.
- Keyed seal verifies under the same key.
- After rotation (`CALLISTO_SEAL_KEY_OLD`), old keyed seals still verify
  (existing `test_rotation_old_key_accepted_current_rejected` should keep passing).
- Tamper still fails under key.

Keep constant-time compare (`hmac.compare_digest`).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_tier3_epi_seal.py -q
```

Commit: `fix(agp): keyed verify_seal must not accept unkeyed SHA-256`

Write `OX_DONE.md` with SHA and test output.
