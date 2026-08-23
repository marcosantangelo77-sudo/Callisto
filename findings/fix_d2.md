# fix_d2 — the mandatory-fetches rule was keyed on a label, not content

**Finding:** D2 (HIGH), from the red-team resume differential pass
(0d05fcc, `findings/redteam_resume_differential.md`). Attack on the C3 fix
that landed same-day in a6466bc.

## The break

`_is_fetch_stage()` gated the C3 mandatory-structure rule on
`"fetch" in stage`. But `stage` is a plain string in an editable JSON file —
attacker-controlled state on disk. Renaming a fetch-bearing checkpoint to
`"decompose"` in-file:

- hid its records from every `_is_fetch_stage` structural check,
- while `replay_ledger` (which reads only `payload["fetches"]`, never the
  stage name) still minted their bytes PRIMARY,
- and `seal_guard` sealed.

A label is not evidence. Repro: `test_d2` in
`tests/test_redteam_resume_differential.py`; ported as
`tests/test_redteam_d2_stage_rename.py`.

## The fix

`_is_fetch_stage(ck)` now takes the whole **checkpoint**, not the name:

- true if the NAME admits to fetching (`"fetch" in stage`) — so the C3
  mandatory-`fetches`-key rule still fires on schema drift in genuinely
  fetch-shaped payloads;
- OR true if the PAYLOAD carries any `fetches` records — coverage follows
  content, whatever the file claims to be.

Kept as ONE predicate; the call site in `provenance_is_intact` just passes
the checkpoint. No scattering.

Verified behavior:
- renamed stage + corrupt digest → REFUSE (was SEAL before the fix)
- renamed stage + intact records → SEAL with bytes verified (coverage, not
  punishment)
- decompose without fetches → unaffected (no false positives)

## Signature-vs-content: what I chose and why

The a6466bc HMAC covers the whole record INCLUDING `stage`, so a rename is
detectable in principle — I pinned that (`test_d2f`: signature fails after
an in-file rename). But it does not make the content check redundant:

1. **Keyed-only.** The documented default deployment sets no key
   (`_harness_key()` reads `CALLISTO_CUTOFF_KEY`/`CALLISTO_SEAL_KEY`, both
   optional; `.env.example` documents neither). Unsigned records verify
   nothing.
2. **Nothing verifies it anyway.** No consumer on the load/replay/seal path
   calls `verify_signature()` on a fetched record — that is D1 (CRITICAL,
   still open). An unverified signature is decoration.

Chosen: **both layers, different jobs** — content-based rule as the always-on
floor (works unkeyed, works today); HMAC verification as the keyed-regime
layer to be wired at the D1 seam. Not either/or.

## Residual gap (honest)

Content cannot see what is ABSENT: a genuinely-fetch payload whose `fetches`
key was stripped entirely and renamed to `decompose` looks like a legitimate
decompose checkpoint to any content rule. Only authenticated records close
that — i.e., D1 (verify signatures on load) plus a keyed deployment. Until
then this residual is bounded by the same trust boundary as everything else
in an unkeyed store: an attacker who can write files can rewrite evidence;
the guard's job is to refuse when evidence does not verify, which this fix
restores for every record that survives inspection.

## Verification

- `tests/test_redteam_d2_stage_rename.py`: 6 passed (ported repro + pins).
- Full suite (excl. 2 xgboost collection errors, pre-existing): 21 failed /
  11136 passed / 8 skipped — identical failure set to base ee549f8 (diffed
  FAILED lists). No confidence score raised anywhere.

Commit: 78ea087 on `fix/d2-stage-rename`.
