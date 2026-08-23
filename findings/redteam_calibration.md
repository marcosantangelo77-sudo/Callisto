# RED TEAM — CALIBRATION SCORING ITSELF

Surface chosen: **calibration scoring** (retrodiction scoring, batch
reporting, the routing score store, the Thompson policy, and the inheritance
rule those scores feed). Not previously covered: redteam_loop H6 flagged the
*coverage* hole in `score_brier` but nothing attacked the referee layer as a
surface — the code that decides whether everything else looks good. I chose it
because the last two commits on this branch (`smoke5 separation rescore`,
`design_estimate_ceiling`) are both *decisions made from these numbers*: if the
measurement layer is gameable or self-confirming, every conclusion drawn from
it inherits the flaw.

Tests: `tests/test_redteam_calib_scoring.py` — 13 tests, all currently PASS.
Per repo convention each is a deterministic reproduction of a confirmed defect;
when a defect is fixed its test FAILS (canary).

Method note: property/random sweeps were used where they bite (`_implied_outcome`
2000-sample sweep; Thompson decisions 500-sample sweeps); the rest are boundary
reproductions at exact values.

---

## K1 — `_implied_outcome` fabricates ground truth from the prediction itself (CRITICAL)

`tools/retrodiction/batch.py::_implied_outcome`: when a `BatchResult` carries
no `answer_binary` (exactly the case for rows rehydrated from checkpoints or
the results JSONL written by older code), the "realised outcome" used by
`build_report`'s calibration table is **recovered from the recorded brier**, and
on any ambiguity falls back to `sign(p)` — the prediction's own direction.

- Random sweep: with attacker-chosen brier values, implied outcome agrees with
  the prediction in **2000/2000** cases.
- Forged flip: truth y=1, model said p=0.05; a row stored with `brier=0.0025`
  and no `answer_binary` reports the call as CORRECT.
- Whole-table fabrication: ten truth-less rows render a textbook-perfect
  calibration table (`(0.15, 0.0), (0.85, 1.0)`) and the verdict
  "strongly better than chance".

Why this matters specifically now: MORNING_REPORT's central empirical claim
("systematically UNDERCONFIDENT, bin 0.2–0.4 predicted 0.33 realised 0.60") was
read off this table. The live path does set `answer_binary`, but resumed and
legacy rows do not, and nothing marks those bins as derived rather than
observed. The system's most important measurement can silently become
self-confirming across exactly the resume boundary checkpointing was built to
make safe.

Also in the same headline block: `sealed_rate` counts `r.sealed`, which
`_run_one` copies from the researcher's own run trace — no seal object is ever
inspected.

**Fix direction:** refuse to compute calibration over rows without an explicit
`answer_binary`; emit `n_truth_known` next to `calibration_overall`; verify
`sealed` against a seal handle, not the trace flag.

## K2 — The routing store has no question identity, no coverage, no class (HIGH)

`tools/routing/scores.py` + `policy.py`:

1. **Duplicates count**: one question_id recorded 100× yields n=100,
   basis="measured". Volume substitutes for breadth; `record()` never dedups
   and `aggregate()` never groups.
2. **Selection loop made real** (H7.3 was a prediction; this is the number):
   cherry-picked model (10 easy answers only) beats honest model (30 full-set
   answers) **500/500** Thompson decisions. Nulls are invisible to the ranking
   input by design ("absence of a record is honest") — honest for the audit,
   wrong for a comparator that never sees who was skipped.
3. **task_class stored but never read**: `decide()` keys on role alone.
   Measurements from `research_synthesis` route `decomposition` calls with
   basis="measured".
4. **Recency rewrite without rewriting history**: 60 terrible observations +
   15 appended good ones put the posterior draw mean at ~0.33 vs lifetime raw
   ~0.38. The docstring says staleness is handled "at read time, never by
   rewriting history" — but a read-time re-weighting that outweighs 4x more
   data IS a history rewrite with extra steps. Nothing bounds how little data
   can overturn a record (HALF_LIFE_RECORDS=60 means ~15 rows ≈ full weight).

**Fix direction:** per-question dedup or question-level aggregation before
`record`-side counting; write nulls as records flagged UNSCOREABLE so coverage
is visible to `decide`; key aggregates on task_class; cap recency re-weighting
by fraction-of-record, not absolute half-life.

## K3/K4 — The inheritance rule converts luck into ceilings (HIGH)

The module's promise: "Zero resolved descendants caps the claim at SPECULATIVE
forever... An unresolved claim is structurally UNABLE to look like a resolved
one." Verified false in three ways:

1. **Coin-flip descendants break the cap.** 40 descendants predicted at
   p=0.5+ε via the REAL path (`resolved_claim_record`) resolve hit/miss by
   chance; Wilson LB on ~20/40 hits drives the parent ceiling to **~0.63–0.66**
   (random seed dependent) — above the 0.55 SPECULATIVE cap, into PROBABLE.
   Each coin-flip carries Brier ≈ 0.25, yet direction-only hits are full
   currency: the calibration term multiplies but cannot veto, and the H6 fix
   direction ("weight inheritance hits by extremity") was never applied.
   With pinball stripped entirely (plain dicts without the key), 40 lucky hits
   reach the **SECONDARY cap 0.75 = CORROBORATED**.
2. **Self-supplied pinball launders misses.** `normalize_records` accepts any
   dict; forged `pinball_score=0.0001` on 35 real misses lifts the ceiling from
   0.565 to **0.768** (+0.19). The calibration term trusts a number the
   resolving side writes about itself — the exact laundering shape Z-series
   found in the seal path.
3. **Hits with near-chance sharpness still give full support**: pinball 0.49 on
   40 'hits' still reaches the 0.75 cap. Provenance is capped; informativeness
   is not.

**Fix direction:** hits must be weighted by |p−0.5| extremity or excluded below
a sharpness floor; treat missing/out-of-range pinball as miss-grade error, not
binary; clamp the ceiling to min(SPECULATIVE_CAP unless support comes from
sharp predictions).

## K5 — Verdict and magnitude metrics misprice honesty (MED)

- At exactly 50% null rate the majority-null guard (`> 0.5`) does not fire;
  ONE scored row earns "strongly better than chance" in the headline verdict.
- `magnitude_score(p == market)` returns `directional_edge = −0.0`: deferring
  exactly to the market is scored as a WRONG directional bet. Combined with
  beat_market_rate counting only e>0, the metric pushes the system off the
  market line even when the market is right — a small structural incentive to
  manufacture disagreement, which is the mirror image of the underconfidence
  bias the smoke5 batch measured.

## What held up (honest negatives)

Attacks that did NOT land, kept as reasoning not tests:

- `Prediction.__post_init__` genuinely rejects NaN/Inf probabilities
  (comparison-based guard happens to catch NaN). Tried.
- `clamp_parent_confidence` survived a 5,000-sample property sweep for
  raise-violations: it never lifts a raw score, including through the
  zero-record SPECULATIVE path. The F2 rounding bug from redteam_confidence is
  gone — `floor_conf` is doing its job at this call site.
- `ModelScoreStore.record` validates brier ∈ [0,1], so negative-brier forgery
  dies at the bridge (`write_routing_scores` propagates the ValueError).
  Note this means one bad row crashes the whole bridge write rather than being
  skipped — fail-closed, consistent with the house style, worth knowing.
- `score_brier` itself is arithmetically sound on duplicates (they're visible
  in the inputs, unlike the routing store where they're invisible in output).
- `resolved_claim_record` computes its own pinball honestly for binaries; the
  laundering vector is only open at the `normalize_records` dict seam, not the
  sanctioned producer.

## Priority

| # | Finding | Severity |
|---|---|---|
| 1 | K1 — calibration fabricates ground truth on truth-less rows | CRITICAL |
| 2 | K4 — coin-flip/laundered descendants lift parent ceilings past promised caps | HIGH |
| 3 | K2 — routing store: dup inflation, cherry-pick wins, class-blind decide | HIGH |
| 4 | K5.2 — zero-edge deference penalised (manufactured-disagreement incentive) | MED |
| 5 | K5.1 — 50% null batch blessed by verdict | MED |

Cross-cutting observation for the next builder: findings K1 and K2 are both
instances of the same disease the morning report named once already — **a
number computed from whatever survived, presented as a number about the
world**. The fix is never another threshold; it is carrying the denominator
(n_truth_known, n_attempted, n_questions_distinct) inside every aggregate.
