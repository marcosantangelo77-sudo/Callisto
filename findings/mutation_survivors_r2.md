# Mutation survivors — round 2 (kill the high-severity set)

**Date:** 2026-08-24 · **Worktree:** `epistemics` · **Branch:** `fix/mutation-survivors-r2`
**Method:** wave-3 boundary-exact tests (`tests/test_mutation_gaps_r2.py`, 111
tests) + harness re-run (`scripts/mutation_harness.py`). Every pin below was
observed on the clean tree, then proven to flip under mutation by the re-run.

## Harness trap check (mandated by task)

The r1 report warned that `--no-summary` suppressed FAILED lines and faked 100%
survival. Before trusting any number this round:

1. Read the current harness: it passes `-rf` (not `--no-summary`) and treats
   rc≠0-with-no-FAILED as a collection-error kill.
2. **Smoke-tested with a known-fatal mutant**: applied `min→max` on kelly.py:282
   (the hard-cap inversion) in memory, ran `run_tests()` → kill detected via
   `test_kelly_dynamic_hard_cap_binds_from_above`. Restored byte-verified.
3. **Found a NEW trap the first run didn't document:** `TEST_MAP` scoped each
   target to its pre-existing test files only. The first re-run of this round
   executed 405 kelly mutants against a suite that did NOT include the new
   gap tests and reported zero kills for them. TEST_MAP now includes all three
   `tests/test_mutation_gaps*` waves; the full run was repeated after fixing.
4. Second trap, same family: the autosave daemon committed leftover mutants
   (`round→floor` in edge_confidence injury_model) into branch history mid-run,
   which then failed every edge_confidence test downstream. Fixed and pinned in
   d038d17; harness's dirty-tree pre-flight does not cover already-committed
   mutants — worth adding a known-good blob hash per target next pass.

## Survivor counts, before → after

| Target | r1 survivors | r2 survivors | killed |
|---|---|---|---|
| tools/kelly.py | 248 | **133** | 115 |
| tools/edge_confidence.py | 248 | **97** | 151 |
| agp/thresholds.py | 14 | **0** | 14 (wave 1+2, confirmed still dead) |
| agp/provenance.py | 12 | **0** | 12 |
| agp/adversary.py | 26 | **0** | 26 |
| tools/pipeline/retrieval.py | 67 | **0** | 67 |
| **Total** | **615** | **230** | **385 (63%)** |

Kill-rate on the two money/confidence targets went from 18–39% to ~52–68%.
Raw tables: `findings/mutation_survivors_raw_r1.json` (before),
`findings/mutation_survivors_raw.json` (after).

## Blast-radius ranking used (stated before work started)

1. kelly sizing internals (correlation penalty, ruin simulator, timing_value)
   — direct dollar loss.
2. ruin risk-ladder + safe-stake — sizing guidance flips bands.
3. edge_confidence ladders — confidence feeds tier multipliers → stakes.
4. thresholds/provenance/adversary constants — already killed wave 1; verified.
5. retrieval gates — quality cost, no direct dollars.

## What the wave-3 tests pin

- **kelly_full clamps** (161): p=1 and p=0 exact — kills min/max clamp nudges.
- **Smoothing ladder** (254–265): tier multiplier at ±ε around every band
  boundary (0.30/0.55/0.75/0.90), exact to 4dp — kills ==→!= and lerp nudges.
- **Variance dampener** (272–274): floor exactly 0.05; k-normalisation pinned
  at edge=0.0001 (kills the 0.001-floor mutant).
- **Portfolio correlation machinery** (376–440): diversification-ratio ladder
  over rho ∈ {-1,…,1} incl. clipping bounds, penalty = 1/sqrt(max(1,ratio))
  exact values + monotonicity, negative-rho never penalised, per-bet cap
  binds at exactly 5%, portfolio cap 20% + cap_hit flag.
- **Ruin analytical** (492–646): neg-EV ⇒ ruin 1.0 + zero stake; break-even
  boundary ratio==1 takes the certain-ruin branch; exact value pin 0.043926;
  safe-stake formula to the cent on a $10 bankroll; all five risk-level bands
  hit both sides of their thresholds; units cap at 10000.
- **Monte Carlo simulator** (613–646): seeded outputs pinned (rp=0.0,
  median 1149.85, dd 0.2424), sure-loss ruins 100%, strong edge grows median
  >4x — kills seed drift, axis swaps, ruined-threshold flips, percentile nudge.
- **timing_value** (689–792): regime boundaries at 4h/24h ±ε; decay lookup
  (spreads 1.1 vs default exactly 1.0) + remaining-fraction pins; hours clamp
  0.01; steam 0.3/0.7 split to 5dp; stale-line bonus w/ 12h cap; NO_BET at
  edge≤0; BET_NOW when decay dominates; middle-band default SLIGHT_LEAN_NOW.
- **calculate_units** (828–893): invalid-input error dicts; UNVERIFIED sizes
  nothing; unit-label ladder at every rung boundary (MAX/STRONG/STANDARD/
  HALF/LEAN/NO_BET).
- **edge_confidence**: NOISE_FLOOR_PCT pin; market-efficiency constants via
  distinct factor pins incl. the 0.80 default; source-class at exactly 2 books;
  book-count ladder 1→6; time/HHI/entropy/KL/JS boundaries AT threshold and
  either side; shading/trap/attention/RLM/steam/key-number ladders incl. caps;
  contrarian gate requires BOTH conditions; final clamp floors at exactly 0.0;
  score quantised DOWNWARD (0.508 not 0.507); parlay combination weights,
  tier mapping ON every threshold, weakest-leg source class, empty-legs.

## Remaining 230 survivors — disposition

Sampled and classified the 148 remaining high-severity-location mutants:

- **~60% are output-formatting digit nudges** (`round(x, 5)`→`round(x, 6)`
  on dict fields). These change a displayed decimal, not a decision. Killing
  them means pinning every reported float to full precision — brittle tests
  that would block legitimate formatting refactors. Recommend an operator:
  make the harness treat rounding-digit-only mutations as *equivalent* and
  stop counting them as survivors.
- **~15% are default-value `.get(x, default)` nudges** on paths where the
  caller always supplies the key. Equivalent-in-context; same recommendation.
- **~10% are genuinely equivalent mutants** (e.g. `min_bankroll <= 1` vs `< 0`
  when stake>1 guarantees the path crosses 0 first).
- **A real remainder (~20 mutants)** sits in `_expected_bets_to_ruin_neg_ev`
  and simulation parameter defaults — low blast radius (advisory numbers, no
  sizing path reads them). Not worth further test weight this pass.

## Cross-checks

- All 173 gap-wave tests green on clean tree (and after removing the stray
  committed mutant).
- Full suite: 11,509 passed, 44 failed — the 44 are the documented pre-existing
  red-team expectation failures (money-path repros stranded on unmerged
  branches `improve/money-path-landing`, `redteam/money-path-deep`, etc., per
  findings/review_2026-08-24_run3.md D1) plus 9 stale assertions inside
  `tests/test_redteam_mutation_survivors.py` that were written against fixes
  living on those unmerged branches (e.g. expecting `PanelVerdict` in
  agp.adversary — it lives in agp.ensemble; expecting h2h market_efficiency
  computed from totals' 0.85 constant). Count unchanged vs baseline class of
  failures; none introduced by this work.
- Production files restored byte-verified after the harness runs; the one
  autosave-committed mutant was reverted in d038d17.
