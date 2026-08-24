# RED TEAM — sealing, provenance and the crypto

**The claim under attack:** a sealed conclusion cannot be forged; a
preregistration cannot be edited after sealing; a belief timeline cannot be
rewritten so its chain still verifies; evidence cannot be laundered into a
class it did not earn.

**Verdict: FALSE on every axis except the narrow in-process one.** The HMAC
upgrade made forgery require *the key* — but the key handling itself is
fail-open, and every persistence path (`from_dict`, `ClaimStore.load`,
`annotate_for_reinjection`) trusts stored state without re-verifying the seal
it cites. The immutability lock guards one attribute-write path out of five.
Run:

```
python3 -m pytest tests/test_redteam_seal_prereg.py \
                   tests/test_redteam_seal_seal_and_chain.py \
                   tests/test_redteam_seal_prov_and_memory.py -q   # 30 passed
```

Sibling findings R1–R8 (`findings/redteam_provenance.md`) are not repeated;
this file covers what they missed.

---

## CONFIRMED DEFECTS

### Z1. Invalid-hex `CALLISTO_SEAL_KEY` silently downgrades to forgeable seals (CRITICAL)
`agp/__init__.py::_seal_keys` catches the `ValueError`, logs, returns no key;
`_seal_digest` then falls back to **unkeyed SHA-256**. A typo'd env var on the
workstation mints seals anyone with repo access can forge — exactly the threat
the HMAC upgrade exists for — while every log line says "keyed". The memory
side (`memory_epistemics._seal_keys`) does the same with no log at all.
**Tests:** `test_forged_unkeyed_seal_verifies_while_key_is_set`,
`test_digest_is_unkeyed_when_key_invalid`.
**Fix:** fail closed — an invalid configured key must raise at startup, never
degrade to the unkeyed scheme.

### Z2. Preregistration immutability holds on one path of five (CRITICAL)
`__setattr__` blocks plain writes. Everything else passes:
- `object.__setattr__(p, "query", ...)` — trivial bypass.
- `p.__dict__["criteria"].confirm_markers.append(...)` — criteria is a mutable
  object in `__dict__`; mutating it doesn't touch `__setattr__`. And **score()
  never calls verify_seal()**, so tampered criteria score CONFIRMED against a
  seal that no longer matches anything.
- `deepcopy` yields a freely-mutable copy that still carries `_sealed=True`
  and the original `seal_hash`.
- Pickle round-trip of a pre-tampered object presents as sealed.
- `Preregistration.from_dict` sets `_sealed = bool(seal_hash)` and verifies
  nothing: rewrite the criteria dict, keep the old hash, load → CONFIRMED on
  zero evidence with post-hoc markers.
**Tests:** `tests/test_redteam_seal_prereg.py` (all).
**Fix class:** score() and from_dict must call verify_seal() first (fail loud,
as claims.load does for its chain); freeze criteria via `types.MappingProxy`/
tuple markers or verify-on-read.

### Z3. Amendments are writable and never verified (HIGH)
`amendments` is deliberately mutable post-seal, and `amend()` seals each
record — but nothing verifies an entry the object didn't create. Appending or
splicing a forged record changes `effective_criteria` (used by default in
score()) to attacker-chosen gates, including `min_evidence_items=0`. The
disclosure line names the chain length but checks no seals.
**Test:** `test_forged_amendment_becomes_effective_criteria`,
`test_spliced_amendment_reorders_chain_undetected`.

### Z4. Claim opens on a fabricated preregistration seal (HIGH)
`Claim.seal_preregistration`: `seal = prereg.seal() if not prereg.seal_hash
else prereg.seal_hash`. Set any truthy hash on an unsealed prereg → claim
opens, belief record cites "opened under prereg seal deadbeef…". The whole
"no claim without sealed criteria" gate reduces to a truthy string.
**Test:** `test_claim_open_with_never_verified_prereg`.

### Z5. ClaimStore chain binds lines to each other, not to content (CRITICAL)
The journal hash-chains each line to its predecessor's bytes — with **no
secret and no external anchor**:
1. **Tail truncation is undetectable.** Delete the last two lines (evidence +
   retraction) and `load()` returns the earlier OPEN state without error.
   Every remaining link is intact by construction.
2. **Fabricated append verifies.** Compute `sha256(last_line)`, append a state
   saying CONFIRMED 0.99 with correct `prev` — loads as gospel.
3. **Whole-file forgery verifies.** Rebuild all four lines GENESIS-rooted from
   one fabricated state; indistinguishable from genuine history.
Tamper-*evident* requires an anchor outside the writable file (a keyed MAC per
entry — the seal HMAC already exists — or a signed head published elsewhere).
**Tests:** `TestClaimStoreChain` (three repros).

### Z6. Key rotation accepts forever, and the two verifiers disagree (MEDIUM)
No key-id, no expiry: a seal under any historical key in
`CALLISTO_SEAL_KEY_OLD` validates indefinitely — a compromised retired key
forges rows forever unless the operator remembers to prune the env var.
Worse, agp splits OLD on `,` (tries every rotation key) while
`memory_epistemics` takes only the FIRST entry: a second-key seal is valid
provenance in one layer and collapses to INFERRED in the other.
**Tests:** `test_old_key_seal_accepted_after_rotation`,
`test_memory_layer_rejects_same_seal_agp_accepts`.

### Z7. PRIMARY minted without any fetch (HIGH, structural)
`record_tool_result`'s "call from the executor only" is a comment, not an
invariant — the ledger is a plain object shared with everything else in the
process. Any caller can mark arbitrary bytes `primary=True`; the flag is
caller-asserted and never verified against a transport record. Combined with
Z2-style boundary crossing, INFERRED text becomes PRIMARY (ceiling 1.0).
**Tests:** `TestPrimaryMinting`.

### Z8. Replay guard degrades on further degenerate inputs (HIGH)
Beyond sibling R1–R3: a record whose digest equals `sha256("")` but which
carries no body replays as a PRIMARY observation of the empty string;
`provenance_is_intact([])` is vacuously True so a resumed run with LOST
checkpoints seals cleanly; an integrity-failed record still lets the remaining
records replay into the ledger (partial laundering), with failures advisory.
**Tests:** `TestReplayGuardDegrades`.

### Z9. Decay is computed then discarded at reinjection (HIGH)
`_build_learnings` correctly computes `decay_confidence(...)` and passes it as
`effective_confidence` — then `annotate_for_reinjection` OVERWRITES that field
with `min(raw stored, ceiling)`, discarding decay entirely. A 100-day-old 0.55
learning emits into prompts as 0.55 effective. Combined with
`record_learning`'s learned_at reset on every upsert, decay is effectively
decorative today.
**Tests:** `test_annotation_clobbers_decay`, `test_decay_reset_by_reobservation`.

### Z10. Trusted-source bypass is an unauthenticated string (HIGH)
`admit_learning` exempts `source in {"human","audit"}` from ALL ceilings — but
source is a caller-supplied argument. Any agent writing a learning passes
`source="human"` and stores 0.999 INFERRED-class content, clearing the wiki's
>= 0.5 admission and every prompt ceiling. The wiki also selects learnings by
raw stored confidence (no decay at compile time).
**Tests:** `test_trusted_source_bypasses_all_ceilings`,
`test_learning_admission_threshold_is_stored_not_effective`.

---

## WHAT I COULD NOT BREAK

1. **Seal replay onto altered content** — canonical payload recomputation
   catches any field change (re-confirmed; sibling found the same).
2. **Live-session ledger demotion** — in-process, with honest callers,
   `assign_source_class` caps correctly; every break is at a boundary.
3. **Article merge raising confidence** — `_merged_article_confidence` clamps
   to the weakest input; pinned as a passing boundary test.
4. **`dataclasses.replace` on Criteria swapped into a live object** — works
   mechanically, but the demoted gates still demote the verdict (AMBIGUOUS),
   so this path lowers rather than inflates; noted, not weaponised.

## PRIORITY ORDER

Z1 first (one-line fail-closed fix, restores the entire HMAC story), then Z5
(key the journal entries), then Z4+Z2/Z3 (verify_seal in score/from_dict/
claim-open), then Z9/Z10 (decay + trust bypass), then Z7/Z8.

## THE SIBLING PATTERN, AGAIN

Every confirmed defect is the same shape as R1–R8: **a verification that
degrades to a pass under its degenerate input** — invalid key → no key,
truthy string → sealed seal, missing anchor → chain still "verifies", decay
computed → overwritten, trusted string → ceiling skipped. The fix class is
uniform and small: explicit else-fail branches and verify-on-read wherever
stored state crosses a trust boundary.

*(18 pre-existing failures elsewhere in the redteam suites reproduce
identically with and without these files — untouched by this branch.)*
