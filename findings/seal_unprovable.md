# D2 — Seal contract honesty: all-unprovable parents must refuse

Battery context: `loop/findings/battery` — accuracy 0/33, and the two
"wrong-answers-sealed" cases (`unknowable_04`, private utility balance;
`unknowable_05`, Wikipedia request count) both sealed at 0.34/SPECULATIVE
on questions with NO knowable answer. The conclusion prose said "cannot
be determined", but nothing consumes prose: to any consumer a sealed
non-answer is indistinguishable from a sealed answer. PATTERNS.md family
#1 — a check producing a verdict nothing reads.

## Root cause

`tools/pipeline/engine.py` (run, stage 6) picked the parent magnitude
from `max(answered, key=confidence)` where `answered` meant only
"the Manager wrote words into the answer field". A leaf carrying a
structured gap verdict — unprovable / honest_null / retrieval_failure,
already set during answering by gaps.py's single membership rule —
proved nothing, but its words still drove a parent confidence and a
seal.

## Fix (commit bcf7439, branch fix/seal-unprovable)

The parent now stands ONLY on **provable** leaves: answered AND
gap-free. Reads the STRUCTURED `LeafOutcome.gap_kind`; never the
conclusion text (parsing prose for meaning is the forecast-sign defect
class).

1. **All leaves gapped → REFUSE**, refusal reason names which kind of
   nothing: e.g. `no provable leaf: every leaf is gap-classified
   (unprovable x4)`. The caller learns WHICH nothing it got. The empty-
   answer early exit ("every leaf came back unanswered") also names the
   kind breakdown instead of bare prose.
2. **Mixed → SEAL deliberately, lowered**: standing only on provable
   leaves (magnitude AND stance from the best provable leaf), ceiling
   capped at SPECULATIVE (SELF_REVIEW_CEILING = 0.54), applied after the
   inheritance clamp so it can only subtract; a note records
   "sealed on N of M leaves".

### Why mixed = seal-with-lowered-ceiling, not refuse

A parent standing on one proven leaf out of five is a real claim —
that leaf met its evidence bar — just a weaker one than standing on
five. Refusing would discard genuine proof and push the system toward
refuse-everything; sealing at full strength would let one thin leg
carry a confident parent. The SPECULATIVE cap states the weakness
numerically while preserving the proven content. Both branches obey
"no confidence may be raised": the fix only refuses or lowers.

## Tests

`tests/test_seal_unprovable.py`:
- all-unprovable → refuses, reason names `unprovable`
- all-retrieval-failure → refuses, reason names `retrieval_failure`
- exact battery shape (every fetch irrelevant junk, model answers over
  gaps) → does NOT seal (unknowable_04/05 regression)
- mixed → seals ≤0.54/SPECULATIVE standing on the proven leaf only
- genuinely answered → seals normally (no refuse-everything)

Updated `tests/test_integration_seam_engine.py` F4 expectations: the
all-unprovable parent now refuses (was asserting `result.sealed`).

## Result

Baseline 25 failures → suite run recorded in session log; new tests 5/5.
unknowable_04 and unknowable_05 shapes can no longer enter the record as
sealed answers.
