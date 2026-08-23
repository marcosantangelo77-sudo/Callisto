# MEMORY & WIKI LAYER — improvement pass (improve/rotating-0823-082556)

**Area chosen: the memory and wiki layer** — tools/hermes_memory.py,
tools/memory_epistemics.py, and the write→read seam between them.

Why this one: CLI (twice), retrodiction/calibration, artifacts/sandbox,
retrieval, synthesis, routing, schema seam and edge quantification are all
taken. The memory layer is the one surface every loop iteration passes
through — it decides what the model sees — and its epistemics code is the
newest in the repo (P4 + R5 fixes), i.e. the most likely place for a
half-closed seam.

## The defect, measured

The P4 wave added provenance classes to the memory layer, but only to HALF of
it. Verified end-to-end before any change:

1. **Write side admits a class; read side never hears about it.**
   `record_learning()` runs `admit_learning()` and correctly clamps an
   unverified claim to 0.55. But the INSERT wrote five columns — no
   `source_class`. `_build_learnings()` then SELECTed without the column and
   passed no class to `annotate_for_reinjection()`, which maps missing →
   INFERRED for EVERY row.

   Consequence: a learning sealed with a verified PRIMARY seal was stored at
   confidence 1.0 and re-read as "provenance INFERRED (ceiling 55%)". The
   honest-looking annotation on every prompt line was false for exactly the
   rows that had earned better. Two failure modes:
   - *Under-trust*: verified evidence permanently reads as a guess.
   - *Latent over-trust*: because confidence 1.0 sat in the row while the
     label said INFERRED, any future consumer reading the number without the
     label (get_actionable_learnings min_confidence=0.5 ranking) saw an
     unearned 1.0.

2. **The columns migration 015 adds were never created on fresh DBs by the
   runtime DDL path.** `_ensure_tables()` CREATE TABLE predated the
   migration; a new database got the old five-column shape and both ends of
   the seam were broken from birth.

Reproduced with a two-line script against the real modules: admission says
`PRIMARY / ceiling 1.0`, reinjection says `INFERRED / ceiling 0.55`.

## What changed

- `_ensure_tables()` now creates `source_class TEXT` and
  `provenance_seal TEXT`, guarded ALTERs for tables that predate it (both
  regimes converge on the same shape).
- `record_learning()` persists the **admitted** class plus the seal blob
  (`{"seal_session":…, "seal_hash":…}` JSON) on both the WriteCoordinator
  and direct paths. Re-verification stays write-time-only: verifying a
  forgeable digest at read time would be security theatre.
- `_build_learnings()` SELECTs `source_class` and hands the STORED ADMITTED
  class to `annotate_for_reinjection()`. NULL (legacy rows) still normalises
  to INFERRED — fail closed for data written before provenance existed.

After: sealed PRIMARY → `provenance PRIMARY (ceiling 100%)`; unsealed claim →
stored 0.55 AND read as INFERRED; legacy row → INFERRED, no crash.

## Tests

4 new tests in `tests/test_build_p4_memory_layer.py`
(TestProvenanceSeamCarries): sealed-class roundtrip, unsealed→INFERRED with
ceiling travelling, legacy-shape DB renders fail-closed, `_ensure_tables`
upgrade path. Suite: 28 passed in that file; full tree run recorded below.

## What I deliberately did NOT do

- No embeddings/semantic retrieval for learnings: the pool is capped at 60
  rows scanned per context build today; semantic search is machinery nobody
  needs yet.
- Did not touch knowledge_wiki.py: its admission gate already keys off the
  same ceilings and was fixed separately (commit c2e189d).
- Did not auto-run migration 015 anywhere — operator sign-off rule stands;
  the runtime guard makes the migration optional for fresh DBs but the
  workstation DB should still be migrated for the clamp/decay backfill.

## Verdict

The rest of the layer is sound: decay-and-replace semantics are tested with
random inputs, trimming is genuinely disconfirming-biased, and the R5
fail-closed seal logic survives re-reading. One seam was open; it is now
pinned shut by tests.
