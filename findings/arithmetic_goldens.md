# Arithmetic-fix goldens: (a) vs (b) adjudication

Date: 2026-08-24. Branch: review/ox-alpha-0824b (+7, fix commit 4c09a7b).

## The question

`tests/test_speed_parallel_leaves.py` compares a LIVE PARALLEL run against
STORED SERIAL goldens. After the C5 arithmetic-contradiction fix, 11 goldens
fail. Two explanations:

- (a) LEGITIMATE: the fix changes leaf outcomes; goldens are stale; serial and
  parallel still agree with each other.
- (b) REAL BUG: `_sole_bare_boolean()` reads sandbox stdout captured through a
  lock-serialized compute path that may behave differently under concurrency —
  a nondeterministic fix.

## Method (no golden was regenerated before this)

1. Ran every speed-golden scenario TWICE under the fix: once forcing strictly
   sequential leaf execution inside the parallel engine (monkeypatched
   `asyncio.gather` → serial await loop in scripts/discriminate_goldens.py),
   once normally concurrent. Compared fingerprints to EACH OTHER.
2. Negative control: injected an artificial parallel-only behavior change into
   the reconciliation path (`_sole_bare_boolean` returns None only when running
   concurrent). The harness flagged 9 divergent fields. The discriminator has
   teeth.
3. Verified the exact motivating case (sandbox prints `4.1 < 3.5` -> False,
   prose asserts "WAS lower") refuses through the parallel harness:
   `reconciliation_failure="sandbox printed False (DENIES) but the answer
   asserted AFFIRMS"`, answer emptied, stance UNDETERMINED, run unsealed.

## Result

All 11 scenarios MATCH serial-vs-parallel byte-for-byte on every observable
field. **Verdict: (a).**

## Why the failures are NOT from the fix at all

Diffed the branch's stored goldens against origin/master's:

- Master's goldens contain an evidence-age line in `conclusion`
  ("[evidence age at seal: ...]", master commit 3cab908) that postdates this
  branch's fork point — absent here by branch age, not by the fix.
- This branch's goldens carry extra `notes` ("sources asked but NOT
  contributing evidence", redteam C4, commit 5fc06ff) not yet in master.
- A field-by-field scan of all 11 goldens vs master found ZERO differences in
  any score, tier, stance, seal status, or per-leaf observable. The C4 note is
  additive information-only (its own commit message says so); it scores
  nothing.

The 11 failures are stale-golden drift from merged-but-unrebased work on both
sides, exactly the situation of precedent 2512b6f (goldens regenerated after
the cmefedfut adapter shifted retrieval order; answers unchanged).

## What regeneration must show (and did)

Regenerated goldens via scripts/gen_speed_golden.py against the fixed branch;
diff vs old goldens shows ONLY the two drift classes above (notes text,
evidence-age line). No confidence raised anywhere; no outcome flipped by the
fix in these fixtures because no scripted fixture asserts the negation of its
own computation — the refusal contract itself is pinned separately in
tests/test_redteam_answer_correctness.py::TestComputeReconciliation.
