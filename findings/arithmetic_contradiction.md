# Arithmetic contradiction: computed False, sealed "lower" — FIXED

Worktree review-ox, branch review/ox-alpha-0824b. Baseline:
findings/redteam_answer_correctness.md §C5 (canary only, never fixed —
confirmed before starting: strict-xfail was failing on HEAD).

## 1. End-to-end trace of the compute path

1. REQUEST — `_answer_leaf` (tools/pipeline/engine.py ~441): the Manager
   model answers with JSON; a `{"compute": {"code": ...}}` key requests a
   sandbox run.
2. EXECUTION — `run_python(...)` under `self._compute_lock`; result object
   carries `.status`, `.stdout`, `.code`, produced files.
3. CAPTURE — status stored on `out.sandbox_status`; file bytes hashed into
   the artifact store; `record_tool_result("run_python", comp_body)`
   mints provenance; stdout appended as an Evidence item (`origin_agent=
   "sandbox"`), capped at INFERRED / ≤0.45.
4. RE-ANSWER — the model is simply asked AGAIN with the same evidence list
   (~line 487). Its returned `answer` and `stance` are accepted verbatim
   (lines ~489–495).
5. SEAL — parent stance = argmax-confidence answered leaf's stance;
   adversary, floor, seal_guard, verify_artifacts gate; seal.

THE SEAM: between steps 4 and 5. `sbx.stdout` was stored and shown to the
model, but NO code ever compared the computed verdict to the declared
stance. The computation was evidence-shaped, never constraint-shaped.
That is exactly PATTERNS.md #1 (check/result nothing consumes) wearing
#9's clothes: internally consistent pipeline, externally wrong answer.

## 2. Design choice: (a) refusal, not (b) or (c)

CHOSEN: (a) reconciliation check → REFUSE.

- (b) "computation supplies the stance directly" silently rewrites the
  model's answer. It hides the disagreement instead of surfacing it,
  destroys the calibration signal (we would never learn the model
  contradicts its own arithmetic), and generalizes badly: most compute
  outputs are numbers, not booleans, and mapping number→stance needs
  exactly the polarity understanding we cannot trust the machinery to
  have (that is task 180/C2 territory). It also violates the pass rule:
  auto-correcting stance upward IS raising a conclusion without new
  evidence.
- (c) blocking adversary objection routes a deterministic arithmetic fact
  through a probabilistic LLM critic that can miss it, overrule it, or be
  gamed; objections are advisory-by-design (overrules are logged and the
  run proceeds). Too weak for the one artifact in the system that is
  actually verified.
- (a) is honest, total, and cheap: refusal is always available, always
  safe, and converts a silent lie into a visible failure. Consistent with
  the standing gate rule: a reconciliation failure may only LOWER
  confidence or refuse.

## 3. Implementation

tools/pipeline/engine.py:
- `_sole_bare_boolean(stdout)`: returns True/False iff stdout is EXACTLY
  one bare boolean line, else None. Deliberately narrow — rich stdout
  (multiple prints, numbers, prose) stays silent rather than guessing
  which line is "the verdict".
- In `_answer_leaf`, after stance parsing: if the sandbox ran ok and
  printed a sole boolean, the declared stance must match it
  (True→AFFIRMS, False→DENIES) or the leaf REFUSES:
  `out.reconciliation_failure` records why; answer emptied; stance →
  UNDETERMINED; proposed estimate zeroed (never raised).
- Refusal propagates naturally: empty-answer leaves are excluded from
  `answered`; a single-leaf run then refuses outright ("every leaf came
  back unanswered") and can never seal the negation.
- `LeafOutcome.reconciliation_failure: Optional[str]` field added
  (default None; additive, no existing consumer breaks).

No confidence number is raised anywhere by this change.

## 4. Tests

tests/test_redteam_answer_correctness.py:
- Historical canary `test_answer_may_not_contradict_its_own_computed_
  comparison` promoted from strict-xfail to PASSING PIN (the exact case:
  print(4.1 < 3.5) → False, prose asserts "WAS lower" → no seal).
- New `TestComputeReconciliation`:
  - false-computation blocks affirmative prose (exact case);
  - TRUE computation blocks NEGATIVE prose (inverse);
  - agreeing computation still seals normally with unchanged answer and
    stance (refusal is surgical, not universal);
  - non-boolean stdout does NOT trigger the veto;
  - refused leaf carries confidence_estimate == 0.0.
Result: 9 passed + 8 xfailed in the file; full suite failures identical
to HEAD baseline (64 pre-existing, unrelated — verified via clean temp
worktree diff of FAILED lists).

Commit: 4c09a7b.

## 5. Pattern survey — verified artifact produced, not consumed (#9/#1)

Found while grepping the same shape:

1. FIXED THIS PASS — sandbox boolean vs asserted stance (above).
2. STILL OPEN — `produced_quant=out.sandbox_status == "ok" or bool(
   out.answer and re.search(r"\d", out.answer))` (engine.py ~553): ANY
   digit — including a year — satisfies a quant_required requirement.
   Canaried (`test_digit_in_prose_is_not_quantitative_evidence`,
   strict-xfail); deliberately left: fixing it changes requirement-gate
   scoring across many paths and deserves its own pass.
3. STILL OPEN — verified computation graded BELOW asserted prose: the
   sandbox Evidence is hard-capped INFERRED/≤0.45 while an unverified
   fetched page can reach PRIMARY/0.95, so in
   `best_leaf = max(answered, key=confidence)` the one VERIFIED artifact
   systematically loses direction-setting to mere assertion. The
   verification result is real but is not consumed where it should
   dominate. Related canaries (C1 family) cover the mechanism.
4. STILL OPEN — non-boolean compute outputs are unconsumed: compute
   printing "4.1" while the answer claims "3.9" passes every gate. Only
   the sole-bare-boolean case reconciles. Extending to numeric
   extraction needs unit/encoding normalization (see the synthesis-layer
   percent-vs-plain canary) and must not be done casually.
5. STILL OPEN — synthesis `detect_contradictions` picks each voice's
   value via max(key=abs): context figures manufacture contradictions,
   encodings of the SAME value read as conflicts (canaried, unfixed).
6. HEALTHY — checkpoint replay now verifies content_sha256 with
   "absence is failure" semantics; `verify_artifacts` is wired at the
   seal point (red team A6). These are the two places the pattern was
   already closed.

## 6. Carry-forward disposition (2026-08-24, landing pass)

- Item 2 (digit-in-prose quant): **FIXED** — commit 38aee73. Year tokens and
  bare sandbox 'ok' no longer satisfy quant_required; a numeric structured
  return value does. Canary promoted to passing pin.
- Item 3 (verified compute graded below prose): **CANARIED** — strict xfail
  `TestVerifiedComputeBelowProse` in tests/test_redteam_answer_correctness.py.
  A real fix re-ranks evidence classes or adds an entitlement-only channel;
  both would raise confidence (barred this pass).
- Item 4 (non-boolean outputs unreconciled): **CANARIED** — strict xfail
  `TestNumericReconciliation`. Needs unit/encoding normalization; must not be
  done casually (percent-vs-plain encodings).
- Items 5 (synthesis max(abs)) and 6 (healthy) unchanged.

## Residual honesty note

The reconciliation veto binds ONLY where the computation is unambiguous
(sole bare boolean). It closes the reported defect class completely for
comparison-shaped questions — the shape that produced the original wrong
seal — and refuses rather than guesses everywhere else.
