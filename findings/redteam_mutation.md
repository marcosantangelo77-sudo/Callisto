# Red Team: Mutation Testing — First Run

**Date:** 2026-08-24 · **Worktree:** `epistemics` (branch `redteam/mutation-testing`)
**Method:** hand-rolled operator-per-line harness (`scripts/mutation_harness.py`).
One token changed per mutant (min↔max, boundary flips, comparison swaps,
floor↔round↔ceil, integer/float constant nudges), targeted test subset per file,
restore + byte-verify after every mutant, kill = NEW failure vs a pre-recorded
baseline of already-failing tests.

## Headline numbers

| Target file | Mutants run | Killed | **Survived** | Kill rate |
|---|---|---|---|---|
| agp/thresholds.py | 20 | 6 | **14** | 30% |
| agp/provenance.py | 34 | 22 | **12** | 65% |
| agp/adversary.py | 79 | 53 | **26** | 67% |
| tools/pipeline/retrieval.py | 124 | 57 | **67** | 46% |
| tools/kelly.py | 405 | 157 | **248** | 39% |
| tools/edge_confidence.py | 301 | 53 | **248** | 18% |
| **Total** | **963** | **348** | **615** | **36%** |

Raw survivor list: `findings/mutation_survivors_raw.json`.

**The suite's overall mutation kill rate on the load-bearing paths is 36%.**
Two thirds of single-token breaks in the money and confidence code go unnoticed.

## Highest-severity survivors (money / confidence paths)

Each row: the surviving mutant, and what it means. "SHOULD have caught"
names the gap class; new tests in `tests/test_mutation_gaps*.py` now kill
the ones marked ✅.

### tools/kelly.py — stake sizing

| Line | Mutation | Consequence if shipped | Test that should catch it |
|---|---|---|---|
| 282 | `min(adjusted, hard_cap)` → `max` | The 5%-of-bankroll single-bet cap INVERTS: every bet becomes at least 5% of bankroll. Ruin guard becomes a ruin accelerator. | No test asserted an upper bound on `kelly_dynamic().fraction`. ✅ killed by `test_kelly_dynamic_hard_cap_binds_from_above` |
| 167 | guard `if b <= 0` → `<` | b==0 divides by zero instead of returning 0.0. | No zero-net-payout case existed. ✅ `test_kelly_full_zero_net_payout_b_eq_zero_guard` |
| 254 | `elif tier == "PROBABLE"` → `!=` | Tier smoothing lerp applies the WRONG band; PROBABLE scores get the SPECULATIVE multiplier (or worse). | Only exact tier-multiplier table was tested, not the smoothing bands. ⚠️ partially covered |
| 243/365/841 | unknown-tier default `0.00` → `0.01` | UNVERIFIED/unmapped tiers would still size 1% of a Kelly stake instead of exactly zero. | Tests never passed an out-of-table tier. ✅ `test_kelly_dynamic_unverified_confidence_bets_nothing` |
| 161 | clamp `max(0.0, ...)` → `max(0.01,...)` | Negative true-probability inputs silently become p=0.01. | No degenerate-input tests. ⚠️ open |
| 274 | dampener floor `max(0.05,...)` → `max(0.06,...)` | Variance dampener floor drifts. | Dampener tested only qualitatively. ⚠️ open |
| 385/388 | correlation penalty `max`→`min`, sqrt arg flip | Correlated portfolio sizing loses its penalty or inverts it — correlated bets sized as independent. | Portfolio tests only checked capping, not correlation monotonicity. ⚠️ open |
| 416 | per-bet cap `min(f, 0.05)` → `min(f, 0.06)` | Portfolio per-bet cap drifts 20%. | Not pinned to exact value. ⚠️ open |
| 504/528/529/556/562 | ruin-path guards and sign flips | Ruin probability can report >100%, negative stakes, or wrong safe-stake when bankroll ≤ 0. | Ruin path only tested for happy-path values. ⚠️ open |
| 635 | `ruined = min_bankroll <= 0` → `>=`/`<` flips | Simulation counts EVERY path as ruined (or none). | Simulator output not pinned against analytic case. ⚠️ open |
| 747/750/758 | bet-now vs wait EV comparisons flipped | timing_value() recommends betting when it should wait and vice versa. | No timing_value decision tests at all. ⚠️ open |
| 866-874 | unit-recommendation ladder boundaries `>`→`≥` | "3 units max" labels off-by-one at every rung. | Ladder never tested at exact rung values. ⚠️ open |

### tools/edge_confidence.py — confidence scoring

| Line | Mutation | Consequence | Test gap |
|---|---|---|---|
| 170-186 | base ladder `>=`→`>` + constant nudges (5.0→5.01, base 0.90→0.91 …) | Every edge-magnitude tier shifts by one cent of probability; claims near boundaries get the wrong base score. | Tests used mid-band values only; no boundary-exact assertions. ✅ killed by `tests/test_mutation_gaps_boundary.py::test_edge_magnitude_base_values_exact` |
| 535-541 | final tier mapping `>=`→`>` | Score exactly 0.90/0.75/0.55/0.30 demotes one tier. | Same boundary gap. ✅ killed (boundary wave) |
| 530 | final clamp `max(0.0,…)` → `max(0.01,…)`, `round→floor` | Sub-noise edges carry ≥0.01 phantom confidence; quantization direction unpinned. | ⚠️ open (partially pinned) |
| 39 | `NOISE_FLOOR_PCT = 0.5` → `0.51` | Noise floor drifts; sub-noise edges reclassified as marginal. | Constant never imported/asserted anywhere. ⚠️ open |
| 43-52 | MARKET_EFFICIENCY constants ±0.01 | Per-market adjustments all shift together. | ⚠️ open |

### agp/thresholds.py — the shared confidence vocabulary

Every constant in this file survived mutation:

| Line | Survivor | Consequence |
|---|---|---|
| 19-22 | TIER_*_MIN ±0.01 | ConfidenceTier.from_score demotes/promotes everything within a cent of each boundary. **The DB CHECK constraint and the runtime tier ladder could disagree with nobody noticing.** |
| 28-31 | MAX_CONFIDENCE_BY_SOURCE ceilings ±0.01 | Source-class caps drift. |
| 45 | DB_CONFIDENCE_FLOOR 0.30→0.31 | Floor drifts from the schema CHECK value the comment says it must match. |
| 51-53 | CONTRADICTION_PENALTY ±0.01 | Adversary penalties change silently. |
| 57 | `floor_conf` → round/ceil (renames fn) | Import errors everywhere — but note the harness initially reported this SURVIVED because collection errors produced no FAILED lines (see Harness lessons). |

✅ All killed by `tests/test_mutation_gaps_constants.py`, which imports the
constants and asserts exact equality plus consumer boundary behavior
(`ConfidenceTier.from_score(TIER_VERIFIED_MIN - 0.001)` etc.).

### agp/provenance.py

| Line | Survivor | Consequence |
|---|---|---|
| 212 family | `min(prior, conf)` → `max(prior, conf)`; `prior >= floor` flips | **The relabel-evidence demotion logic can be inverted into promotion** — exactly the laundering vector dd4fb18 fixed — without any current test failing. ⚠️ partially killed by wave 1 |
| 199 | rank-default `0`→`1` on demotion compare | Unknown classes stop being demoted. ⚠️ open |

### agp/adversary.py

| Line | Survivor | Consequence |
|---|---|---|
| 118/120 | spread thresholds `>=`→`>` | Ensemble disagreement cap misses the exact-threshold spread (float-boundary: only [0.0, 0.15]-style exact pairs observe it). ✅ killed |
| 130 | `s <= ceil_` → `<` | Score exactly equal to ceiling gets re-floored differently. ✅ killed |
| 138 | ceiling floored via `ceil(…*100)/100` | Disagreement ceiling ROUNDS UP — raises confidence by up to 0.01. ⚠️ open (needs exact-ceiling pair) |
| 490 | blocking veto `max(0.0,…)` → `max(0.01,…)` | A blocked claim keeps 0.01 phantom confidence. ✅ killed |

### tools/pipeline/retrieval.py

| Line | Survivor | Consequence |
|---|---|---|
| 119 | gate default `min_coverage=0.25` → `0.26` | Admission threshold drifts; nobody pins the documented default. ✅ killed |
| 220 | question-type translation `d.score >= best*0.9` → `>` | Near-tie translations drop out. ⚠️ open |
| 722 | sufficiency `len(keys) >= min_independent` → `>` | Leaves needing exactly N sources stop at N−1. ⚠️ open |
| 724 | termination record `max(1, min_indep)` → `min` | Zero-requirement leaves divide by zero / misrecord. ⚠️ open |
| 360 | gain estimate `duplicate_voice and indep_short == reasons` → `!=` | Expected-gain gate skips sources it should fetch (and vice versa). ⚠️ open |

## New tests written

- `tests/test_mutation_gaps.py` (33 tests): floor-conf direction, kelly tier
  boundaries, zero-payout guard (incl. monkeypatched b==0), hard-cap binding,
  UNVERIFIED sizes zero, variance-dampener monotonicity, relabel-evidence
  never raises sub-floor items, provenance ceiling clamps, ensemble spread
  boundaries with float-exact pairs ([0.0, 0.15] is the only exact half-
  threshold pair), apply_verdict blocking/penalty/no-bonus, gate default.
- `tests/test_mutation_gaps_boundary.py` (18 tests): exact edge-magnitude
  base ladder (5.0/4.99/3.0/2.99/2.0/1.99/1.0/0.5/0.49), source-class ladder,
  score ≤ ceiling, calculate_units ladder monotonicity.
- `tests/test_mutation_gaps_constants.py` (11 tests): exact-value pinning of
  every thresholds.py constant + consumer behavior through
  `ConfidenceTier.from_score` and the contradiction-penalty arithmetic.

All 62 pass on clean tree. Verified kills (spot-check of ~45 high-severity
survivors): 19 previously-surviving mutants now die, including the inverted
hard cap (kelly 282), zero-payout guard (167), blocking-veto 0.01 (adversary
490), ensemble boundary family, and the full thresholds constant family.

⚠️ Still open after wave 1+2 (~85 of the high-severity set): kelly
correlation-penalty internals (385/388/416), ruin simulator (504-646),
timing_value decision logic (689-790), retrieval sufficiency/gain gates
(360/722/724), provenance relabel inversion (212). These need deeper tests
than boundary pins — recommended next red-team pass.

## Harness lessons (for whoever runs this again)

1. **`--no-summary` suppresses FAILED lines** → first full run reported 615/615
   survivors because kills were invisible. Always smoke-test the harness with a
   known-fatal mutant before trusting a survival report.
2. **rc≠0 with no FAILED lines is a collection error = a kill.** Renaming
   mutations (floor_conf→round_conf) break imports, not assertions.
3. **This repo's autosave daemon commits uncommitted files every 5 min.**
   Mid-run mutants got committed to history twice; the second run's "original"
   was itself a mutant. Harness now refuses dirty targets, restores from an
   on-disk backup, and kills hung pytest process groups (`start_new_session`
   + `killpg`).
4. Float boundaries: `1.00-0.70 == 0.30000000000000004`. Exact-threshold
   observations need pairs like `[0.0, 0.15]`; otherwise `>=`→`>` is an
   equivalent mutant.

## Verification

- Production tree clean vs HEAD after run (byte-verified restore per mutant).
- Baseline pre-existing failures (15 red-team expectation tests awaiting fixes
  on other branches) excluded from kill determination.
