# RED TEAM FINDINGS — checkpointing and resume (W3 module)

**Claim under attack:** the checkpoint module's own docstring — "Resumption
must never become a way to launder evidence whose provenance was lost; when we
cannot guarantee provenance, we refuse to seal" and "integrity is checked,
not assumed."

**Surface:** `tools/pipeline/checkpoint.py` + its engine integration
(`engine.py:422-616`). Chosen because it is the only listed surface with NO
prior red-team pass: `findings/redteam_confidence.md` covered confidence
inflation, `test_redteam_prov_memory_wiki.py` memory/wiki,
`test_redteam_retry_after_dos.py` transport, `test_redteam_w5_cutoff_forgery.py`
the retrodiction cutoff. Checkpointing is exactly where two already-confirmed
bug classes meet — provenance laundering (F4 of the confidence pass) and
resume-vs-live divergence (the historical 0.54→0.80 checkpoint bug) — and its
tests (`test_build_w3_checkpoint.py`) are happy-path plus property tests that
use well-formed fixtures written by the same code under test.

**Method:** DIFFERENTIAL + adversarial input. Differential between the
docstring contract and actual behaviour; adversarial inputs aimed at the
checkpoint JSON itself, which is plain unauthenticated file storage that any
co-located process (or another agent instance on this shared machine) can
write.

Run: `python3 -m pytest tests/test_redteam_checkpoint_resume.py -q`
Result at time of writing: **6 failed, 2 passed** (failures ARE the findings;
the 2 passes are honest-negative regression pins).

---

## CONFIRMED BREAKS

### C1 — Missing/empty `content_sha256` bypasses the entire integrity check (CRITICAL)
`checkpoint.replay_ledger` guards with `if digest and _sha(body) != digest`.
A falsy digest skips verification AND still gets replayed via
`ledger.record_tool_result(..., primary=True)`. One missing JSON field in a
checkpoint file mints PRIMARY provenance for arbitrary fabricated bytes, and
`seal_guard` returns SEAL. Worse, with empty-string digests the dedup key is
`""`, so a second distinct record is silently dropped as a "duplicate".
Fix: absence or emptiness of `content_sha256` must be an integrity failure,
not an unconditional pass.

### C2 — `seal_guard(trace, cp.list_all(), ledger)` consults and mutates across runs (CRITICAL)
`engine.py:611` passes **every checkpoint in the store**, not this run's.
Verified differential: run A ("semiconductor supply chains") fetches bytes;
run B ("unemployment rate", fresh, never crashed) calls seal_guard; run A's
bytes are replayed INTO run B's ledger as PRIMARY observations, then B seals.
Any later INFERRED claim in run B echoing those bytes re-classes upward off
evidence its own run never collected. Also: the guard mutates the ledger as a
side effect of *checking* it — including on the fresh branch, which returns
SEAL over bytes it just laundered in itself. Fix: filter checkpoints to
`trace.run`, and make the guard verify against a scratch ledger.

### C3 — No-fetch checkpoints make the guard vacuous (HIGH)
`provenance_is_intact` iterates `payload["fetches"]`; checkpoints without
fetch records (decompose, answer_leaf, or any payload written by an older/
different schema version) trivially read as "intact". "Nothing to verify"
collapses to "verified". A resumed run whose fetch payloads were restructured
seals with zero verified provenance.

### C4 — `produced_at` is attacker-writable; evidence age is cosmetic (HIGH)
The docstring promises resumed runs stay "honest about evidence age".
`produced_at` is an unauthenticated JSON field. Rewriting it to now() makes
40-day-old checkpointed evidence report age ≈0 — and by the same mechanism
keeps it permanently immune to `gc()` (age resets on every touch). The store
needs at minimum an HMAC over the record (the seal key already exists), or
age must be derived from file mtime cross-checked against content.

### C5 — fetch_leaf cache key binds only `question_id` (MEDIUM-HIGH)
`engine.py:487` caches on inputs `{"qid": q.question_id}` — not leaf text,
date, or registry state. Question ids are uuid-derived per decomposition, so
a resumed run after a regenerated decomposition normally misses — but any
path that reuses ids (replay from stored programs, tests, future id
stabilisation) serves fetches collected for a *different question text* and
the run proceeds to seal them. The cache claims to be content-addressed; the
key does not include the content.

### C6 — GC claim protection is declaration-based (INFERRED, not yet exploited end-to-end)
`gc()` spares only checkpoints that DECLARE open `claim_ids`. The engine
declares them on fetch_leaf only; decompose and answer_leaf carry none. An
open claim therefore does not protect the decomposition or answer payloads
that the evidence depends on — gc deletes half a run's state while keeping
the other half, producing exactly the kind of Frankenstein-resume C5 feeds.
(Not separately demonstrated in the test file; flagged for the owner.)

## HONEST NEGATIVES — attacks that did NOT land (kept as pins)

- Tampered body WITH a recorded good digest: correctly refused
  (`integrity_failures`, REFUSE).
- Double replay into the same ledger: dedup works; no double-record.

## WHAT TO FIX (ordered)

1. Treat missing/empty digest as integrity failure (C1) — one line, closes
   the loudest hole.
2. Scope seal_guard to `trace.run`'s checkpoints and verify on a scratch
   ledger so checking has no side effects (C2).
3. HMAC-sign checkpoint records with the existing seal key; verify on load;
   refuse unsigned records produced after the key exists (C4, and hardens C1).
4. Include leaf text + today + registry version in the fetch_leaf stage
   inputs (C5).
5. Thread claim_ids through every stage save (C6).
