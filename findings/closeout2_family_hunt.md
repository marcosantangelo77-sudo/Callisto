# CLOSEOUT2 VERIFICATION PASS — family hunt across the eight failure families

Date: 2026-08-23 late night · Branch `closeout2` @ fb039fb (pushed).
Role note: this run was a cross-family hunt, not an improve pass. Every
finding below was re-executed against current HEAD bytes today, not copied
from prior reports. Families hunted: ALL EIGHT, prioritising #1/#2/#3.

## Family #1 — verification/scoring layers that never run (STILL LIVE)

Each item below reproduced by running the named check myself:

1. **Preregistration seals never verified (CRITICAL).** Ran the review
   branch's repro (`tests/test_review_prereg_seal_unverified.py` from
   8de3ff2): 3/3 FAIL on this branch. Tampered journal + recomputed hashes
   → `resolve()` scores CONFIRMED 0.75 while `verify_seal()` is False.
   Fix is one line at each seam (`score()` raises when
   `not self.verify_seal()`; `ClaimStore.load()` checks the embedded seal).
2. **Engine round() still mints tiers (HIGH, family #6 too).** Verified:
   `round(0.5497, 2) == 0.55 → PROBABLE`, `round(0.749999, 2) == 0.75 →
   CORROBORATED` via engine.py:469. ba0a63c's floor_conf landed in six
   sites but NOT this one (family #2 inside #6).
3. **Synthesis scorer inert on this branch** — `engine.run` has zero
   references to synthesize(); fixed by d28e118 on
   improve/synthesis-adoption, UNMERGED here.
4. **ThompsonRoutingPolicy + PredictionStore have zero production callers**
   (review run 10, confirmed by grep). Empirical routing is an archive,
   not a decider.
5. **B1 formula trust boundary unenforced** — repro fails: any spec dict's
   `model[*].formula` executes as a live xlsx formula. Boundary is a
   comment, not code.

## Family #5/#4 — structural agreement laundering (REPRODUCED LIVE)

Ran the constructions directly against HEAD:

- **Unanimous REFUTATION scores 1.0**: three PRIMARY items with
  `stance="refutes"` → `synthesize()` confidence **1.0**, zero
  contradictions surfaced. Stance gates nothing before corroboration
  credit. (S1, unfixed anywhere in the tree.)
- **Ten name-swapped mirrors of ONE document = ten independent voices =
  1.0.** Independence keys off declared host names, which mirrors control.
- **Report confidence is max-over-groups**: one lucky PRIMARY group +
  INFERRED filler → 0.70 overall.
- **SR1**: four hosts serving byte-identical payloads mint four
  independent keys; retrieval stops at "sufficient: 4 >= 2".

## Family #2 — the fix landed in one copy (NOW AT BRANCH GRANULARITY)

This is the dominant meta-failure of the whole operation tonight:

| fix | lives on | master/closeout2 state |
|---|---|---|
| clamp_to_ceiling floor (memory) | 4352ad1 build/declared-stance | **still round()s up here** (0.5497→0.55) |
| wiki routed LLM calls, domain-general topics, identity de-welding | acbb5d0..3adf879 review/ox-alpha-0823 | absent here |
| provenance write→read seam (`source_class` persisted) | same lineage | absent here |
| A2 gc refuse-on-corrupt-index | d8f0b17 fix/a14-a2-store-integrity | merged into closeout2 ✓ but NOT in origin/master |
| synthesis adoption (stage 6b) | d28e118 improve/synthesis-adoption | absent here |
| F6a/F6b/F5 ensemble+synthesis laundering fixes | 156a837 fix/confidence-laundering | absent here |

Review runs 9–10 said it first and it is now worse: **the merge train is
the single highest-leverage defect in the repo.** Reviewed-good fixes pile
up on side branches while every branch keeps the bugs. Recommend, ahead of
any new build work: drain the merge queue in review-run-10's order
(e4edcca hypothesis-writer fix FIRST — one API restart kills
create_hypothesis post-migration-013 otherwise), then cli-front-door
rebased over ba0a63c, then the memory/synthesis/laundering lineage.

## UNCOMMITTED WORK FOUND IN THIS WORKTREE

A peer left, uncommitted: `agp/provenance.py` (+gate-rejection supersede),
`tools/pipeline/retrieval.py` (+bind verdict to ledger), and two untracked
test files (`tests/test_redteam_retrieval_relevance.py`,
`tests/test_redteam_synthesis_corroboration.py`, 12 failing canaries for
real defects). I verified the supersede mechanism works end-to-end (a
gate-rejected body no longer mints PRIMARY once `record_gate_rejection`
fires) but left everything UNCOMMITTED — the wiring fixes only the
pipeline path; two of the peer's own repros still fail because
ProvenanceLedger alone does not know about gate verdicts. Whoever owns
this surface should finish it (make `_record()` status-aware per SR6, or
route the repros through the retriever) rather than have it half-landed
under someone else's name.

## Full-suite accounting on THIS branch today

11,204 passed / 58 failed / 9 skipped (serial run, ml_* collection errors
excluded). Failure census: artifacts_store ×13, money_path ×11,
backtest_e2e ×11 (pre-existing), synth_corroboration ×7,
confidence_laundering ×7, relevance ×5, lifecycle_claim ×2, prop_scanner
×1, p1_pipeline ×1 — i.e. the failures ARE the documented open
red-team/merge debt, concentrated exactly where this report says the
unmerged fixes exist.

## Bottom line

No new failure family needed: every break found tonight maps to families
#1/#2/#3/#5/#6. The system's remaining defect is not analytical — it is
logistical. The fixes exist, are reviewed, and are not where the code runs.
