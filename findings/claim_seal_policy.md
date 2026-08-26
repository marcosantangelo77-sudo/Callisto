# Claim-journal seal policy — explicit, strict, fail-closed

Branch: codex/claim-journal-tail-seal. Supersedes the policy described in
findings/instance4.md for the CLAIM JOURNAL ONLY (AGP session/prereg seals
keep their own legacy-compatible scheme).

## Policy boundary (exact)

A single strict parser (`_parse_seal_policy` in agp/claims.py) is used by
BOTH `save()` and `load()`. It resolves one of:

- **keyed** — HMAC-SHA256 under `CALLISTO_SEAL_KEY`; rotation via
  `CALLISTO_SEAL_KEY_OLD`. Selected by a valid current key (with no explicit
  policy) or an explicit `CALLISTO_SEAL_POLICY=keyed`. Old keys are
  verification-only; they NEVER authorize new writes.
- **unkeyed** — public SHA-256 checksums. Only via EXPLICIT opt-in:
  `CALLISTO_SEAL_POLICY=unkeyed` or `ClaimStore(dir, seal_policy="unkeyed")`.
  This is tamper-EVIDENCE, not authenticity: anyone who can write the file
  can recompute the digest.
- **unspecified** — fail closed on both save and load. Silence is never
  interpreted as "unkeyed".

Fail-closed rules:
- Every nonblank configured key token must be valid hex; malformed tokens
  are never silently dropped (mixed valid/invalid rings are configuration
  errors on save AND load).
- Malformed current/old-only configurations raise SealPolicyError before
  any write or verification.
- Explicit unkeyed + any seal-key variable = contradictory config, rejected.

## Honest limits (documented, not fixable in-format)

1. **Regime marker is not a provenance anchor.** The `{alg}` field inside a
   seal is covered by the digest but is attacker-rewritable together with
   the file. An attacker controlling BOTH the external policy/configuration
   AND the journal can re-seal under any regime (pinned by test:
   test_envelope_rewrite...). The journal defends only against attackers
   who cannot alter the external configuration.
2. **Tail truncation.** A filesystem writer can delete validly signed tail
   lines WITHOUT the key; absent an external head/count anchor, load()
   success does not prove lines were not removed.

## Legacy migration

Historical bare-string seals and wholly unsigned journals never silently
load or silently append. `ClaimStore.migrate_legacy_journal(claim_id,
attest_unverified=...)` performs an atomic (temp-file + os.replace) re-seal
under the current policy; unverifiable entries require explicit operator
attestation and are permanently marked `"migrated_unverified": true`.

The old footgun — `load(verify=False)` followed by `save()` appending a
new envelope onto permanently unloadable history — has no safe silent
route anymore; migration is the only sanctioned path.

## Operator migration notes

Deployments relying on implicit unkeyed behavior MUST now set
`CALLISTO_SEAL_POLICY=unkeyed` explicitly (or accept fail-closed).
Keyed deployments need no change beyond ensuring CALLISTO_SEAL_KEY is
valid hex wherever set.
