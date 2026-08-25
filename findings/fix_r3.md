# fix_r3 — a VERIFIED leaf that determines nothing must not seal

Task 212 / defect R3, branch `fix/r3-verified-empty-leaf`, commit `2c29c29`.

## The regression

The battery re-run flipped unknowable_02 and treasury_04 from CORRECT
REFUSAL to SEALED. The task-194 seal gate keyed on GAP-CLASSIFICATION
only:

```python
provable = [l for l in result.leaves if l.answer and not l.gap_kind]
```

`gap_kind` is set only when fetches are absent (`classify_null_kind`) or
when declared evidence requirements go unmet (`unprovable`). A leaf with
excellent provenance — real bytes from real sources, admitted by the
gate, requirements met — carries no gap_kind and an answer string. It is
"provable" under that predicate even when the evidence determines
nothing about the question asked (intraday asked, only daily published;
wrong period; wrong measure). Tier measures where bytes came from; it
says nothing about whether they answer anything.

## The fix

1. **Structured signal, declared not parsed.** `LeafOutcome`
   gains `answers_question: bool = True`. The answering model returns it
   in its JSON payload alongside `stance`; the engine reads it as-is
   (`tools/pipeline/engine.py`, `_answer_leaf`). The conclusion prose is
   never scanned for "cannot be determined" — parsing prose for meaning
   is the forecast-sign defect class, and task 194 was forbidden from it
   too. The prompt (`ANSWER_SYSTEM` in tools/pipeline/model.py) tells the
   model to declare false whenever the evidence settles nothing about the
   question asked, explicitly warning that good provenance is not an
   answer. The signal is orthogonal to both existing axes: provenance
   tier (where bytes came from), gap_kind (why usable evidence is absent).

2. **One predicate, not two.** The task-194 rule was extended in place:
   provable = answered AND gap-free AND declares answering. No second
   gate exists to drift. The refusal breakdown names WHY each leaf
   failed — `non-answering` vs the gap kinds — so the caller learns which
   kind of nothing it got.

3. **Mixed parents.** A parent with some answering leaves stands only on
   those; a high-tier non-answering leaf can no longer win best-leaf
   selection and hand its magnitude or stance to the parent. Existing
   SPECULATIVE cap on partial proof unchanged.

4. **Refuse-or-lower only.** Absent field defaults True (legacy models,
   old checkpoints replayed via `_leaf_from_payload` are unaffected);
   nothing raises any confidence number.

## Tests

`tests/test_fix_r3_nonanswering_leaf.py` (5 tests, all green):
regression shape (VERIFIED + answers_question=false refuses, naming
non-answering), mixed-parent direction/magnitude isolation, VERIFIED +
answers_question=true still seals normally, legacy-absent-field still
seals, single-predicate refusal naming. `tests/test_seal_unprovable.py`
still passes untouched — one rule.

Full suite vs baseline `a6e4467`: fail set byte-identical (53
pre-existing failures, dominated by strict-xfail redteam canaries,
mutation-survivors exact-scoring pins, and retrieval-relevance R2–R4
canaries; plus 2 collection errors from missing libomp/xgboost). Zero
introduced.

## Root cause shared with the stance-from-confidence bug?

Yes, and it is worth fixing once. The C1 family (parent takes stance
from the highest-CONFIDENCE leaf) and R3 are the same mistake at two
seams: a leaf's STRUCTURAL quality score (confidence/tier/provenance)
was read as INFORMATIONAL content (does this bear on and settle the
question). Confidence is an entitlement number; nothing in it encodes
relevance-to-the-question. The general shape of the once-fix: every
place that selects or aggregates leaves by structural score must first
filter through the leaf's declared answer-bearing signal — i.e.
`answers_question` should become a precondition for ANY leaf to
contribute stance, tier story, source class, or corroboration weight to
a parent, not just to sealability. That is a broader refactor than this
task allowed (it touches synthesis/corroboration weighting too), but the
signal now exists to hang it on. Until then, the two remaining C1
strict-xfail canaries in tests/test_redteam_answer_correctness.py are
the standing record of the unfixed half.

## Files

- tools/pipeline/engine.py — LeafOutcome.answers_question; declared-signal
  read in _answer_leaf; unified provable predicate at the seal gate.
- tools/pipeline/model.py — ANSWER_SYSTEM prompt asks for the declaration.
- tests/test_fix_r3_nonanswering_leaf.py — new contract tests.
