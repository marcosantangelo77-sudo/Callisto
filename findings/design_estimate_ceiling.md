# DESIGN — SEPARATE THE ESTIMATE FROM THE CEILING

**Date:** 2026-08-23 · **Branch:** `build/dd-decomposition-diversity`
**Prototype:** `agp/estimate.py` · **Tests:** `tests/test_design_ec_*.py` (25 passing)
**Rescore artifact:** `data/retro_batch/rescore_estimate_vs_ceiling.json`

---

## 0. VERDICT FIRST

The diagnosis is **half right, and the half that is right does not lead where
the morning report hoped.**

1. The collapse is real and traceable in code: a single number carries both
   the belief and the entitlement, and every mechanism acts on both at once
   (§1). Separating them is architecturally correct and safe (§2, §5).
2. But separation **does not improve calibration on this batch — it makes it
   worse, provably, for every admissible reconstruction of the discarded
   estimates (§6).** All five outcomes were True while the system said 0.33.
   The model was underconfident *and wrong*. A number shaded up from 0.33 to
   0.9 moves further from an outcome set that is only 60% True-weighted
   against confident misses.
3. Therefore the binding constraint is **not the ceiling stack — it is
   answer quality**, exactly as the second live run already showed: nine
   fetches from OpenAlex cannot contain sell-side consensus estimates, and
   the adversary said so unprompted (`results_smoke5.jsonl`, question
   `c1672ca2fb5444c9`: "any affirmative lean is fabricated from empty
   retrievals"). The 27-point gap has TWO components: a structural downward
   shading (real, ~mechanism-attributed) AND a directional error rate the
   shading was partly masking. Fixing the first without the second makes the
   Brier score worse, because hedged confidence on wrong answers is cheaper
   than confident confidence on wrong answers.

**Do not refactor the pipeline around this split yet.** Ship the type, keep
the invariant, instrument the raw estimate (already built:
`tools/calibration/instrument.py`), and re-measure on a batch where retrieval
is fixed enough that the answers carry information. n=5 with all-True
outcomes cannot distinguish "underconfidence bias" from "answers are noise"
— and the rescore shows the two hypotheses diverge sharply.

---

## 1. IS THE DIAGNOSIS RIGHT? — the merge point, traced

The single number is born at clamp time and never splits again.

**Leaf level — `tools/pipeline/engine.py:440-466` (`_answer_leaf`):**

```
proposed = float(proposal.get("proposed_confidence"))          # :441  THE ESTIMATE
clamped  = min(proposed, MAX_CONFIDENCE_BY_SOURCE[best])       # :443-445  ← merge #1
if reasons: clamped = min(clamped, 0.54)                       # :463-464  requirement cap
out.confidence = round(max(0.0, clamped), 2)                   # :466      estimate destroyed
```

After line 466 there is no field that remembers what the model believed. The
attribution run confirms this is many-to-one: any raw estimate in
[0.55, 1.0] produces the identical trace (`diagnosis_underconfidence.json`,
`raw_estimate_caveat`).

**Parent level — `engine.py:656-681` (`run` step 6-7):**

```
proposed = best_leaf.confidence                     # :662  already-collapsed child
clamp_parent_confidence(proposed, descendants)      # :665  inheritance rule, min()
Adversary.apply_verdict(clamped, objections)        # :681  subtract-only, agp/adversary.py:479-496
```

**Binary mapping — `tools/pipeline/retro.py:97-99` (the scored number):**

```
prob = (0.5 + conf/2) if leans_yes else (0.5 - conf/2)
```

So the calibration measurement scores `min(everything)` mapped onto the
binary. Every mechanism — provenance ceiling (`agp/thresholds.py:27-32`),
evidence relabeling (`agp/provenance.py:139-175`), requirement gate,
inheritance clamp (`agp/research_program.py`), ensemble spread
(`agp/ensemble.py:103-125`), self-review cap, downward quantisation
(`agp/thresholds.py:floor_conf`) — operates by `min()` or subtraction on this
one survivor. Nothing ever centres it. That is the diagnosis, confirmed at
line level.

**Where the diagnosis is INCOMPLETE:** it treats the 27-point gap as if it
were one quantity. It is bias + error, and §6 shows error dominates.

## 2. WHAT BREAKS IF THEY ARE SEPARATED?

Nothing structural — provided the split is expressed as I did in
`agp/estimate.py`:

| Concern | Today | Under the split |
|---|---|---|
| Seal covers | conclusion + confidence | conclusion + `sealable() = min(estimate, ceiling)` — unchanged; the estimate rides beside it as recorded metadata |
| DB stores | `confidence_score` | adds `raw_estimate` column/JSON field; `sealable()` fills `confidence_score` so no consumer breaks |
| Consumer acts on | confidence | position sizing, promotion gates, escalation triggers all keep reading `sealable()` |
| Calibration reads | collapsed number | reads `raw_estimate` (the counterfactual column) |

The failure mode to avoid is consumers quietly migrating to the estimate
because it is "more accurate." The type prevents this by making the
authoritative action number an explicit method (`sealable()`), not a field.

## 3. WHICH NUMBER IS CALIBRATION SCORED AGAINST?

**The estimate — but only once answers carry information.** Argument:

- Scoring the ceiling measures our **caution**: did we claim no more than we
  were entitled to? That is a property of the protocol, not of the beliefs,
  and it is largely verifiable by inspection (the ceilings are declared).
- Scoring the estimate measures our **accuracy**: is P(outcome) right? This
  is the thing retrodiction exists to measure, and it is the number that can
  be *learned from* — routing, model selection, reference-class priors all
  need a gradient, and `min(estimate, ceiling)` has zero gradient whenever
  the ceiling binds (which the attribution shows is always).
- They are different tests and both should exist: report `sealable()`
  calibration as the entitlement audit and `estimate` calibration as the
  accuracy signal. The morning report's number (Brier 0.3129) is currently
  neither — it is the accuracy test computed on a caution-clipped number.

Caveat that decides the sequencing: scoring an estimate is only meaningful
when the estimate varies across questions. On smoke5 it could not even be
recorded. Instrumentation precedes separation in practice.

## 4. WHICH NUMBER SIZES A POSITION?

**The sealable number (`min(estimate, ceiling)`), explicitly and without
apology.** In `tools/edge.py:183-222` the calibrated probability feeds edge
and Kelly. Sizing on the raw estimate:

- sizes positions on a belief the provenance layer has explicitly ruled
  insufficiently evidenced to claim;
- converts the architecture's central safety property into money before the
  property has been validated on live outcomes;
- would have made smoke5's negative realised edge WORSE in expectation on
  any batch where the estimate is confidently wrong (see §6).

Sizing on the ceiling alone ("conservative") is systematically biased toward
0.5-mapped bets and would take almost no position above the SPECULATIVE
band — safe but dead. `min(estimate, ceiling)` is not a compromise between
them; it is the correct semantics: bet your entitled belief, record your
full belief for science. When retrieval improves and ceilings rise on real
evidence, positions grow automatically and legitimately.

## 5. DOES THE ANTI-INFLATION GUARANTEE SURVIVE? — yes, structurally

The guarantee survives because the split changes *what is remembered*, not
*what is permitted*. Concretely:

1. **Ceilings are monotone non-increasing by construction.**
   `EstimateCeiling.with_ceiling` raises `ValueError` on any increase;
   `apply_adversary_penalty` rejects negative penalties (no bonus path);
   the dataclass is frozen. There is no API by which an automated actor
   raises a ceiling.
2. **Reported = min(estimate, ceiling), always.** Even the one upward-
   capable path (`with_estimate`, which requires an explicit caller and
   stands for new evidence or a new model call) leaves the reported number
   clamped by the untouched ceiling.
3. **Quantisation stays floor-only** (`agp/estimate.py::floor_conf` mirrors
   `agp.thresholds.floor_conf`).
4. **Property-tested, per the R3 lesson** (`MORNING_REPORT.md` §Ugly-2):
   `tests/test_design_ec_antiinflation.py` drives ~5000 random trajectories
   of mechanism sequences per seed × 3 seeds, asserting after every step
   `sealable(t+1) <= sealable(t)` and `sealable <= entry_ceiling`, plus
   random upward probes that must raise. 25/25 tests pass.

The one honest caveat: `with_estimate` means a *model* proposing a higher
estimate now persists visibly instead of being silently clipped. That is
disclosure, not inflation — nothing sealed, stored, or acted upon rises.

## 6. PROTOTYPE RESCORE OF SMOKE5 — the negative result

Method (`tools/calibration/rescore_smoke5.py`): reconstruct each scored
row's (ceiling, leans_yes) from its recorded prediction, sweep the raw
estimate over the admissible range [0.55, 1.0] established by the mechanism
attribution, and compare Brier under collapse vs separation using
`agp.estimate.rescore`.

| assumed raw estimate | collapsed Brier | separated Brier | Δ |
|---|---|---|---|
| 0.55 | 0.3129 | 0.3806 | −0.068 |
| 0.80 | 0.3129 | 0.4900 | −0.177 |
| 1.00 | 0.3129 | 0.6000 | −0.287 |

**Separation worsens Brier for EVERY admissible reconstruction.** Why:
four of five outcomes were True, but the separated predictions move toward
0.75–1.0 while one outcome was False — and the collapsed 0.33 was already
closer to the misses than the confident estimate would have been. The
collapsed column reproduces the reported mean Brier 0.3129 and the exact
+0.27 underconfidence bias (asserted in `tests/test_design_ec_rescore.py`),
so the machinery is verified; the direction of the result is the finding.

What separation WOULD fix, and what it would not:

- It fixes the **measurement** (zero gradient under the ceiling → no
  learning signal) and the **bias accounting** (+0.27 becomes visible as
  structure rather than fate).
- It does not fix **answers built on retrieval that cannot contain the
  deciding fact**. The adversary's own objection on `c1672ca2fb5444c9`
  ("OpenAlex ... structurally cannot contain the deciding fact") predicts
  this result: when evidence is empty, a high estimate is fabrication with
  better arithmetic.

### Recommended sequence

1. Land the type + tests (done, this branch).
2. Wire `tools/calibration.instrument.wrap_model` into the next batch so raw
   estimates survive to the results file — cheap, no pipeline change.
3. Fix source diversity / query authoring until sealed runs show evidence
   heterogeneity (this is already NEXT.md's bottleneck).
4. Only then re-run this rescore on real estimates with real n (~100). If
   the estimate column beats the collapsed column there, adopt the split in
   `engine.py`; if not, the ceiling stack is doing its job and the gap was
   never its fault.

---

## Appendix: files

- `agp/estimate.py` — prototype type + rescore
- `tests/test_design_ec_prototype.py` — unit semantics
- `tests/test_design_ec_antiinflation.py` — property-based guarantee
- `tests/test_design_ec_rescore.py` — batch reproduction + honest negative
- `tools/calibration/rescore_smoke5.py` — rescore driver
- `data/retro_batch/rescore_estimate_vs_ceiling.json` — artifact
