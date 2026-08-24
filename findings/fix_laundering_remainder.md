# Fixing the laundering remainder (7 red → 2, both argued and left failing)

Branch: `fix/laundering-remainder`, commit `76a3f5c` (pushed).
Hard rule honoured: no assertion weakened, skipped, xfailed, or edited. The
only test-file change restores a missing IMPORT (`inherited_ceiling`) that an
earlier pass had added on `fix/confidence-laundering`; it touches no
assertion. See "A0" below.

## Root cause of most of this block: the earlier fixes never landed

The A1/A2/A3 pass lived on branch `fix/confidence-laundering`, branched from
old master `fa2bea9`. This worktree's branch (`fix/laundering-remainder`,
sitting on the backlog2 merge `8123b86`) never received them — family #2 in
PATTERNS.md, at the branch level: a fix landed in one copy while another kept
the bug. The test file here had also lost the `inherited_ceiling` import the
earlier pass documented as its A0 repair.

So three of the seven "surviving" reproductions are re-manifestations of
already-solved defects, ported here by hand (a straight cherry-pick would have
reverted newer S5/null-classifier work present only on this branch):

### F6a — unknown author reads every reviewer as independent (CRITICAL)
`agp/ensemble.py::ReviewProvenance.independent`. With `author_model=''`
(the engine's `adversary.attack` call omits author_model), `normalize_model('')`
matched nothing, so ANY reviewer was "independent" and SELF_REVIEW_CEILING
(0.54) could never engage on the pipeline path. Fix: unknown author ⇒
self_review, conservative direction.

### F6b — model identity judged on spelling, not weights (HIGH)
Same module: `gpt-4o-proxy-alias` reviewed its own conclusion as a genuinely
different reviewer. Fix: `_same_weights()` resolves alias/proxy/mirror/replica
suffixes to the base identity; ambiguity counts as self-review.

A1+A2 together green two tests with one mechanism change (`independent`),
plus `test_normalize_model_strips_provider_prefix_both_sides_equally` and
`test_unattributed_reviewer_is_correctly_rejected` stay green unchanged.

### F4c/F5 — mixed-provenance groups rode their strongest member's ceiling (CRITICAL)
`tools/pipeline/synthesis.py::confidence_from_agreement` took the group
ceiling from `best_class = MAX(items)`: one PRIMARY item let two INFERRED
voices score VERIFIED (1.0). Fix: per-class accounting — each class earns
credit only within its own ceiling:
`score = max over classes of ceiling(class) * frac(class_voices)`.
Single-class behaviour is unchanged (the honest-negative pins in
TestCorroborationCeiling still pass); all 47 tests in
test_build_i3_synthesis.py + test_build_b4_inheritance.py pass, plus 8,993
across w4_cross_model / s5_vacuous / confidence_inflation.

## The two left RED deliberately — internally contradictory tests

Both assert the bug's output AND its invariant simultaneously; no code can
satisfy either pair. Per the hard rule they stay failing.

1. `test_synthesis_best_class_laundering_in_group` asserts
   `score == 1.0   # the bug, demonstrated` AND
   `score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]   # FAILS`.
   1.0 > 0.75 always. My fix satisfies the second line (per-class accounting
   scores this group 0.70); the first line pins the defect itself.
   ARGUMENT TO THE OWNER: delete or invert the first assertion — the file's
   own convention elsewhere converts demonstrated defects into fix pins;
   this one half-converted. Post-fix form:
   `assert score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]` alone (or
   `== 0.70` to pin per-class accounting exactly).

2. `test_panel_verdict_blocking_veto_returns_rounded_up_score` asserts
   `out == 0.84` AND `out <= 0.836`. Unsatisfiable by arithmetic, and the
   round-up it demonstrates was already fixed (veto path uses floor_conf):
   out == 0.83 today. This is a stale duplicate of the fixed F1 rounding
   family, not an open break. Correct form: keep only `out <= 0.836`.

The invariants both protect ARE enforced in production: A3 above, and
floor_conf on the veto path.

## Where I looked for more family-#6 violations (rounding moving numbers UP)

PATTERNS.md family #6; property sweep found 1,385 upward-moving rounds.
Audited this session:

- `agp/ensemble.py::PanelVerdict.apply` veto path — uses `floor_conf`. Clean.
- `tools/pipeline/synthesis.py::confidence_from_agreement` — all score paths
  end in `floor_conf(...)`, including the new per-class branches. Clean.
- `tools/research_program.py::inherited_ceiling` — final `round(min(...), 4)`
  is applied to a value that only ever LOWERS raw scores via min() in
  `clamp_parent_confidence`; rounding a ceiling to 4dp cannot raise any
  clamped parent above its evidence-implied bound by more than 5e-5 against
  a ceiling that itself came from the same rounding. No upward seam found,
  but noted honestly: the round() sits inside the ceiling computation, so a
  caller using the ceiling directly (not through clamp) sees the rounded
  number — acceptable at 4dp, would be a violation at 2dp.
- `grep -rn "round(" agp/ tools/pipeline/ tools/research_program.py` —
  remaining hits are display formatting or floor-directional. No new
  violations fixed this pass; the sweep harness that found the 1,385 should
  be re-run over the synthesis per-class path as follow-up.

Also checked family #3 (absence-as-success) at the touched seams: empty
`reviewer_models` with unknown author now yields self_review (fail closed);
empty group classes fall back to INFERRED's 0.55, never PRIMARY.

## Open item surfaced, not fixed (unchanged from prior findings)

F7.3: `best_source_class` on ResolutionRecords has no seal/provenance check
at the record layer. Its test PASSES AS WRITTEN because it asserts the
exploit works (>0.70). Closing it needs a seal-verification seam in
`_rec_from_mapping`/`inherited_ceiling` and flipping the assertion — a design
decision I did not make unilaterally. Tracked as follow-up work.

## Suite accounting

laundering block: 7 → 2 (both argued above). Nothing outside the block
regressed: i3_synthesis (40), b4_inheritance (7), r3_adversary (19),
w4_cross_model + s5 + inflation (8,993) all pass. Unrelated pre-existing
baseline failures untouched.
