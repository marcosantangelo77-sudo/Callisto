# Declared stance: the sign of a forecast is declared, never inferred from prose

Defect pinned in 5e88b05; fix in fa2bea9 (branch `redteam/rotating-0823-162344`).

## The defect

`tools/pipeline/retro.py::PipelineResearcher._leans_yes` decided the SIGN of
every retrodiction forecast by scanning the conclusion for six hardcoded
English phrases ("no evidence", "does not", "not supported", "unlikely",
"falsified", "refused") and DEFAULTING TO YES when none fired. `answer_async`
then computed `prob = 0.5 +/- confidence/2` in that direction. Magnitudes were
fine — every sign was a coin flip weighted by incidental wording.

## Before / after on real examples

### Example 1 — affirmative finding with a negating aside

Conclusion: *"The merger completed on schedule in Q3, confirmed by the 8-K
filing. There is no evidence of regulatory objection."*

- Before: scan fires on "no evidence" -> leans NO. `p = 0.5 - conf/2`
  (0.20 at conf 0.60). An affirming run scored against the claim.
- After: model declares `"stance": "AFFIRMS"` in the structured synthesis
  output -> `p = 0.5 + conf/2` (0.80 at conf 0.60).

### Example 2 — clearly negative, dodges all six phrases

Conclusion: *"The trial missed its primary endpoint; the drug failed to
separate from placebo on every measured axis."*

- Before: no phrase fires -> default YES. `p = 0.5 + conf/2` (0.80).
  A refuted claim scored confidently FOR it.
- After: model declares `"stance": "DENIES"` -> `p = 0.5 - conf/2` (0.20).

### Example 3 — evidence genuinely does not settle it

Conclusion: *"Sources conflict and none is primary; the question is not
settled by the evidence gathered."*

- Before: inexpressible. The default-yes scan had to take a side.
- After: `"UNDETERMINED"` maps to exactly p = 0.5. A scorer that cannot tell
  says so instead of guessing.

### Example 4 — unparseable / absent stance

Any stance value other than AFFIRMS/DENIES (including missing) is coerced to
UNDETERMINED -> p = 0.5, never a confident lean.

## What changed

1. `tools/pipeline/engine.py`: `LeafOutcome.stance` and `PipelineResult.stance`
   added (default UNDETERMINED). Populated in `_answer_leaf` from the model's
   structured JSON (`proposal["stance"]`), not by parsing the answer string.
   Unknown/absent -> UNDETERMINED.
2. `tools/pipeline/model.py`: synthesis contract (`answer_messages`) now
   instructs the model to return `"stance"` as one of AFFIRMS / DENIES /
   UNDETERMINED with an explicit definition of each.
3. Parent inheritance: parent takes the stance of the leaf that set its
   confidence (engine.py, same path as parent confidence).
4. `tools/pipeline/retro.py`: `answer_async` reads `result.stance`;
   `_leans_yes` deleted entirely. UNDETERMINED -> p = 0.5.
5. `tools/calibration/instrument.py`: the mirrored keyword scan there is kept
   for ATTRIBUTION ONLY — it now reports what the old scorer would have said,
   so historical damage can be quantified. It no longer drives anything.

Grep sweep for other prose-inferred directions: only instrument.py retained a
copy (now attribution-only); no other module infers forecast sign from text.

Hard rules honoured: no confidence score raised anywhere; the 0.5 +/- conf/2
magnitude mapping untouched (sign-only change); no live execution path armed.

## Verification

- `tests/test_redteam_direction_from_prose.py`: xfail(strict) markers removed;
  all 6 tests pass on their own terms, including a pin that `_leans_yes` and
  any negation word-list must not reappear in retro.py.
- Full suite: `pytest tests/ -q --ignore=tests/test_ml_classifier.py
  --ignore=tests/test_ml_drift.py` -> **21 failed, 11124 passed** — identical
  to the stated baseline of 21 failures (all pre-existing, unrelated to this
  change; backtest e2e, lifecycle claim, prop scanner, confidence laundering).
