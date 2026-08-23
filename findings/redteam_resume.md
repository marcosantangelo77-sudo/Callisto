# RED TEAM FINDINGS — checkpointing and resume

**Surface:** checkpointing and resume (`tools/pipeline/checkpoint.py`,
`tools/pipeline/engine.py` run/resume path). Not previously covered — the
existing red-team file attacked confidence scoring; W3's own tests
(`test_build_w3_checkpoint.py`, `test_fix_ckpt_confidence.py`) cover the
happy paths and the single-run isolation case.

**Method: B — DIFFERENTIAL**, rotating both surface and style from the last
pass (which used property sweeps + adversarial constructions on confidence
math). The same logical question is driven through three executions that must
agree — plain live, fresh-with-checkpointer, resumed-from-checkpoints — and
two different questions are driven through one shared store, which must not
interact. A C-flavored incentive read (what is a retry loop rewarded for?)
finishes the pass. Differential found all six breaks below; none was findable
by reading `checkpoint.py` alone, because five of them live in how *engine.py*
uses it.

**Files:** `tests/test_redteam_resume_differential.py`
Run: `python3 -m pytest tests/test_redteam_resume_differential.py -q`
Current result: 9 failures exactly as described, 5 pins passing.

**Scope honesty:** the interactive engine (`callisto.py:_make_engine`)
currently passes no checkpointer, so today these paths are reachable via
`scripts/run_retro_batch.py` — which builds `FileCheckpointer()` with the
SHARED default store (`$STATE_DIR/callisto/checkpoints`) and no
`is_claim_open` — and via any future adoption of the injected checkpointer.
Retrodiction batches feed `ModelScoreStore` routing scores, so anything that
biases which runs seal biases calibration too.

---

## CONFIRMED BREAKS

### R1 — Digest-less fetch records mint PRIMARY provenance across the resume boundary (CRITICAL)
`checkpoint.py:337-349`. In `replay_ledger`, the integrity check runs only
`if digest and _sha(body) != digest`. A fetch record whose
`content_sha256` is **missing or empty** skips verification entirely, is
recorded into the ledger with `primary=bool(rec.get("primary", True))` —
**defaulting upward to PRIMARY** — and its bytes become ceiling-1.0
provenance. Then `provenance_is_intact()` (line 364-366) validates the very
same bytes against the ledger entry the replay just created from them:
**the anti-laundering oracle is populated by the data under test.**
Demonstrated end-to-end: attacker-chosen bytes in a checkpoint payload come
out as `SourceClass.PRIMARY` with `seal_guard == ("SEAL", "")`.

This is contract 4 of the module docstring verbatim inverted: "Resumption
must never become a way to launder evidence whose provenance was lost."

Reachability: FetchResult serialization always writes a digest *today*, so
this needs a legacy checkpoint, a format drift, a buggy writer — or an
autonomous component editing state files, which is squarely inside this
system's stated threat model (the doom loop rewrote its own history). Note
the cross-module asymmetry (method F): learnings need HMAC seals to claim a
class (`memory_epistemics`), cutoff proofs got signatures after W5 —
checkpoint fetch records are the third "stored bytes back a provenance class"
mechanism and the only one still unauthenticated. Even with the digest
present, `sha256(body)` against a digest stored **in the same writable file**
detects bitrot only; it defends against nothing that controls the writer.

### R2 — One rotted checkpoint from a finished run blocks every fresh seal on the machine (HIGH)
`engine.py:611` passes `cp.list_all()` — every checkpoint of EVERY run ever
written under the store root (`checkpoint.py:204` globs `*/*.json`) — into
`seal_guard`. Demonstrated: run A completes; one byte of A's stored body rots
(valid JSON, mutated payload); a brand-new run B on a different question,
with zero checkpoints of its own, gets `REFUSE` for up to the 30-day GC
window. Fail-closed, so this is availability, not integrity — but it is a
persistent machine-wide seal outage triggered by bitrot, and the refusal
reason ("checkpointed evidence provenance could not be verified") blames B's
own evidence and names neither the file, the run, nor the stage. The guard's
own comment says its purpose is "checkpointed evidence **whose** integrity
fails" — the implementation's scope is global, the intent's scope is per-run.

### R3 — A refused run can be re-rolled for free until the critic passes, and the seal hides it (HIGH — incentive)
After attempt 1 dies to an adversary BLOCKING veto, attempt 2 of the same
question on the same day hits every stage cache — **zero model calls, zero
fetch calls** (asserted in the test) — and re-rolls only the stochastic
critic. The veto gate was not lowered; it was sampled until it passed, with
the evidence frozen identical between samples. Nothing marks checkpoints
consumed at a terminal outcome (sealed or refused), nothing counts attempts,
and neither `result.notes` nor `summary_dict()` discloses resumed-ness —
`summary_dict()` drops the trace entirely (`engine.py:127-139`). The sealed
result is indistinguishable from a first-attempt seal.

This is the morning-report doom-loop class found by incentive analysis
rather than by code reading: an actor rewarded for sealing (an autonomous
loop, an operator wanting a number) gets near-free retries against the one
gate that can stop it, and the bookkeeping cannot show it happened. It also
biases retrodiction/routing scores, since refusal→retry→seal converts a
critic-rejected prediction into a scored observation.

### R4a — Sealed evidence_count depends on HOW the run executed (MEDIUM)
`engine.py:519-542`. On a fresh checkpointer-equipped run, each leaf's
evidence is added to the session TWICE — once inside `_answer_leaf`
(line 335, via `_answer`), then again unconditionally from the stored payload
(lines 534-542). A resumed run adds it once; a plain no-checkpointer run adds
it once. Measured triple divergence on the identical question:
`plain=1, fresh-with-checkpointer=2, resumed=1`. The sealed session's
`evidence_count`, adversary prompt (duplicate items read as corroboration),
and seal hash all depend on execution mode rather than evidence.

### R4b — Resume re-stamps evidence timestamps (MEDIUM)
`engine.py:535-541` rebuilds `Evidence` without its `timestamp` field, so it
re-defaults to the resume moment (`agp/__init__.py:146`). The Checkpoint
keeps contract 2 ("resume semantics that do not lie") honest in
`RunTrace.produced_at` — and the sealed session immediately violates it:
evidence acquired hours/days ago is sealed claiming acquisition NOW.
Anything keying on evidence timestamps (staleness audits, retrodiction
cutoffs downstream) inherits the lie.

### R5 — GC's open-claim protection is decorative everywhere in production (MEDIUM)
Module contract 5 promises gc never deletes an open claim's checkpoints.
The protection requires wiring `is_claim_open`; the only production
constructor (`scripts/run_retro_batch.py:67-68`) and the default both leave
it None — "nothing is ever open" (`checkpoint.py:146-148`). Demonstrated:
a 40-day-old checkpoint carrying `claim_ids=["claim-still-open"]` is silently
deleted. Crash-recovery state for open work can vanish without a word. (The
pin shows the mechanism works when wired — the defect is that nobody wires
it.)

### R6 — One naive `produced_at` crashes the whole GC pass (LOW)
`Checkpoint.age_seconds` / `gc` compare an aware cutoff to
`datetime.fromisoformat(produced)` outside any try block. A legacy or
hand-edited file with timezone-naive `produced_at` raises TypeError and kills
collection for ALL checkpoints instead of skipping one bad file.

---

## HONEST NEGATIVES — attacks that did NOT land (pins, kept green)

- Body tampered WITH the digest retained → integrity check catches it;
  `REFUSE`. The check does its bitrot job (pin).
- Unreadable/garbage JSON checkpoint → miss everywhere, never poison
  (pin). Only valid-JSON-with-mutated-payload poisons (R2).
- Replay idempotence holds: replaying twice records nothing twice (pin).
- Resumed ≤ live confidence in single-run isolation still holds (pin; the
  existing property test covers a wider input space).
- `fetch_leaf` keyed only on `{"qid": ...}` was probed as a stale-fetch
  laundering vector: question_ids are uuid4-derived and travel inside the
  cached decomposition, so new-text-under-old-fetches would need the
  decompose checkpoint to vanish while fetch_leaf survives AND a uuid
  collision. Not reachable; noted as design fragility, not a break.
- Crash window between `execute()` and `save()`: re-execution duplicates
  model/fetch calls (cost), but ledger hash-keying and content-addressed
  artifacts produced no duplication or inflation I could demonstrate.

## WHAT TO FIX (ordered by leverage)

1. **Fail closed on unverifiable records** (`replay_ledger`): a fetch record
   without a truthy digest is an integrity failure, not a free pass; absent
   `primary` defaults False, never True. Closes R1's mint path outright.
   The complete fix is HMAC-signing fetch records at `save()` and verifying
   at replay — same treatment memory_epistemics and the cutoff enforcer
   already got; sha-in-same-file detects rot, not writers.
2. **Scope the seal guard to THIS run**: add `list_run(rk)` and pass that
   from `engine.py:611`; name offending run/stage/key in refusal reasons.
   Fixes R2 and the diagnosability gap together.
3. **Terminal-state tombstones + disclosure**: when a run ends sealed or
   refused, mark it consumed; refuse cached-stage service to later attempts;
   surface attempt count, resumed stages, and oldest evidence time in
   `notes` AND `summary_dict()`. Fixes R3.
4. **Make session reconstruction payload-only**: rebuild `session.evidence`
   solely from payloads on BOTH paths (or skip the restore loop when execute
   ran), so all three execution modes seal identical sessions. Fixes R4a.
5. Restore the original `timestamp` in rebuilt Evidence — it is already in
   the payload. One line. Fixes R4b.
6. Wire `is_claim_open` (agp.claims liveness) in `run_retro_batch.py`, or
   strike contract 5 from the docstring. Fixes R5 either way.
7. Parse `produced_at` defensively in gc/age_seconds; unparseable ⇒ warn and
   treat as old, never raise. Fixes R6.
