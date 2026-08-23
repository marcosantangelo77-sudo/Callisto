# RED TEAM FINDINGS — checkpointing & resume (method B: differential)

**Choice of surface:** checkpointing and resume. The morning report's central
resume claim — "a resumed run scores exactly what the equivalent live run
scored" (engine.py's own comment) and "resumption must never become a way to
launder evidence whose provenance was lost" (checkpoint.py docstring §4) — had
never been differentially tested end-to-end. Prior passes attacked confidence
arithmetic (property sweep) and the cutoff enforcer; the resume boundary is
where a crash, an attacker with file access, or plain partial corruption meets
the trust path.

**Method:** DIFFERENTIAL. Run one honest live run; run the same question
through a checkpointer twice (fresh + resumed); perturb ONE thing per test;
assert the resumed run may not come out more confident or less evidenced than
the live one. Baseline agreement is verified first (`test_baseline_honest_...`
passes).

**Test:** `tests/test_redteam_resume_boundary.py` (R1–R4 FAIL on current code,
baseline and R5 pin PASS).

**Relationship to prior work:** instance `build/checkpointing` already found
and fixed C1–C4 on branch `improve/rotating-0823-151045` (commits a7b951e,
a6466bc): C1 missing/empty digest bypass, C2 cross-run ledger pollution in
seal_guard, C3 vacuous "nothing to verify = verified", C4 unsigned produced_at.
This pass confirms those fixes hold under my re-probes (R5 pins C2) AND finds
four defects they do not cover. All four share one root cause:

## ROOT CAUSE — checkpoints are trusted, only verified against themselves

The HMAC signature added for C4 covers key|run|stage|input_hash|produced_at|
claim_ids|sha256(payload). It could authenticate everything. But **no resume-
path code ever calls verify_signature**: grep shows `verify_signature` /
`trusted_age_seconds` are consulted ONLY inside `gc()` (checkpoint.py:275).
`run_stage.load_by_key`, `replay_ledger`, `provenance_is_intact`, and
`seal_guard` all read payloads verbatim. Even under a keyed regime
(CALLISTO_CUTOFF_KEY set), every attack below still lands byte-identically —
I re-ran all probes with the key set. The signature exists; nothing on the
trust path checks it. This is W5's failure mode ("the cutoff signature system
was inert end to end") recurring in a second module.

---

## CONFIRMED BREAKS

### R1 — Tampered answer_leaf payload seals fabricated conclusions (CRITICAL)
Rewrite the answer_leaf checkpoint: leaf answer → any text, leaf confidence →
0.99, evidence source_class → PRIMARY / confidence 1.0. Resumed run:
- leaf answer flows **verbatim into the sealed conclusion**
  (`"- [PROBABLE 0.99] ...: FABRICATED: buy now"`),
- tampered PRIMARY evidence enters session.evidence at score 1.0,
- seal succeeds.
The final parent score happens to stay 0.55 only because the inheritance rule
caps zero-descendant claims — an accident of a different guard, not a defense.
Nothing authenticates the answer/evidence payload. Repro:
`test_R1_tampered_answer_checkpoint_cannot_inflate`.

### R2 — Self-consistent fetch forgery defeats the integrity check (CRITICAL)
replay_ledger verifies `_sha(body) == content_sha256` where BOTH fields live in
the same unauthenticated JSON file. Forge both consistently: the forged
document enters the resumed run as primary ledger bytes and the run SEALS over
evidence no source ever returned. The check hashes a file against itself.
Repro: `test_R2_self_consistent_fetch_forgery_must_not_seal`. (Under the
keyed regime this specific rewrite would break the sig — but the sig is never
checked on this path, so it does not.)

### R3 — Deleted fetch checkpoints: sealed on stale evidence with zero fetches (HIGH)
Delete only the fetch_leaf files (partial GC, disk error, attacker). The
guard iterates `cp.list_all()` — absent files are invisible to it, so C3's
"fetches-key-must-exist" fix cannot fire. Meanwhile answer_leaf's cached
payload rehydrates session.evidence, so AGP's zero-evidence refusal doesn't
fire either. Result: sealed=True, n_fetches=0, n_evidence=1, resumed stages =
[decompose, answer_leaf]. A run whose retrieval layer vanished entirely seals
on memory alone. Repro: `test_R3_deleted_fetch_checkpoint_refuses_to_seal`.

### R4 — Evidence age is recorded but enforced by nobody (HIGH)
C4 signed produced_at and made gc() read authenticated age. But the SEAL path
still consults age nowhere: backdate produced_at 400 days (unkeyed regime) or
simply leave old-but-valid records and skip GC (nobody calls `.gc(` anywhere
outside tests — zero production call sites), and the resumed run seals year-old
evidence at full confidence with empty notes. `oldest_produced_at()` is
honestly reported into `result.trace` and then read by no consumer. The
docstring's promise — "the caller decides what staleness means" — delegates a
decision no caller makes. Repro:
`test_R4_year_old_evidence_cannot_seal_silently`.

---

## HONEST NEGATIVES

- Honest fresh-vs-resume differential PASSES: same question/day through the
  checkpointer twice scores identically to live (0.55 == 0.55), trace reports
  all stages resumed. The happy path is honest.
- C1/C2/C3/C4 regression suites (test_redteam_c1..c4) all pass on this branch;
  R5 re-pins C2 from the engine side because engine.run STILL hands
  seal_guard the entire store (`cp.list_all()`) — correctness rests solely on
  seal_guard's internal `ck.run == trace.run` filter and its scratch-ledger
  fresh-run branch. That filter is now load-bearing with no redundancy; if a
  future caller builds a RunTrace with the wrong run id, the leak reopens.
- Cross-run fetch_leaf input-hash collision: step keys bind run_key, which
  binds the root question+day — I could not collide two different questions'
  checkpoints. Clean.
- The inheritance rule (clamp_parent_confidence) incidentally caps R1's score
  inflation for zero-descendant parents; it is not a resume control and gives
  no protection once descendants resolve.

## WHAT TO FIX (ordered by leverage)

1. **Verify the signature wherever a checkpoint is trusted, not just in
   gc().** `run_stage`'s cache-hit path and `seal_guard` should refuse an
   invalid-signature checkpoint (fail closed: re-execute the stage, don't use
   it). One predicate, used at both sites — the C3 comment about membership
   rules landing three times applies here too.
2. **Make replay_ledger verify against something outside the file:** the
   signature already covers sha256(payload); checking `verify_signature`
   before trusting `content_sha256` closes R2 without new crypto.
3. **Refuse a resume whose fetch stage is missing but whose later stages
   exist** (a gap in the stage sequence = provenance lost → REFUSE, per the
   module's own contract). Closes R3.
4. **Give staleness a consumer:** seal_guard should REFUSE (or the result must
   carry a loud stale flag) when `trusted_age_seconds` exceeds a policy bound.
   Optionally wire gc() into pipeline startup so C4's mechanism actually runs.

Blast radius: LOUD for R2/R3 (fabricated/stale evidence sealed as verified),
SILENT for R4 (looks like epistemic caution, is amnesia).
For: whoever owns build/checkpointing next.
