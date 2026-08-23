# FIX — D3: seal_guard and the ledger inspected different worlds

Finding: findings/redteam_resume_differential.md (D3).
Repro ported to: tests/test_redteam_d3_split_world.py
Branch: fix/d3-split-world

## The defect

engine.py replayed each leaf checkpoint into the live ledger via
`load_by_key` with no run filter, no signature check, and no integrity
check; `seal_guard` then scoped `cp.list_all()` down to `ck.run ==
trace.run` (the C2 fix) before judging. Two codepaths independently
assembled "what evidence exists", so relabeling `ck.run` in the file made
the guard blind to bytes the ledger had already absorbed — the seal covered
a world the guard never inspected.

## The fix — one codepath

`tools/pipeline/checkpoint.py` now defines ONE predicate,
`partition_admissibility(trace_run, checkpoints)`:

- ADMISSIBLE = belongs to THIS run AND HMAC verifies under the harness key.
- REJECTED = everything else, RETURNED, not dropped.

Consumers:

1. **seal_guard()** calls it once. The rejected bucket VETOES the seal
   ("N checkpoint(s) failed authentication or belong to another run;
   refusing to seal") — dropping silently would leave the guard judging an
   empty world and sealing anyway. It judges only the admissible bucket,
   exactly as before (fresh path via scratch ledger, resume path via
   provenance_is_intact).
2. **engine.py** fetch_leaf replay calls `admissible_checkpoints()` — the
   admissible half of the SAME function — and replays only that set. A
   signature that fails is never replayed into the ledger at all.

No duplicated logic: neither site reimplements the run-scope or signature
rule. The old inline `[ck for ck in checkpoints if ck.run == trace.run]`
in seal_guard is gone; the C2 filter and the D1 signature check now live in
exactly one place, consumed by both paths.

Keyed vs unkeyed: under CALLISTO_SEAL_KEY/CALLISTO_CUTOFF_KEY the HMAC is
enforced everywhere through the shared predicate. With no key configured
(unkeyed default deployment) signature verification cannot run, so only
the run-scope filter applies — unchanged behavior there, and D1 remains the
finding that owns making keys mandatory.

## Tests

tests/test_redteam_d3_split_world.py:

- test_d3_run_relabel_blinds_guard_but_not_the_ledger — foreign-run record:
  ledger must not absorb what the guard scopes away; guard and ledger must
  agree about which evidence exists.
- test_d3b_guard_verdict_and_ledger_state_diverge_on_integrity_failure —
  corrupt digest refuses even on a fresh trace (pinned negative, still holds).
- test_d3c_bad_signature_checkpoint_is_never_replayed — keyed regime, body
  rewritten AND content_sha256 recomputed on disk: HMAC fails, checkpoint is
  inadmissible through the shared predicate, replay mints nothing PRIMARY,
  guard REFUSES.

All 3 pass. Suite: 11137 passed; failure count identical to pre-change
baseline (21 tracked + 17 tests-first repros from peer commit c8719de,
which fail with or without this change). No confidence score raised.
