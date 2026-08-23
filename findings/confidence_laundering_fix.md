# Fixing the confidence-laundering red-team block (7 failures → 2, both argued)

Branch: `fix/confidence-laundering`, baseline `origin/master` = fa2bea9.
Hard rule honoured: no assertion weakened, skipped, or edited. One test-file
change was made — adding a missing import (see A0) — which touches no
assertion.

## What was actually broken

### A0 — three "failures" were a missing import, not production defects
`tests/test_redteam_confidence_laundering.py` imports only
`clamp_parent_confidence` from `tools.research_program`, but three tests in
`TestInheritanceRule` call `inherited_ceiling(...)` directly. All three died
with `NameError` before touching production code. Added `inherited_ceiling`
to that import line; **all three then passed unchanged** against HEAD's
production code:

- `test_stale_resolutions_earn_hit_rate_credit` — stale records are already
  excluded from Wilson support (`n_resolved - n_stale`) AND from the lift gate
  in effect: with outcome="stale", pinball=None → brier err 1.0, calib=0.0,
  support=0.0; the F7 docstring's feared 4-hits+1-stale lift does not occur on
  current code. The commit message of 667656e ("stale-credit lift to 0.7095")
  described pre-ba0a63c behaviour.
- `test_pinball_none_on_quantile_style_record_scores_as_clean_hit` — a flat
  50% miss record already yields ceiling < 0.62 (calib 0, Wilson LB at 50%
  ≈ 0.35 keeps base near SPECULATIVE). Passes as written.
- `test_best_source_class_is_self_reported_on_records` — 15 hits with claimed
  PRIMARY do exceed 0.70 today. NOTE: this test PASSES but the finding it
  documents is real and OPEN — `best_source_class` on ResolutionRecords still
  has no seal/provenance check. The red team wrote the test asserting the
  laundering WORKS (>0.70); it does. Closing it requires a seal-verification
  seam in `_rec_from_mapping`/`inherited_ceiling` (design decision needed:
  where seals live at the record layer), tracked here as follow-up work.

## Production fixes

### A1 — F6a: empty author_model read every reviewer as independent (CRITICAL)
`agp/ensemble.py::ReviewProvenance.independent`. With `author_model=''`
(the common case — engine.py's `adversary.attack` call omits author_model),
`normalize_model('') == ''` matched nothing, so ANY reviewer was
"independent" and SELF_REVIEW_CEILING (0.54) could never engage on the main
pipeline. Fix: unknown author ⇒ not independent (conservative), documented in
the docstring.

### A2 — F6b: model identity was spelling, not weights (HIGH)
`agp/ensemble.py`. `gpt-4o-proxy-alias` reviewed its own conclusion as a
"genuinely different reviewer". Added `_same_weights()`: names that share a
base name modulo alias markers (-proxy/-alias/-mirror/-replica) resolve to
the same identity. Conservative direction: ambiguity counts as self-review.

### A3 — F5/F4c invariant: mixed-provenance groups rode their strongest member's ceiling (CRITICAL)
`tools/pipeline/synthesis.py::confidence_from_agreement` took the group
ceiling from `best_class = MAX(items)`: one PRIMARY item let two INFERRED
voices score VERIFIED (1.0). Replaced with per-class accounting:
`score = max over classes of MAX_CONFIDENCE_BY_SOURCE[class] * frac(class_voices)`.
The repro group now scores 0.70 ≤ SECONDARY's 0.75; single-class behaviour
unchanged (honest-negative pins in TestCorroborationCeiling still pass);
all 40 tests in test_build_i3_synthesis.py pass.

## Two tests left RED deliberately — they are internally contradictory

Both assert the bug's output and the invariant simultaneously; no code can
satisfy either pair.

1. `test_synthesis_best_class_laundering_in_group` asserts
   `score == 1.0   # the bug, demonstrated` AND
   `score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]   # FAILS`.
   1.0 > 0.75 always. My fix satisfies the second line (0.70); the first
   line pins the defect itself. ARGUMENT FOR EDITING WHEN THE OWNER NEXT
   TOUCHES THIS FILE: delete or invert the first assertion — the file's own
   convention (see 667656e) converted demonstrated defects into fix pins;
   this one half-converted. The correct post-fix form is
   `assert score <= MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]` alone (or
   `== 0.70` to pin the per-class accounting).

2. `test_panel_verdict_blocking_veto_returns_rounded_up_score` asserts
   `out == 0.84` AND `out <= 0.836`. Unsatisfiable by arithmetic. Moreover
   the round-up it demonstrates was ALREADY FIXED by ba0a63c (veto path uses
   floor_conf): out == 0.83 today. This is a stale duplicate of the fixed
   F1 family, not an open break. Correct form: keep only `out <= 0.836`.

Per the job's hard rule both stay red until their owner rewrites them; the
invariants they protect are enforced elsewhere (A3 above; ba0a63c's floor_conf).

## Open item surfaced, not fixed
F7.3 (`best_source_class` unverified at the record layer) remains REAL even
though its test passes-as-written (it asserts the exploit works). Recommend a
follow-up: seal-check `best_source_class` in `tools/research_program.py` the
way `memory_epistemics.admit_learning` does, then flip the test to assert the
ceiling STAYS ≤ 0.75 when the claim is unsealed.

## Suite accounting vs the 21-failure baseline
This worktree @ fa2bea9 measures (excluding 2 collection-error modules,
test_ml_classifier.py / test_ml_drift.py — broken imports, part of the
pre-existing baseline):
- laundering block: 7 → 2 (5 greened: 3 via import repair + existing-correct
  production code, 2 via A1/A2; plus the F5/F4c invariant satisfied though
  the test stays red on its contradictory demo line)
- unrelated pre-existing failures untouched:
  test_lifecycle_claim.py ×2, test_prop_scanner.py ×1,
  test_backtest_e2e.py ×11, collection errors ×2 modules
Total failures after: 2 (redteam) + 14 (unrelated) = 16 visible + baseline
collection/broken-module differences account for the rest of the 21.
Nothing outside the laundering block was modified.
