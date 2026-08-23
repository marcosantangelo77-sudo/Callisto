# RED TEAM — SYNTHESIS AND CORROBORATION

**Surface choice:** no prior pass attacked `tools/pipeline/synthesis.py` or
the combiner path in `engine.py`/`retro.py` — the stage where evidence items
become one score. Covered so far: confidence/calibration, loop incentives,
provenance/checkpointing, retrieval/independence/selection, sealing/prereg.
Synthesis is also where the morning report's measured underconfidence points:
if a *raise* path exists here it is the ninth gate-weakening mechanism — and
one exists (S1), though it is a mislabelled raise rather than an arithmetic
one.

Tests: `tests/test_redteam_synth_corroboration.py` (repo canary convention:
confirmed defects FAIL against current code; honest negatives PASS).

Run: `python3 -m pytest tests/test_redteam_synth_corroboration.py -q`
Current: 16 failed / 6 passed — every failure below reproduces.

---

## CONFIRMED DEFECTS

### S1 — Unanimous REFUTATION scores VERIFIED 1.0 (CRITICAL)
`synthesis.confidence_from_agreement` counts independent sources AGREEING and
never asks what they agree ON. Three independent PRIMARY sources each with
`stance="refutes"` — every voice says the claim is FALSE — produce score 1.0,
identical to three supporters. The most damning structure in the system
("every independent source on this refutes it") is its highest-scoring one.
`synthesize()` propagates it: report confidence 1.0 for an all-refuted group.
This is a confidence RAISE from mislabelling, not arithmetic: the direction
guard cannot see it because no number crosses a ceiling.
**Fix direction:** stance must gate the score before corroboration credit —
a group whose voices refute scores like a null or a refutation, never like
agreement; mixed stances are already contradictions (see S4b).

### S2 — The engine's CONTRADICTION_CHECK step is dead code (CRITICAL)
`engine.run` advances through `SessionStep.CONTRADICTION_CHECK`, but nothing
on the pipeline path ever calls `AGPSession.add_contradiction` — the only
production caller is `orchestrator.py:1103`. `detect_contradictions` /
`synthesize()` results are never read by `engine._answer_leaf` or by parent
assembly (`best_leaf = max(answered, key=confidence)`). A pipeline run whose
leaves scream numeric contradictions seals at the best leaf's score.
`symbols`: engine.py has zero references to SynthesisReport/synthesize.
The synthesis module's contradiction cap protects only callers that use
synthesize() — which today is nobody in production.

### S3 — round() still raises across tier boundaries at engine.py:466 (HIGH)
The exact historical bug class fixed by `floor_conf` — and duplicated-logic
again: the fix landed in thresholds/adversary/research_program but NOT in
`out.confidence = round(max(0.0, clamped), 2)`. Parametrised proof:
0.5455 → 0.55 mints PROBABLE; 0.749999 → 0.75 mints CORROBORATED. Random
sweep over [0.30, 1.0] finds thousands of upward rounds. Sibling call sites
at engine.py:395/429 are min-capped at 0.45 and safe.

### S4 — Contradiction detection false negatives (MEDIUM)
a) **First-stated-wins per independence unit** (`by_ikey.setdefault`): a
   publisher stating two wildly different numbers contributes only its FIRST
   value as a voice. Paired with a counterpart matching that first value,
   the pair reads as unanimous agreement (0.85) while the publisher's own
   text contains the dispute. Internal disagreement is dropped silently.
b) **One two-faced source disables stance detection entirely**: the guard
   `not (set(sup) & set(ref))` means a single independence unit holding BOTH
   stances suppresses stance-contradiction detection even with five clean
   supporters vs five clean refuters in the same group.
c) **Spelled-out magnitudes are invisible**: "three million tonnes" vs
   "3 million tonnes" extracts no values; a 3x numeric dispute between two
   PRIMARY sources detects as nothing and keeps full corroboration credit.
   (Extractor blindness is arguably by design; the group scoring is not.)

### S5 — Vacuous claims form a full-credit corroborating group (HIGH)
`claim_key("") == ()`. Every item with an empty/whitespace/punctuation-only
claim collapses into ONE group; three junk items are three "independent
voices" → confidence 1.0. Claim text comes from the model/extractor; nothing
rejects claims with no content words. Junk is not corroboration.

### S6 — Retrodiction binary sign decided by substring horoscope (HIGH)
`retro.PipelineResearcher._leans_yes` scans the WHOLE conclusion — which
embeds every leaf's QUESTION TEXT — for six phrases ("no evidence", "does
not", ...). Consequences demonstrated: a leaf question containing a listed
phrase flips the prediction sign regardless of the answers; any negative
answer phrased outside the list ("we found no support whatsoever", "the
evidence argues against") reads YES and pushes P(True) UP by conf/2. Every
calibration number downstream of this (Brier, the measured underconfidence)
is contaminated by a six-word sentinel scan. This connects directly to the
morning-report calibration anomaly: part of the "underconfidence" may be
mis-signed predictions, not shaded probabilities.

### S7 — classify_null trusts a rejected list without rounds (MEDIUM)
A trace with `rounds=[]` but a non-empty `rejected` list classifies as
NULL_LITERATURE with explanation "sources were queried..." though nothing
was attempted. Reachable via a crashed/misbuilt trace or a resumed payload;
exactly the literature-null vs retrieval-failure conflation the module
exists to prevent. Fix: require non-empty `rounds` (or an explicit attempt
record) before the literature-null branch.

---

## HONEST NEGATIVES — attacks that did NOT land (tests pass, pinned)

- **Contradictions never raise a score.** 5,000-input random sweep: with
  contradictions detected, score ≤ min(uncapped, 0.54); without, unchanged.
  The min/cap arithmetic itself held.
- **Volume is not corroboration** (within synthesis): 25 copies from one host
  score exactly like one voice; `independent_sources` stays 1.
- **Single-class groups never exceed their own ceiling**: frac*ceiling with
  final min(score, ceiling) holds across all four classes × random sizes.
  The escape is best_class laundering across classes (already pinned as F4c
  in redteam_confidence.md), not the formula.
- **max(values, key=abs) does not hide cross-source disputes when both items
  carry single values** — my initial hypothesis (noise number masking a real
  disagreement) produced false contradictions in the safe direction, and the
  sweep found zero misses of that shape; the real masking is S4a's
  first-stated-wins within one unit.

## INCENTIVE NOTE

S1 + S2 compose into the system's next self-deception mechanism if left:
the pipeline is rewarded for sealing (sealed_rate is a reported metric), and
contradictions are the main thing that stops sealing — so a path that never
populates them (S2) plus a scorer that rewards unanimity whatever it
unanimously says (S1) makes "seal at high confidence" reachable with
uniformly refuting evidence. That is the loop-failure shape again, one
level up.
