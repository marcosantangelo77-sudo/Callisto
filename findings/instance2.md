# Instance 2 findings — the money path (tools/bet_executor.py, tools/kelly.py, tools/clv_tracker.py, tools/sizing.py)

Session opened 2026-08-22 on branch audit/tier0-money.
Method: START_HERE brief; AUDIT_MANDATE §2 protocol; ROADMAP §0/§3.3 treated as
unverified prior and re-derived. Gate-tier findings (instance3) read first —
their design answer #3 ("gate on CLV, not win rate") is what makes the CLV
unit audit below the priority item.
Characterization tests: NEW files `tests/test_tier0_money_{kelly,sizing_and_units,executor}.py`
(95 tests, all passing, 0.45s). No live-execution path armed, touched, or enabled
anywhere; the executor fixture is never `enable()`d and every DB is a temp file.
Environment note: pytest-asyncio/numpy/python-dotenv/pytest-cov were missing on
this machine and were installed; without pytest-asyncio, 21 of the 29 pre-existing
money-path tests error — the "46% coverage" baseline itself is partially an
artifact of an unrunnable async suite on THIS sandbox (workstation coverage may differ).

---

## WORK UNIT 1 — Which half is covered? (coverage decomposition)

Measured with pytest-cov over the money-path test subset only (same method as
the Wave-1 baseline, so numbers are comparable):

| module | baseline | after my tests | what the baseline covered |
|---|---|---|---|
| tools/kelly.py      | 39.6% | **72%** | plumbing: imports, dict-shaping, timing_value constants |
| tools/clv_tracker.py| 45.7% | 46% (30% alone; existing tests cover the rest) | devig helper + paper-trade log path |
| tools/bet_executor.py| 46.3% | **55%** | regime clamps, status(), log plumbing |
| tools/sizing.py     | (in kelly's orbit) | **100%** | almost nothing |

The brief's suspicion is CONFIRMED: the near-46% baseline was exercising
plumbing, not arithmetic. Zero baseline coverage of: `kelly_full`,
`kelly_fractional`, `kelly_dynamic` (the LIVE SIZER), `kelly_portfolio`,
`ruin_probability`, `calculate_units`, `compute_stake`, `compute_portfolio_stakes`
caps, `kelly_with_push`, `uncertainty_adjusted_kelly`, `best_price`, `bet_size`.
Every dollar-moving function in the codebase was untested before this session.

## [VERIFIED] bet_executor.py — the naive arming fix is still the loaded gun, and the sizing stack behind it now has pinned arithmetic
Blast radius: ARMING (context)
Evidence: `generate_paper_trade_signal` still hard-gates to paper_trading; nothing
in my files changes that. What changed: `compute_stake`, `compute_portfolio_stakes`
(per-game 8% / per-sport 15% / portfolio 25% caps, MIN_BET floor), preflight gates
(disabled / min-edge / daily-loss / max-bet), and the drawdown kill-switch now have
95 hand-derived characterization tests. Arming later will fail loudly against these
if behaviour drifts.
Falsifier: run `python3 -m pytest tests/test_tier0_money_executor.py -q`; any pass
with an armed executor would require code changes these tests pin.
For: whoever arms the path (owner decision)

## [VERIFIED] kelly.py kelly_full/kelly_fractional/kelly_dynamic — arithmetic CORRECT, proof below
Blast radius: n/a (clean finding)
Evidence: proof + 20 hand-derived test vectors, all matching to 1e-6.
Falsifier: any test vector in tests/test_tier0_money_kelly.py failing without a
corresponding deliberate change.
For: unowned (this is the good news finding)

### WRITTEN PROOF — Kelly sizing (kelly_full, and by extension sizing.kelly_binary)

Setup: binary bet, decimal odds d, net odds b = d − 1, win prob p, lose prob
q = 1 − p, fraction f of bankroll staked. Wealth after one bet: W₁ = W₀(1 + bf)
with prob p, W₀(1 − f) with prob q.

Claim 1 (expected-log optimality): the growth-optimal fraction is
f* = (bp − q)/b, clamped to [0, 1] (and by prudence to [0, 1] anyway since
bp − q ≤ b·1 − 0 < b ⇒ f* < 1; f* < 0 exactly when the bet is −EV).

Proof. Expected log growth g(f) = p·ln(1 + bf) + q·ln(1 − f).
g′(f) = pb/(1 + bf) − q/(1 − f). Setting g′ = 0:
  pb(1 − f) = q(1 + bf) ⇒ pb − pbf = q + qbf ⇒ pb − q = bf(p + q) = bf
  ⇒ f* = (bp − q)/b. ∎ (uniqueness/maximum:)
g″(f) = −pb²/(1 + bf)² − 1/(1 − f)² < 0 for all f ∈ (0, 1), so g is strictly
concave and the critical point is the unique global max on the domain.
Boundary check: g(0) = 0; g(f*) = p·ln(pd) + q·ln(qd/… ) — standard result
g(f*) > 0 iff bp > q, i.e. iff EV > 0. So for −EV bets the max on [0,1] is at
f = 0, which is what the `max(0.0, …)` clamps implement.

Claim 2 (code equivalence): kelly_full(edge, odds) computes
p = implied(odds) + edge, b = decimal(odds) − 1, f = (bp − q)/b.
Since EV per unit staked = p·d − 1 = p(1+b) − 1 = bp − q, the code's f* equals
the theoretical f* exactly. The only lossy steps are the 6-dp rounding
(`round(fraction, 6)`) and the clamp — both pinned by test.

Claim 3 (fractional Kelly): kelly_fractional returns φ·f* with φ ∈ (0,1].
This is NOT optimal for any criterion; it trades ~φ² of growth rate for a ~φ
reduction in variance (the growth curve is quadratic near f*: g(f*+ε) ≈
g(f*) − ½|g″|ε², so quarter-Kelly gives up ~9/32 of max growth while staking
¼). Documented intent, correctly implemented.

Claim 4 (push-aware, sizing.kelly_with_push): with push prob π, wealth is
1 + bf (p_win), 1 (π), 1 − f (p_loss = 1 − p_win − π).
g′(0) = p_win·b − p_loss, so f* = (b·p_win − p_loss)/b — exactly the code.
Verified numerically: (0.54, 0.04, 1.9091) → 0.078 (matches the module's own
docstring claim and my independent derivation).

Claim 5 (uncertainty scaling, sizing.uncertainty_adjusted_kelly): a stepwise
scale {0, 0.3, 0.7, 1.0} on info_ratio = edge/noise, times 0.25. This is a
heuristic, not derived — but its DIRECTION is right (shrink stake with
estimation error) and its ladder is monotone. Pinned as characterization.

Falsifier for all claims: re-derive g′ by hand or run a Monte-Carlo of
log-wealth over 10k bets at the code's f* vs f*±ε; the code's f* must
maximize median log-wealth. Any deviation falsifies.

## [VERIFIED] clv_tracker.py `_log_clv`/`log_paper_trade_clv` — canonical clv_prob_bp path is unit-consistent and devigged
Blast radius: n/a (clean)
Evidence: both writers compute (close_fair − placement_fair) × 10000 where both
fairs are `_half_vig_devig` outputs (probability, 0..1). Units match, sign
convention documented ("positive = better price"), legacy clv_cents column now
populated with the same bp value so no NEW mixed-unit rows accrue. Tests pin
the devig arithmetic and the bp computation.
Falsifier: a row where clv_prob_bp ≠ (close_fair − place_fair)×10⁴ ± 0.1.
For: unowned

## [VERIFIED] THE CLV GATE UNIT BUG — hypothesis.py:1189-1192 (READ-ONLY for me; defect documented, not fixed)
Blast radius: SILENT — the statistic the CLV-first rebuild depends on cannot fail
Evidence (re-derived independently, ROADMAP §3.3 CONFIRMED):
  - `get_clv_report()` returns `positive_clv_rate` as PERCENT (10.0 for 1-in-10;
    line 758 computes the 0..1 fraction, line 776 multiplies by 100).
  - The gate at hypothesis.py:1189 reads `report["clv"]["positive_clv_rate"]`
    from backtest events (0..1 fraction there, :792-793) and compares against
    MIN_CLV_RATE = 0.005 (hypothesis.py:79).
  - Two inconsistent scales feed one threshold: fraction-of-trades vs a
    0.005 magnitude. With n ≥ 10 trades, ONE positive-CLV trade ⇒ rate ≥ 0.10
    ⇒ PASS at 20× the floor. The gate binds ONLY when literally zero trades
    closed positive — i.e. it is vacuous as a promotion statistic and would
    remain vacuous if the rebuild simply reuses it.
  - Compounding: the gate reads `paper_trades.clv_implied`, a THIRD unit
    (raw devigged-nowhere implied-prob delta), not the canonical
    `clv_log.clv_prob_bp` the two clv_tracker writers maintain.
  - Missing-data renders as FAIL "CLV rate 0.0% < 0%" (f-string rounds 0.005
    to 0%) — a formatting lie, ROADMAP-confirmed, still present at :1192.
Test: tests/test_tier0_money_sizing_and_units.py::TestClvGateUnitSemantics
pins the 1-in-10-passes arithmetic using the real report builder.
Falsifier: construct a gate evaluation with ≥10 resolved trades, ≥1 positive
CLV, and a FAIL verdict on min_clv_rate. (I could not: the arithmetic forbids it.)
For: gate rebuild (instance3 owns hypothesis.py) — fix is: gate on mean
clv_prob_bp (or its positive rate) from clv_log, document the unit, render
the threshold honestly, treat NULL as insufficient-data.

## [VERIFIED] bet_executor.py regime multiplier silently halves stakes in the offseason
Blast radius: SILENT (if armed)
Evidence: `_clamped_regime_multiplier("basketball_nba")` returns ~0.5 in August
(NBA preseason/offseason phase) — my first executor test run returned $45.50
instead of $91.00 for an identical bet purely because of the calendar. The
multiplier is applied inside compute_portfolio_stakes with only a debug-level
log when the lookup FAILS; successful lookups log at info. An operator arming
the path in a sport's offseason would see all stakes halved (or ×0.1 at the
clamp floor) with no error. This is arguably intended (regime-aware sizing),
but the interaction with MIN_BET_AMOUNT means offseason stakes can silently
floor to 0 (no bets) rather than announce "regime says no".
Falsifier: call `_clamped_regime_multiplier('basketball_nba')` in July vs
January; if both return 1.0 the finding is stale.
For: me (bet_executor.py) — recommend: log the multiplier at placement time in
the executor_log `details` column; currently only the app log carries it.

## [VERIFIED] bet_executor.py:1003 — fraction-of-bankroll written to `kelly_at_placement` column is mislabeled (ROADMAP N5 CONFIRMED, narrower than reported)
Blast radius: SILENT (display only)
Evidence: `_record_bet` writes `round(stake / bankroll, 4)` into the
`kelly_at_placement` column (bet_executor.py:1003). The column name says Kelly;
the value is stake fraction (e.g. 0.0091 for a $91 bet on $10k). The actual
Kelly fraction was ~0.0091/0.25 = 0.0364 pre-dampener. Any reader treating
kelly_at_placement as a Kelly fraction understates the true Kelly by the
fractional factor. clv_tracker.record_bet (:230) writes a REAL Kelly fraction
into the same-named column — two writers, two semantics, one column.
Falsifier: query both writers' outputs for the same bet parameters; they
disagree by ~4×.
For: me (bet_executor.py) — fix when arming: write kelly_dynamic's
`kelly_base` or rename the column.

## [VERIFIED] kelly.py `calculate_units` is NOT Kelly — linearized, stepwise-tier, and inconsistent with kelly_dynamic
Blast radius: SILENT (if used for sizing; currently appears uncalled from live paths — reachability check below)
Evidence: fraction = edge × kelly_fraction × tier_table_mult. Three divergences
from the real Kelly, all pinned in tests: (1) linear in edge (true Kelly is
~linear for small edges at fixed odds, so this one is benign); (2) uses the
STEPWISE AGP_TIER_MULTIPLIERS table (0.80 at conf 0.8) while kelly_dynamic
uses the smoothed lerp (0.8667) — same bet, two functions, ~8% apart before
(3) the variance dampener, which calculate_units omits entirely. Net effect
at the reference bet: $30.00 vs $91.00 on a $5k bankroll — 3× apart.
Falsifier: the two functions returning equal stakes for equal inputs.
For: me — recommend deleting or delegating to kelly_dynamic before arming.

## [INFERRED] reachability: calculate_units and kelly_portfolio have no production callers outside bet_executor
Blast radius: context
Evidence: grep for `calculate_units|kelly_portfolio|timing_value|ruin_probability`
across tools/ and api.py shows definitions and tests but no call sites outside
the module (verify at workstation — grep here covers the repo copy).
timing_value and ruin_probability appear entirely uncalled in production.
Q5: dead-but-plausible code manufacturing confidence.
Falsifier: a call site in autonomous.py/api.py.
For: me (attic candidates per AUDIT_MANDATE rule 4 — NOT deleted)

## [VERIFIED] sizing.py `best_price` tie-break and improvement math — correct
Blast radius: n/a
Evidence: picks max decimal; tie goes to DK deterministically; improvement_pct
= best/other − 1 on decimals (correct relative-price measure). Pinned by test
including a no-worse-price sweep over 25 odds pairs.
For: unowned

## Q6/Q7 quick answers (per mandate §6 dossier format)
- kelly.py: purpose = bankroll sizing under confidence tiers (docstring honest).
  Build-today: same math, but one entry point (kelly_dynamic) instead of three
  divergent sizers; calculate_units deleted; timing_value/ruin_probability
  attic'd until a caller exists. Retirement condition: none — load-bearing,
  now tested.
- clv_tracker.py: purpose = permanent CLV record. Canonical bp path is right;
  retirement condition: never (it IS the statistic). Fix the gate to read it.
- bet_executor.py: purpose = place bets. Correctly dormant. The safety stack
  (exposure caps, drawdown kill, dup-guard, bankroll lock) is genuinely well
  built — better than the rest of the repo — and now characterized. Safe to
  arm ONLY after: gate rebuild lands + paper-mode end-to-end + owner consent
  (ROADMAP §6.10 ordering is correct).

## Environment correction for COORDINATION.md (factual, not a defect)
The 29 pre-existing money-path tests ERROR without pytest-asyncio (not
installed on this machine). Wave-1's 39.6/45.7/46.3% baselines were measured
somewhere it worked; on this sandbox they are not reproducible as stated. My
subset numbers above are reproducible here: kelly 72%, sizing 100%,
bet_executor 55%, clv_tracker 30% (46% incl. pre-existing tests).
